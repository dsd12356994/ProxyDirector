"""Kling 全能参考客户端（api-beijing.klingai.com，Bearer api-key 鉴权）。

与 runway.py 同构的安全骨架：状态文件先写后发、拿到 task_id 只轮询不重发、
失败不自动付费重试。差异点：
- feature_video 只收公网 URL（文档明示，图片才支持 base64）：参考视频与冻结
  视图发布到本团队 GitHub 仓库的 release（gh CLI），URL 稳定、团队可控。
- 计费是积分不是美元：试用旗舰包仅覆盖 Kling 4.0 / 4.0 Flash；有输入视频且
  输入时长≤输出时长时，总时长=2×输出时长。账本里 credits 单列，cost_usd 记 0
  （积分不在美元预算内，预算闸门由资源包余量检查承担）。
"""
import hashlib
import json
import subprocess
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


def _headers(key: str) -> dict:
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def packs(cfg: dict) -> list[dict]:
    """GET /account/costs（免费，QPS≤1）：返回生效中的视频资源包。"""
    key = api_key(cfg["kling"]["api_key_env"])
    if not key:
        raise RuntimeError(f"Configure {cfg['kling']['api_key_env']} before querying Kling.")
    base = cfg["kling"]["base_url"].rstrip("/")
    now_ms = int(time.time() * 1000)
    with httpx.Client(timeout=30) as client:
        r = client.get(f"{base}/account/costs",
                       params={"start_time": now_ms - 90 * 24 * 3600 * 1000,
                               "end_time": now_ms + 24 * 3600 * 1000},
                       headers=_headers(key))
        r.raise_for_status()
        body = r.json()
    if body.get("code") != 0:
        raise RuntimeError(f"Kling could not return the credit balance (code {body.get('code')}).")
    infos = body.get("data", {}).get("resource_pack_subscribe_infos", [])
    return [p for p in infos if p.get("status") == "online"]


def credits_remaining(cfg: dict) -> float:
    """生效中资源包的剩余积分之和（官方口径有 12h 统计延迟）。"""
    return round(sum(float(p.get("remaining_quantity") or 0) for p in packs(cfg)), 2)


def estimate_credits(cfg: dict, input_duration_s: float, output_duration_s: int) -> float:
    """有输入视频时：输入≤输出 → 总时长=2×输出；否则 输入+输出。"""
    k = cfg["kling"]
    rate = k["rates"][k["model"]][k["resolution"]]
    total_s = 2 * output_duration_s if input_duration_s <= output_duration_s \
        else input_duration_s + output_duration_s
    return round(total_s * rate, 2)


# ---------- 素材发布（feature_video 需要公网 URL） ----------

def _gh(repo: str, *args: str) -> str:
    proc = subprocess.run(["gh", *args, "--repo", repo], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"GitHub asset operation failed (exit code {proc.returncode}). Check the release and your gh login.")
    return proc.stdout.strip()


def _asset_url(cfg: dict, filename: str) -> str:
    k = cfg["kling"]
    return f"https://github.com/{k['gh_asset_repo']}/releases/download/{k['gh_asset_tag']}/{filename}"


def _ensure_release(cfg: dict) -> None:
    k = cfg["kling"]
    try:
        _gh(k["gh_asset_repo"], "release", "view", k["gh_asset_tag"], "--json", "name")
        return
    except RuntimeError:
        pass                                          # 不存在才创建；其他错误会在 create 处暴露
    _gh(k["gh_asset_repo"], "release", "create", k["gh_asset_tag"], "--title",
        f"pipeline assets {k['gh_asset_tag']}", "--notes",
        "Frozen appearance views and rendered reference videos for Kling image and video conditioning.", "--latest=false")


def publish_asset(cfg: dict, path: Path, filename: str) -> str:
    """把文件传到 GitHub release（先复制成目标资产名再上传——gh 只认 basename，
    `path#name` 重命名语法实测不生效）；已存在同名资产则跳过；上传后回验公网 200。"""
    k = cfg["kling"]
    _ensure_release(cfg)
    resp = json.loads(_gh(k["gh_asset_repo"], "release", "view", k["gh_asset_tag"], "--json", "assets"))
    if filename not in {a["name"] for a in resp.get("assets", [])}:
        import shutil
        import tempfile
        with tempfile.TemporaryDirectory() as td:            # basename 即资产名
            staged = Path(td) / filename
            shutil.copy2(path, staged)
            _gh(k["gh_asset_repo"], "release", "upload", k["gh_asset_tag"], str(staged))
    url = _asset_url(cfg, filename)
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        for attempt in range(6):                     # 新资产 CDN 生效有延迟；下载流偶发中断
            try:
                r = client.get(url)
            except httpx.HTTPError:
                r = None
            if r is not None and r.status_code == 200 and len(r.content) == path.stat().st_size:
                return url
            time.sleep(10)
    raise RuntimeError(f"Could not verify the published reference asset (HTTP {getattr(r, 'status_code', 'error')}).")


