from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from dataclasses import asdict
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
import uvicorn

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Important: this bridge runs on the user's host, where Agent-Reach/OpenCLI can access
# the user's existing browser session. It never fabricates data and never logs cookies.
from app.collectors.agent_reach import collect, doctor

app = FastAPI(title="Agent-Reach Local Read Bridge", version="1.2")
TOKEN = os.getenv("AGENT_REACH_BRIDGE_TOKEN", "")


class CollectIn(BaseModel):
    platform: str = Field(min_length=2, max_length=50)
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=20, ge=1, le=100)
    enrich: bool = False
    detail_limit: int = Field(default=8, ge=0, le=30)
    comments_limit: int = Field(default=15, ge=0, le=100)


def auth(authorization: str | None) -> None:
    if TOKEN and authorization != f"Bearer {TOKEN}":
        raise HTTPException(401, "invalid bridge token")


@app.get("/health")
def health(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth(authorization)
    return {"status": "ok", "real_data_only": True, "read_only": True, "token_required": bool(TOKEN)}


@app.get("/doctor")
def bridge_doctor(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth(authorization)
    # Temporarily prevent recursive bridge routing if the host .env also contains a bridge URL.
    from app.config import settings
    old = settings.agent_reach_bridge_url
    settings.agent_reach_bridge_url = ""
    try:
        result = doctor()
    finally:
        settings.agent_reach_bridge_url = old
    return result


@app.post("/collect")
def bridge_collect(body: CollectIn, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    auth(authorization)
    from app.config import settings
    old = settings.agent_reach_bridge_url
    settings.agent_reach_bridge_url = ""
    try:
        items = collect(body.platform, body.query, body.limit, enrich=body.enrich, detail_limit=body.detail_limit, comments_limit=body.comments_limit)
    except Exception as exc:
        raise HTTPException(502, str(exc))
    finally:
        settings.agent_reach_bridge_url = old
    return {"platform": body.platform, "query": body.query, "count": len(items), "items": [asdict(x) for x in items]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the local Agent-Reach read-only bridge")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    loopback_hosts = {"127.0.0.1", "localhost", "::1"}
    if args.host not in loopback_hosts and not TOKEN:
        parser.error(
            "AGENT_REACH_BRIDGE_TOKEN is required when the bridge listens on a non-loopback host. "
            "Set a strong token before using --host 0.0.0.0 or another LAN-facing address."
        )
    uvicorn.run(app, host=args.host, port=args.port)
