from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any

from ..config import settings
from ..db import now_iso
from .clustering import rebuild_clusters
from .sources import collect_source, list_sources

_lock = threading.Lock()
_thread: threading.Thread | None = None
_stop = threading.Event()
_status: dict[str, Any] = {
    "enabled": False,
    "running": False,
    "last_started_at": None,
    "last_finished_at": None,
    "last_error": None,
    "last_result": None,
}


def run_cycle() -> dict[str, Any]:
    if not _lock.acquire(blocking=False):
        return {"status": "SKIPPED", "reason": "cycle already running"}
    _status.update({"running": True, "last_started_at": now_iso(), "last_error": None})
    try:
        sources = [x for x in list_sources() if x.get("enabled")]
        results = []
        for source in sources:
            try:
                results.append(collect_source(int(source["id"])))
            except Exception as exc:
                results.append({"source_id": source["id"], "status": "FAILED", "error": str(exc)})
        cluster = rebuild_clusters() if any(x.get("status") in {"SUCCESS", "EMPTY"} for x in results) else None
        result = {"status": "SUCCESS", "processed_sources": len(results), "success_count": sum(1 for x in results if x.get("status") in {"SUCCESS","EMPTY"}), "skipped_count": sum(1 for x in results if x.get("status")=="SKIPPED"), "failed_count": sum(1 for x in results if x.get("status")=="FAILED"), "results": results, "cluster_run": cluster}
        _status["last_result"] = result
        return result
    except Exception as exc:
        _status["last_error"] = str(exc)
        raise
    finally:
        _status["running"] = False
        _status["last_finished_at"] = now_iso()
        _lock.release()


def _loop() -> None:
    # Delay first automatic network activity after boot. Manual run-now remains available immediately.
    interval = max(15, int(settings.auto_collect_interval_minutes)) * 60
    while not _stop.wait(30):
        try:
            run_cycle()
        except Exception:
            pass
        if _stop.wait(max(30, interval - 30)):
            break


def start_scheduler() -> None:
    global _thread
    _status["enabled"] = bool(settings.auto_collect_enabled)
    if not settings.auto_collect_enabled or (_thread and _thread.is_alive()):
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="real-data-collector", daemon=True)
    _thread.start()


def stop_scheduler() -> None:
    _stop.set()


def scheduler_status() -> dict[str, Any]:
    return {
        **_status,
        "enabled": bool(settings.auto_collect_enabled),
        "interval_minutes": int(settings.auto_collect_interval_minutes),
        "real_data_only": True,
        "ai_called_by_scheduler": False,
    }
