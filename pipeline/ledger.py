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


def spent_usd(ledger_path: Path, run_id: str | None = None) -> float:
    if not ledger_path.exists():
        return 0.0
    total = 0.0
    for line in ledger_path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if run_id and row.get("run_id") != run_id:
            continue
        if not row.get("mock"):
            total += float(row.get("cost_usd") or 0.0)
    return round(total, 4)


def ensure_budget(projected_usd: float, ledger_path: Path, max_usd: float) -> None:
    """任何付费调用之前必须过这一关。"""
    current = spent_usd(ledger_path)
    if current + projected_usd > max_usd:
        raise BudgetExceeded(
            f"预算拦截：已花费 ${current:.2f} + 本次预计 ${projected_usd:.2f} "
            f"超过总预算 ${max_usd:.2f}")
