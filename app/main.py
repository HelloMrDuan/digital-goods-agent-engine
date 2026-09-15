from __future__ import annotations

# Keep the validated V1.2 commercial-validation application intact and layer the
# Opportunity Card / rebuild APIs on top. Splitting the routes keeps the next
# iteration small and makes rollback straightforward.
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


app.include_router(opportunity_v12_router)
app.version = "1.2.1-opportunity-agent"
