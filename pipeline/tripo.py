"""Tripo 多图建模客户端。显式槽位传参（front/left/back/right），带输入哈希缓存与防重复提交。

坑位说明（来自调研文档）：
- SDK 的 images 位置参数按 前/左/后/右 解释；本项目文件对应 view_1前 view_2右 view_3后 view_4左。
  本客户端走原始 HTTP API，inputs 里每个 token 显式带槽位名，顺序无关，天然避开该坑。
- model_version 默认是旧版 v2.5，必须显式传 v3.1-20260211。
"""
import hashlib
import json
import time
from pathlib import Path

import httpx

from . import ledger
from .config import api_key

# 槽位 -> 本项目冻结文件名（view_1 前 / view_2 右 / view_3 后 / view_4 左）
SLOT_OF_VIEW = {"view_1.png": "front", "view_2.png": "right",
                "view_3.png": "back", "view_4.png": "left"}


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def cache_key(view_files: dict[str, Path], params: dict) -> str:
    """按输入图片内容 + 冻结参数算缓存键，四图与参数都相同才复用 GLB。"""
    parts = [params[k] for k in sorted(params)]
    for name in sorted(view_files):
        parts.append(f"{name}:{digest(view_files[name])}")
    return hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:32]


def frozen_params(cfg: dict) -> dict:
    t = cfg["tripo"]
    return {"model": t["model"], "face_limit": t["face_limit"], "model_seed": t["model_seed"],
            "texture": t["texture"], "pbr": t["pbr"], "geometry_quality": t["geometry_quality"]}


