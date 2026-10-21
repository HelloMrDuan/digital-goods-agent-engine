from __future__ import annotations

from typing import Any, Callable

from ..db import conn, dumps, loads, now_iso
from ..security import redact_error


def run_paid_once(operation: str, entity_id: int, snapshot: str, callback: Callable[[], dict[str, Any]], *, force: bool = False) -> dict[str, Any]:
    """Persist request claims across processes; repeated snapshots reuse completed work.

    A RUNNING claim is never expired automatically: a crashed process may already
    have incurred provider charges. An operator must reconcile it before retrying.
    """
    with conn() as c:
        c.execute("BEGIN IMMEDIATE")
        c.execute("""CREATE TABLE IF NOT EXISTS paid_workflows (
            operation TEXT NOT NULL, entity_id INTEGER NOT NULL, snapshot_hash TEXT NOT NULL,
            status TEXT NOT NULL, result_json TEXT, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY(operation,entity_id,snapshot_hash))""")
        active = c.execute("SELECT 1 FROM paid_workflows WHERE operation=? AND entity_id=? AND status='RUNNING'",(operation,entity_id)).fetchone()
        if active:
            raise ValueError("paid workflow is already RUNNING; reconcile interrupted work before retrying")
        previous = c.execute("SELECT * FROM paid_workflows WHERE operation=? AND entity_id=? AND snapshot_hash=?",(operation,entity_id,snapshot)).fetchone()
        if previous and not force:
            if previous["status"] == "SUCCESS":
                result = loads(previous["result_json"],{})
                return {**result,"idempotent":True}
            raise ValueError("paid workflow previously failed; inspect costs and use an explicit force retry")
        now=now_iso()
        c.execute("""INSERT INTO paid_workflows(operation,entity_id,snapshot_hash,status,created_at,updated_at)
                     VALUES(?,?,?,'RUNNING',?,?) ON CONFLICT(operation,entity_id,snapshot_hash)
                     DO UPDATE SET status='RUNNING',result_json=NULL,error=NULL,updated_at=excluded.updated_at""",
                  (operation,entity_id,snapshot,now,now))
    try:
        result=callback()
    except Exception as exc:
        with conn() as c:
            c.execute("UPDATE paid_workflows SET status='FAILED',error=?,updated_at=? WHERE operation=? AND entity_id=? AND snapshot_hash=?",
                      (redact_error(exc),now_iso(),operation,entity_id,snapshot))
        raise
    with conn() as c:
        c.execute("UPDATE paid_workflows SET status='SUCCESS',result_json=?,updated_at=? WHERE operation=? AND entity_id=? AND snapshot_hash=?",
                  (dumps(result),now_iso(),operation,entity_id,snapshot))
    return result
