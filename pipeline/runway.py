"""Runway Seedance 2.5 参考视频客户端。防重复提交：状态文件一旦有 task_id 只轮询不重发。"""
import base64
import hashlib
import json
import time
from pathlib import Path

import httpx

from . import ledger
from .config import api_key


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _data_uri(path: Path, mime: str, limit: int) -> str:
    raw = path.read_bytes()
    uri = "data:" + mime + ";base64," + base64.b64encode(raw).decode()
    if len(uri) > limit:
        raise ValueError(f"{path.name} exceeds the {limit // 1048576} MiB data URI limit. "
                         f"The file is {len(raw) // 1024} kB. Prepare a lower-bitrate reference.")
    return uri


def _headers(key: str, cfg: dict) -> dict:
    return {"Authorization": f"Bearer {key}",
            "X-Runway-Version": cfg["runway"]["api_version"]}


def build_request(cfg: dict, reference_mp4: Path, view_files: dict[str, Path],
                  prompt_text: str, quality: str) -> tuple[dict, dict]:
    r = cfg["runway"]
    q = r["qualities"][quality]
    body = {"model": r["model"], "mode": r["mode"],
            "promptVideo": _data_uri(reference_mp4, "video/mp4", r["data_uri_limit_bytes"]),
            "references": [{"uri": _data_uri(view_files[n], "image/png", r["data_uri_limit_bytes"])}
                           for n in sorted(view_files)],
            "promptText": prompt_text,
            "duration": r["duration"], "ratio": q["ratio"],
            "audio": r["audio"], "seed": r["seed"]}
    record = {"model": r["model"], "mode": r["mode"], "quality": quality, "ratio": q["ratio"],
              "duration": r["duration"], "audio": r["audio"], "seed": r["seed"],
              "request_sha256": hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest(),
              "reference_video_sha256": digest(reference_mp4),
              "estimated_credits": q["credits"], "estimated_usd": q["usd"]}
    return body, record


def submit(cfg: dict, run_dir: Path, ledger_path: Path, run_id: str, body: dict, record: dict,
           dry_run: bool) -> Path:
    """提交并轮询到成片下载。返回 video.mp4 路径。dry-run 只冻结请求不触网。"""
    out = run_dir / "commercial"
    out.mkdir(parents=True, exist_ok=True)
    state_path = out / "state.json"

    if dry_run:
        (out / "frozen_request.json").write_text(json.dumps(record, ensure_ascii=False, indent=2),
                                                 encoding="utf-8")
        placeholder = out / "video.mp4"
        if not placeholder.exists():
            placeholder.write_bytes(b"")               # dry-run 占位，标明未真实生成
        ledger.log({"run_id": run_id, "stage": "seedance", "provider": "runway",
                    "status": "dry_run_frozen", "cost_usd": 0.0,
                    "estimated_usd": record["estimated_usd"], "mock": True,
                    "request_sha256": record["request_sha256"]}, ledger_path)
        return placeholder

    key = api_key("RUNWAYML_API_SECRET")
    if not key:
        raise RuntimeError("Configure RUNWAYML_API_SECRET before submitting to Runway.")
    base = cfg["runway"]["base_url"].rstrip("/")
    ledger.ensure_budget(record["estimated_usd"], ledger_path, cfg["budget"]["max_usd"])
    t0 = time.time()

    with httpx.Client(timeout=180) as client:
        if not state_path.exists():                    # 首次提交
            (out / "frozen_request.json").write_text(json.dumps(record, ensure_ascii=False, indent=2),
                                                     encoding="utf-8")
            balance = client.get(f"{base}/organization", headers=_headers(key, cfg)).json()["creditBalance"]
            if balance < record["estimated_credits"]:
                raise ValueError(f"Runway has {balance} credits available. The estimate is {record['estimated_credits']} credits.")
            state_path.write_text(json.dumps({"status": "submitting", "balance_before": balance}),
                                  encoding="utf-8")
            resp = client.post(f"{base}/video_to_video", headers=_headers(key, cfg), json=body).json()
            (out / "submit_response.json").write_text(json.dumps(resp, ensure_ascii=False, indent=2),
                                                      encoding="utf-8")
            task_id = resp.get("id")
            if not task_id:
                raise RuntimeError("Runway returned no task ID. The submission state is uncertain. "
                                   "Check the Runway dashboard before attempting another submission.")
            state = {"status": "submitted", "task_id": task_id, "balance_before": balance,
                     "request_sha256": record["request_sha256"]}
            state_path.write_text(json.dumps(state), encoding="utf-8")
            ledger.log({"run_id": run_id, "stage": "seedance", "provider": "runway",
                        "status": "submitted", "task_id": task_id,
                        "cost_usd": record["estimated_usd"],
                        "credits": record["estimated_credits"], "mock": False,
                        "latency_s": round(time.time() - t0, 1)}, ledger_path)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("request_sha256") != record["request_sha256"]:
            raise ValueError("This request differs from the submitted task. Use a new run for a different request.")
        task_id = state["task_id"]                     # 恢复路径：只轮询原任务
        while True:
            resp = client.get(f"{base}/tasks/{task_id}", headers=_headers(key, cfg)).json()
            (out / "query_response.json").write_text(json.dumps(resp, ensure_ascii=False, indent=2),
                                                     encoding="utf-8")
            status = resp["status"]
            if status == "SUCCEEDED":
                break
            if status in ("FAILED", "CANCELLED"):
                failure = resp.get("failure", "")
                ledger.log({"run_id": run_id, "stage": "seedance", "provider": "runway",
                            "status": f"terminal_{status}", "task_id": task_id,
                            "failure": str(failure), "cost_usd": 0.0, "mock": False}, ledger_path)
                raise RuntimeError(f"Runway task ended with status {status}. "
                                   f"Review the recorded failure before authorizing a new run.")
            time.sleep(10)
        url = resp["output"][0]
        if not str(url).startswith("https://"):
            raise ValueError("The output did not include a valid HTTPS video URL.")
        video = out / "video.mp4"
        if not video.exists():
            video.write_bytes(client.get(url).content)
        state.update(status="SUCCEEDED")
        state_path.write_text(json.dumps(state), encoding="utf-8")
        balance_after = client.get(f"{base}/organization", headers=_headers(key, cfg)).json()["creditBalance"]
        credits_used = state["balance_before"] - balance_after
        ledger.log({"run_id": run_id, "stage": "seedance", "provider": "runway",
                    "status": "downloaded", "task_id": task_id,
                    "cost_usd": round(credits_used * 0.01, 2), "credits": credits_used,
                    "video_sha256": digest(video), "mock": False,
                    "latency_s": round(time.time() - t0, 1)}, ledger_path)
        return video
