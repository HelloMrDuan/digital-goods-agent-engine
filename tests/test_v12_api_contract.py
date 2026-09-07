from __future__ import annotations

from fastapi.routing import APIRoute

from app.main import app


def test_opportunity_analysis_route_is_registered_once():
    matches = [
        route
        for route in app.routes
        if isinstance(route, APIRoute)
        and route.path == "/api/opportunities/{opportunity_id}/analyze"
        and "POST" in route.methods
    ]
    assert len(matches) == 1


def test_v12_validation_routes_are_exposed():
    paths = {
        (route.path, method)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }
    assert ("/api/opportunities/{opportunity_id}/validation", "GET") in paths
    assert ("/api/opportunities/{opportunity_id}/validation", "POST") in paths
    assert ("/api/opportunities/batch/validate", "POST") in paths


def test_frontend_bundle_exists():
    from app.config import BASE_DIR

    bundle = BASE_DIR / "web" / "app.js"
    assert bundle.is_file()
    assert bundle.stat().st_size > 1000
