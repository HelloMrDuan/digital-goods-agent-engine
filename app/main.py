from __future__ import annotations

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


@app.middleware("http")
async def _guard_real_refresh(request, call_next):
    if request.method.upper() == "POST" and request.url.path == "/api/opportunities/rebuild":
        refresh = str(request.query_params.get("refresh", "true")).strip().lower() not in {"0", "false", "no", "off"}
        if refresh:
            ready, reason = _real_refresh_readiness()
            if not ready:
                return JSONResponse(status_code=409, content={"detail": reason})
    return await call_next(request)


app.include_router(opportunity_v12_router)
app.version = "1.2.3-asset-license-registry"