VIEW_ASSET_NAMES = {"view_1.png", "view_2.png", "view_3.png", "view_4.png"}


def ensure_views_published(cfg: dict, view_files: dict[str, Path]) -> dict[str, str]:
    """冻结四视图只发布一次，之后复用同一 URL。"""
    urls = {}
    for name in sorted(view_files):
        urls[name] = publish_asset(cfg, view_files[name], name)
    return urls


# ---------- 请求构建与提交 ----------

def build_request(cfg: dict, run_id: str, reference_mp4: Path, view_files: dict[str, Path],
                  prompt_text: str, dry_run: bool = False) -> tuple[dict, dict]:
    """构建全能参考请求。dry_run=True 时只推算素材 URL 不上传（保持全程不触网）。"""
    k = cfg["kling"]
    if dry_run:
        reference_url = _asset_url(cfg, f"{run_id}_reference.mp4")
        view_urls = {n: _asset_url(cfg, n) for n in sorted(view_files)}
    else:
        reference_url = publish_asset(cfg, reference_mp4, f"{run_id}_reference.mp4")
        view_urls = ensure_views_published(cfg, view_files)
    contents = [{"type": "prompt", "text": prompt_text},
                {"type": "feature_video", "url": reference_url, "id": "video_1"}]
    contents += [{"type": "refer_image", "url": view_urls[n], "id": f"image_{i}"}
                 for i, n in enumerate(sorted(view_urls), start=1)]
    body = {"contents": contents,
            "settings": {"resolution": k["resolution"], "aspect_ratio": k["aspect_ratio"],
                         "duration": k["duration"], "audio": k["audio"]},
            "options": {"watermark_info": {"enabled": k["watermark"]}}}
    # 计费输入时长 = 参考视频时长（feature_video）；输出时长显式指定
    record = {"provider": "kling", "model": k["model"], "resolution": k["resolution"],
              "aspect_ratio": k["aspect_ratio"], "duration": k["duration"], "audio": k["audio"],
              "watermark": k["watermark"],
              "input_duration_s": cfg["pipeline"]["frames"] / cfg["pipeline"]["fps"],
              "reference_url": reference_url, "assets_published": not dry_run,
              "request_sha256": hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest(),
              "reference_video_sha256": digest(reference_mp4),
              "estimated_kling_credits": estimate_credits(
                  cfg, cfg["pipeline"]["frames"] / cfg["pipeline"]["fps"], k["duration"])}
    return body, record


