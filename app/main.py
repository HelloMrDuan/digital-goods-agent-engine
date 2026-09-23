from __future__ import annotations

import secrets

# Keep the validated V1.2 commercial-validation application intact and layer the
# Opportunity Card / rebuild APIs on top. Splitting the routes keeps the next
# iteration small and makes rollback straightforward.
from fastapi.responses import JSONResponse

from . import main_legacy as _legacy
from .main_legacy import *  # noqa: F401,F403
from .main_legacy import app
from .routes_v12 import router as opportunity_v12_router


# Compatibility shims for tests and local scripts that intentionally monkeypatch
# helpers on app.main. The original functions live in main_legacy, so copy the
# patched dependencies into that module before delegating.
def _collect_source_batch(sources, scope, force=False):
    _legacy.collect_source = collect_source
    _legacy.build_from_unlinked_evidence = build_from_unlinked_evidence
    return _legacy._collect_source_batch(sources, scope, force=force)


def _activate_p0_from_real_login():
    _legacy.agent_reach_doctor = agent_reach_doctor
    _legacy.list_sources = list_sources
    return _legacy._activate_p0_from_real_login()


def _real_refresh_readiness() -> tuple[bool, str | None]:
    """Require a real connected OpenCLI Browser Bridge before refresh=true rebuilds."""
    doctor = agent_reach_doctor()
    opencli = doctor.get("opencli") if isinstance(doctor, dict) else None
    if not isinstance(opencli, dict) or not bool(opencli.get("connected")):
        return False, "OpenCLI Browser Bridge is not connected; connect the browser extension before refresh=true rebuild"
    return True, None


def _is_loopback_client(host: str | None) -> bool:
    value = str(host or "").strip().lower()
    return value in {"127.0.0.1", "::1", "localhost", "testclient"} or value.startswith("::ffff:127.")


def _write_auth_error(
    method: str,
    path: str,
    client_host: str | None,
    authorization: str | None,
    api_key: str | None,
) -> tuple[int, str] | None:
    if method.upper() not in {"POST", "PUT", "PATCH", "DELETE"} or not path.startswith("/api/"):
        return None
    expected = str(settings.api_auth_token or "").strip()
    if not expected:
        if _is_loopback_client(client_host):
            return None
        return 403, "remote API writes are disabled until API_AUTH_TOKEN is configured"

    bearer = ""
    raw_auth = str(authorization or "").strip()
    if raw_auth.lower().startswith("bearer "):
        bearer = raw_auth[7:].strip()
    supplied = str(api_key or "").strip() or bearer
    if not supplied or not secrets.compare_digest(supplied, expected):
        return 401, "valid API token required for mutating API requests"
    return None


@app.middleware("http")
async def _security_guards(request, call_next):
    client_host = request.client.host if request.client else None
    auth_error = _write_auth_error(
        request.method,
        request.url.path,
        client_host,
        request.headers.get("Authorization"),
        request.headers.get("X-API-Key"),
    )
    if auth_error:
        status, detail = auth_error
        return JSONResponse(status_code=status, content={"detail": detail})

    if request.method.upper() == "POST" and request.url.path == "/api/opportunities/rebuild":
        refresh = str(request.query_params.get("refresh", "true")).strip().lower() not in {"0", "false", "no", "off"}
        if refresh:
            ready, reason = _real_refresh_readiness()
            if not ready:
                return JSONResponse(status_code=409, content={"detail": reason})
    response = await call_next(request)

    # Windows PowerShell 5.1 may decode application/json without an explicit
    # charset using a legacy code page, producing mojibake for Chinese text.
    # FastAPI emits UTF-8 JSON, so advertise that encoding explicitly.
    content_type = str(response.headers.get("content-type") or "")
    if content_type.lower().startswith("application/json") and "charset=" not in content_type.lower():
        response.headers["content-type"] = "application/json; charset=utf-8"
    return response


app.include_router(opportunity_v12_router)
app.version = "1.4.0-xianyu-market-publish"
