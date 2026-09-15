from __future__ import annotations

# Keep the validated V1.2 commercial-validation application intact and layer the
# Opportunity Card / rebuild APIs on top. Splitting the routes keeps the next
# iteration small and makes rollback straightforward.
from .main_legacy import *  # noqa: F401,F403
from .main_legacy import app
from .routes_v12 import router as opportunity_v12_router

app.include_router(opportunity_v12_router)
app.version = "1.2.1-opportunity-agent"