def submit(cfg: dict, run_dir: Path, ledger_path: Path, run_id: str, body: dict, record: dict,
           dry_run: bool) -> Path:
    """提交并轮询到成片下载。返回 commercial/video.mp4。dry-run 只冻结请求不触网。"""
    out = run_dir / "commercial"
    out.mkdir(parents=True, exist_ok=True)
    state_path = out / "kling_state.json"
    k = cfg["kling"]

    if dry_run:
        (out / "frozen_request.json").write_text(json.dumps(record, ensure_ascii=False, indent=2),
                                                 encoding="utf-8")
        placeholder = out / "video.mp4"
        if not placeholder.exists():
            placeholder.write_bytes(b"")               # dry-run 占位，标明未真实生成
        ledger.log({"run_id": run_id, "stage": "seedance", "provider": "kling",
                    "status": "dry_run_frozen", "cost_usd": 0.0,
                    "estimated_kling_credits": record["estimated_kling_credits"], "mock": True,
                    "request_sha256": record["request_sha256"]}, ledger_path)
        return placeholder

    key = api_key(k["api_key_env"])
    if not key:
        raise RuntimeError(f"Configure {k['api_key_env']} before submitting to Kling.")
    base = k["base_url"].rstrip("/")
    t0 = time.time()
    est = record["estimated_kling_credits"]

    with httpx.Client(timeout=180) as client:
        if not state_path.exists():                    # 首次提交
            (out / "frozen_request.json").write_text(json.dumps(record, ensure_ascii=False, indent=2),
                                                     encoding="utf-8")
            # 预算闸门（积分版）：资源包余量须覆盖本次预估（余量统计有 12h 延迟）
            remaining = credits_remaining(cfg)
            if remaining < est:
                raise ValueError(f"Kling has {remaining} credits available. The estimate is {est} credits.")
            state_path.write_text(json.dumps(
                {"status": "submitting", "credits_before": remaining,
                 "request_sha256": record["request_sha256"]}), encoding="utf-8")
            resp = client.post(f"{base}/omni-video/{k['model']}", headers=_headers(key),
                               json=body).json()
            (out / "submit_response.json").write_text(json.dumps(resp, ensure_ascii=False, indent=2),
                                                      encoding="utf-8")
            task_id = resp.get("data", {}).get("id") if resp.get("code") == 0 else None
            if not task_id:
                raise RuntimeError(f"Kling returned no task ID (code {resp.get('code')}). "
                                   f"Check the Kling dashboard before attempting another submission.")
            state = {"status": "submitted", "task_id": task_id,
                     "credits_before": remaining, "request_sha256": record["request_sha256"]}
            state_path.write_text(json.dumps(state), encoding="utf-8")
            ledger.log({"run_id": run_id, "stage": "seedance", "provider": "kling",
                        "status": "submitted", "task_id": task_id, "cost_usd": 0.0,
                        "kling_credits": est, "mock": False,
                        "latency_s": round(time.time() - t0, 1)}, ledger_path)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("request_sha256") != record["request_sha256"]:
            raise ValueError("This request differs from the submitted task. Use a new run for a different request.")
        task_id = state["task_id"]                     # 恢复路径：只轮询原任务
        deadline = time.time() + k["poll_timeout_s"]
        while True:
            resp = client.get(f"{base}/tasks", params={"task_ids": task_id},
                              headers=_headers(key)).json()
            (out / "query_response.json").write_text(json.dumps(resp, ensure_ascii=False, indent=2),
                                                     encoding="utf-8")
            task = next((t for t in resp.get("data", []) if t.get("id") == task_id), None)
            status = (task or {}).get("status", "unknown")
            if status == "succeeded":
                break
            if status == "failed":
                failure = (task or {}).get("message", "")
                ledger.log({"run_id": run_id, "stage": "seedance", "provider": "kling",
                            "status": "terminal_failed", "task_id": task_id,
                            "failure": str(failure), "cost_usd": 0.0, "mock": False}, ledger_path)
                raise RuntimeError(f"Kling reported that the video task failed. "
                                   f"Review the recorded failure before authorizing a new run.")
            if time.time() > deadline:
                raise RuntimeError(f"Polling timed out after {k['poll_timeout_s']}s. Task {task_id} is still {status}. "
                                   f"Resume the same run to check the existing task.")
            time.sleep(k["poll_interval_s"])
        outputs = (task or {}).get("outputs") or []
        video_out = next((o for o in outputs if o.get("type") == "video"), None)
        url = (video_out or {}).get("url", "")
        if not str(url).startswith("https://"):
            raise ValueError("Kling returned an invalid video URL.")
        video = out / "video.mp4"
        if not video.exists():                         # 防盗链 URL 有有效期，先到先存
            last_err = None
            for _ in range(4):                         # 下载偶发被 CDN 掐断，重试
                try:
                    video.write_bytes(client.get(url, timeout=120).content)
                    last_err = None
                    break
                except httpx.HTTPError as e:
                    last_err = e
                    time.sleep(5)
            if last_err is not None:
                raise RuntimeError(f"The video task succeeded, but the download failed. Resume the same run to download the existing output.")
        state.update(status="succeeded")
        state_path.write_text(json.dumps(state), encoding="utf-8")
        usage = task.get("usage") or {}
        billed = usage.get("output_duration")
        credits_after_query = credits_remaining(cfg)
        ledger.log({"run_id": run_id, "stage": "seedance", "provider": "kling",
                    "status": "downloaded", "task_id": task_id, "cost_usd": 0.0,
                    "kling_credits": est, "pack_remaining_after": credits_after_query,
                    "billed_output_duration_s": billed, "video_sha256": digest(video),
                    "mock": False, "latency_s": round(time.time() - t0, 1)}, ledger_path)
        return video
