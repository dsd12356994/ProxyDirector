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
        raise ValueError(f"{path.name} 超过 {limit // 1048576}MiB data-URI 上限；"
                         f"请降低参考视频码率（当前 {len(raw) // 1024}KB）。不要静默重压缩冻结输入")
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
        raise RuntimeError(".env 缺少 RUNWAYML_API_SECRET")
    base = cfg["runway"]["base_url"].rstrip("/")
    ledger.ensure_budget(record["estimated_usd"], ledger_path, cfg["budget"]["max_usd"])
    t0 = time.time()

    with httpx.Client(timeout=180) as client:
        if not state_path.exists():                    # 首次提交
            (out / "frozen_request.json").write_text(json.dumps(record, ensure_ascii=False, indent=2),
                                                     encoding="utf-8")
            balance = client.get(f"{base}/organization", headers=_headers(key, cfg)).json()["creditBalance"]
            if balance < record["estimated_credits"]:
                raise ValueError(f"Runway 余额不足：{balance} < {record['estimated_credits']} 积分")
            state_path.write_text(json.dumps({"status": "submitting", "balance_before": balance}),
                                  encoding="utf-8")
            resp = client.post(f"{base}/video_to_video", headers=_headers(key, cfg), json=body).json()
            (out / "submit_response.json").write_text(json.dumps(resp, ensure_ascii=False, indent=2),
                                                      encoding="utf-8")
            task_id = resp.get("id")
            if not task_id:
                raise RuntimeError("提交未返回任务ID（提交状态不确定）：禁止重发 POST，"
                                   "请到 Runway 控制台核对后再决定")
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
            raise ValueError("请求要素与已提交任务不一致；如需新请求请换新 run 目录")
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
                raise RuntimeError(f"任务终态 {status}（{failure}）。按失败码人工决定是否重试，"
                                   f"本项目不自动付费重试；重试请新建 run 目录")
            time.sleep(10)
        url = resp["output"][0]
        if not str(url).startswith("https://"):
            raise ValueError("输出 URL 异常")
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
