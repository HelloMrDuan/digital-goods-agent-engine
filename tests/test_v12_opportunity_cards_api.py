from __future__ import annotations

from fastapi.routing import APIRoute

from app.main import app


def _paths() -> set[tuple[str, str]]:
    return {
        (route.path, method)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }


def test_opportunity_card_routes_are_exposed():
    paths = _paths()
    assert ("/api/opportunity-cards", "GET") in paths
    assert ("/api/opportunity-cards/{card_id}", "GET") in paths
    assert ("/api/opportunity-cards/by-opportunity/{opportunity_id}", "GET") in paths
    assert ("/api/opportunities/{opportunity_id}/agent-runs", "GET") in paths


def test_rebuild_routes_are_exposed():
    paths = _paths()
    assert ("/api/opportunities/rebuild", "POST") in paths
    assert ("/api/opportunities/rebuild/tasks/{task_id}", "GET") in paths


def test_main_reports_opportunity_agent_version():
    assert app.version == "1.2.1-opportunity-agent"
