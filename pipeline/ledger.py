"""JSONL 账本 + 预算闸。评价阶段的成本结论以本账本为准。"""
import json
import time
from pathlib import Path
from threading import Lock

_lock = Lock()


class BudgetExceeded(RuntimeError):
    pass


def log(entry: dict, ledger_path: Path) -> dict:
    """追加一行账本。所有工具调用（成功/失败/mock）都要记。"""
    row = {"ts": round(time.time(), 3), **entry}
    with _lock:
        with ledger_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def sum_usd(rows) -> float:
    """汇总非 mock 账本行的美元开销；带 task_id 的任务按 (provider, task_id) 去重，
    优先取最新 downloaded 行的实付金额，否则回退到 submitted 预估。"""
    total = 0.0
    by_task: dict[tuple, dict[str, float | None]] = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("mock"):
            continue
        cost = float(row.get("cost_usd") or 0.0)
        task_id = row.get("task_id")
        if not task_id:
            total += cost
            continue
        key = (row.get("provider"), task_id)
        slot = by_task.setdefault(key, {"downloaded": None, "submitted": None, "other": 0.0})
        status = row.get("status")
        if status == "downloaded":
            slot["downloaded"] = cost
        elif status == "submitted":
            slot["submitted"] = cost
        else:
            slot["other"] = cost
    for slot in by_task.values():
        if slot["downloaded"] is not None:
            total += slot["downloaded"]
        elif slot["submitted"] is not None:
            total += slot["submitted"]
        else:
            total += slot["other"]
    return round(total, 6)


def spent_usd(ledger_path: Path, run_id: str | None = None) -> float:
    if not ledger_path.exists():
        return 0.0
    rows = []
    for line in ledger_path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        if run_id and row.get("run_id") != run_id:
            continue
        rows.append(row)
    return round(sum_usd(rows), 4)


def ensure_budget(projected_usd: float, ledger_path: Path, max_usd: float) -> None:
    """任何付费调用之前必须过这一关。"""
    current = spent_usd(ledger_path)
    if current + projected_usd > max_usd:
        raise BudgetExceeded(
            f"Adding this estimate of USD {projected_usd:.2f} to the USD {current:.2f} already spent "
            f"exceeds the USD {max_usd:.2f} budget.")
