from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ...config import settings
from ...db import conn, now_iso


class BudgetExceededError(RuntimeError):
    pass


def _ensure_schema(c) -> None:
    c.execute(
        """CREATE TABLE IF NOT EXISTS ai_budget_reservations (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               provider TEXT NOT NULL,
               reserved_cost_cny REAL NOT NULL,
               created_at TEXT NOT NULL
           )"""
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_ai_budget_reservations_created ON ai_budget_reservations(created_at)"
    )


def reserve_ai_budget(provider: str, estimated_cost_cny: float) -> int:
    """Atomically reserve today's budget before a paid request starts."""
    amount = max(0.0, float(estimated_cost_cny or 0.0))
    if amount <= 0:
        amount = max(0.000001, float(settings.ai_unknown_call_reserve_cny or 0.0))
    budget = max(0.0, float(settings.daily_ai_budget_cny or 0.0))
    today = datetime.now(timezone.utc).date().isoformat()
    stale_cutoff = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(timespec="seconds")

    with conn() as c:
        c.execute("BEGIN IMMEDIATE")
        _ensure_schema(c)
        c.execute("DELETE FROM ai_budget_reservations WHERE created_at<?", (stale_cutoff,))
        spent_row = c.execute(
            "SELECT coalesce(sum(estimated_cost_cny),0) cost FROM ai_runs "
            "WHERE status='SUCCESS' AND substr(created_at,1,10)=?",
            (today,),
        ).fetchone()
        reserved_row = c.execute(
            "SELECT coalesce(sum(reserved_cost_cny),0) cost FROM ai_budget_reservations "
            "WHERE substr(created_at,1,10)=?",
            (today,),
        ).fetchone()
        spent = float(spent_row["cost"] or 0.0)
        reserved = float(reserved_row["cost"] or 0.0)
        if spent + reserved + amount > budget + 1e-12:
            raise BudgetExceededError(
                f"Daily AI budget would be exceeded: spent=¥{spent:.4f}, "
                f"reserved=¥{reserved:.4f}, request<=¥{amount:.4f}, budget=¥{budget:.2f}"
            )
        cur = c.execute(
            "INSERT INTO ai_budget_reservations(provider,reserved_cost_cny,created_at) VALUES(?,?,?)",
            (str(provider), amount, now_iso()),
        )
        return int(cur.lastrowid)


def release_ai_budget(reservation_id: int | None, c=None) -> None:
    if not reservation_id:
        return
    if c is not None:
        _ensure_schema(c)
        c.execute("DELETE FROM ai_budget_reservations WHERE id=?", (int(reservation_id),))
        return
    with conn() as owned:
        _ensure_schema(owned)
        owned.execute("DELETE FROM ai_budget_reservations WHERE id=?", (int(reservation_id),))


def budget_snapshot() -> dict[str, float]:
    today = datetime.now(timezone.utc).date().isoformat()
    with conn() as c:
        _ensure_schema(c)
        spent = c.execute(
            "SELECT coalesce(sum(estimated_cost_cny),0) cost FROM ai_runs "
            "WHERE status='SUCCESS' AND substr(created_at,1,10)=?",
            (today,),
        ).fetchone()
        reserved = c.execute(
            "SELECT coalesce(sum(reserved_cost_cny),0) cost FROM ai_budget_reservations "
            "WHERE substr(created_at,1,10)=?",
            (today,),
        ).fetchone()
    return {
        "spent_cny": float(spent["cost"] or 0.0),
        "reserved_cny": float(reserved["cost"] or 0.0),
        "budget_cny": float(settings.daily_ai_budget_cny or 0.0),
    }