def seed_cache(cfg: dict, ledger_path: Path, provenance: str) -> Path:
    """把冻结 GLB 预置为缓存命中：输入 = 03 文件夹白底四图（与 20260921 批次逐字节一致），
    输出 = 该批次 Tripo 实际返回的 model.glb。三组实验条件因此共用同一几何（SHA-256 固定）。"""
    views_dir = Path(cfg["_root"]) / cfg["pipeline"]["views_dir"]
    view_files = {n: views_dir / n for n in SLOT_OF_VIEW}
    for p in view_files.values():
        if not p.exists():
            raise FileNotFoundError(f"冻结视图缺失：{p}")
    key = cache_key(view_files, frozen_params(cfg))
    cache_dir = Path(cfg["_root"]) / "data" / "cache" / "tripo" / key
    glb = cache_dir / "model.glb"
    if glb.exists():
        return glb
    cache_dir.mkdir(parents=True, exist_ok=True)
    seed_src = Path(cfg["_root"]) / cfg["pipeline"]["seed_glb"]
    glb.write_bytes(seed_src.read_bytes())
    (cache_dir / "meta.json").write_text(json.dumps({
        "source": "cache_seed", "provenance": provenance,
        "params": frozen_params(cfg), "input_sha256": {n: digest(p) for n, p in view_files.items()},
        "model_sha256": digest(glb), "credits_consumed": 0, "seeded_ts": time.time(),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    ledger.log({"stage": "tripo_seed_cache", "provider": "tripo", "status": "cache_seeded",
                "cost_usd": 0.0, "cache_key": key, "model_sha256": digest(glb),
                "mock": True, "note": provenance}, ledger_path)
    return glb


def ensure_glb(cfg: dict, run_dir: Path, ledger_path: Path, run_id: str, dry_run: bool) -> Path:
    """缓存优先；未命中时提交 Tripo 任务（唯一扣费点 $0.20）。dry-run 下未命中直接报错，不静默造假。"""
    views_dir = Path(cfg["_root"]) / cfg["pipeline"]["views_dir"]
    view_files = {n: views_dir / n for n in SLOT_OF_VIEW}
    params = frozen_params(cfg)
    key = cache_key(view_files, params)
    cache_dir = Path(cfg["_root"]) / "data" / "cache" / "tripo" / key
    glb = cache_dir / "model.glb"

    if glb.exists():                                   # 缓存命中：免费、可无限复用
        meta = json.loads((cache_dir / "meta.json").read_text(encoding="utf-8"))
        ledger.log({"run_id": run_id, "stage": "tripo_model", "provider": "tripo",
                    "status": "cache_hit", "cost_usd": 0.0, "cache_key": key,
                    "model_sha256": meta["model_sha256"], "mock": False}, ledger_path)
        return glb

    if dry_run:
        raise RuntimeError("dry-run 下 Tripo 缓存未命中：先运行 `python run.py seed-tripo-cache` "
                           "预置冻结 GLB，或去掉 --dry-run 真实建模（$0.20/次）")

    key_env = "TRIPO_API_KEY"
    token = api_key(key_env)
    if not token:
        raise RuntimeError(f".env 缺少 {key_env}；请填写后重试")
    auth = {"Authorization": f"Bearer {token}"}
    base = cfg["tripo"]["base_url"].rstrip("/")
    t0 = time.time()
    ensure_budget_call(cfg, ledger_path)

    state_path = cache_dir / "task_state.json"
    cache_dir.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=180) as client:
        if not state_path.exists():                    # 只允许首次提交；之后只轮询
            inputs = []
            for name, slot in SLOT_OF_VIEW.items():
                up = client.post(f"{base}/files", headers=auth,
                                 files={"file": (name, open(view_files[name], "rb"), "image/png")}).json()
                if up.get("code") != 0:
                    raise RuntimeError(f"Tripo 视图上传被拒（{slot}）：{up}")
                inputs.append({slot: up["data"]["file_token"]})
            body = {**params, "inputs": inputs}
            r = client.post(f"{base}/generation/multiview-to-model", headers=auth, json=body).json()
            (cache_dir / "submit_response.json").write_text(json.dumps(r, ensure_ascii=False, indent=2),
                                                            encoding="utf-8")
            if r.get("code") != 0:
                raise RuntimeError(f"Tripo 任务被拒：{r}")
            task_id = r["data"]["task_id"]
            state_path.write_text(json.dumps({"task_id": task_id, "status": "submitted"}), encoding="utf-8")
            ledger.log({"run_id": run_id, "stage": "tripo_model", "provider": "tripo",
                        "status": "submitted", "task_id": task_id,
                        "cost_usd": cfg["tripo"]["credits_per_task"] * cfg["tripo"]["usd_per_credit"],
                        "credits": cfg["tripo"]["credits_per_task"], "mock": False,
                        "latency_s": round(time.time() - t0, 1)}, ledger_path)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        task_id = state["task_id"]                     # 恢复路径：绝不重复提交，只轮询原任务
        deadline = time.time() + cfg["tripo"]["poll_timeout_s"]
        while True:
            q = client.get(f"{base}/tasks/{task_id}", headers=auth).json()
            status = q.get("data", {}).get("status")
            if status == "success":
                break
            if status in ("failed", "banned", "cancelled", "expired"):
                raise RuntimeError(f"Tripo 任务终态失败：{status}")
            if time.time() > deadline:
                raise RuntimeError(f"Tripo 轮询超时（任务 {task_id} 仍在 {status}）；"
                                   f"恢复方式：重跑本命令，只会继续轮询，不会重复扣费提交")
            time.sleep(cfg["tripo"]["poll_interval_s"])
        url = q["data"]["output"]["model_url"]
        data = client.get(url).content
        if data[:4] != b"glTF":
            raise ValueError("Tripo 输出不是 GLB")
        glb.write_bytes(data)
        credits = q["data"].get("credits_consumed", cfg["tripo"]["credits_per_task"])
        (cache_dir / "meta.json").write_text(json.dumps({
            "source": "api", "task_id": task_id, "params": params,
            "input_sha256": {n: digest(p) for n, p in view_files.items()},
            "model_sha256": digest(glb), "credits_consumed": credits,
            "finished_ts": time.time()}, ensure_ascii=False, indent=2), encoding="utf-8")
        ledger.log({"run_id": run_id, "stage": "tripo_model", "provider": "tripo",
                    "status": "downloaded", "task_id": task_id, "cost_usd": credits * 0.01,
                    "credits": credits, "model_sha256": digest(glb), "mock": False,
                    "latency_s": round(time.time() - t0, 1)}, ledger_path)
        return glb


def ensure_budget_call(cfg: dict, ledger_path: Path) -> None:
    ledger.ensure_budget(cfg["tripo"]["credits_per_task"] * cfg["tripo"]["usd_per_credit"],
                         ledger_path, cfg["budget"]["max_usd"])
