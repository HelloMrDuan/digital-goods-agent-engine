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

def test_gate_a_gate_b_and_fulfillment_routes_are_exposed():
    paths = {
        (route.path, method)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }
    assert ("/api/opportunities/{opportunity_id}/gate-a", "GET") in paths
    assert ("/api/opportunities/{opportunity_id}/gate-a", "POST") in paths
    assert ("/api/products/{product_id}", "GET") in paths
    assert ("/api/products/{product_id}/gate-b", "GET") in paths
    assert ("/api/products/{product_id}/gate-b", "POST") in paths
    assert ("/api/products/{product_id}/approve", "POST") in paths
    assert ("/api/deliveries", "GET") in paths
    assert ("/api/deliveries", "POST") in paths
    assert ("/api/source-assets", "GET") in paths
    assert ("/api/source-assets", "POST") in paths
    assert ("/api/source-assets/{asset_id}", "GET") in paths
    assert ("/api/source-assets/{asset_id}", "PATCH") in paths
    assert ("/api/products/{product_id}/assets", "GET") in paths
    assert ("/api/products/{product_id}/assets/{asset_id}", "POST") in paths
    assert ("/api/products/{product_id}/assets/{asset_id}", "DELETE") in paths
    assert ("/api/products/{product_id}/license-readiness", "GET") in paths



def test_market_intelligence_routes_are_exposed():
    paths = {
        (route.path, method)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }
    assert ("/api/market-intel", "GET") in paths
    assert ("/api/market-intel/summary", "GET") in paths
    assert ("/api/market-intel/collect", "POST") in paths
    assert ("/api/market-intel/rematch", "POST") in paths



def test_real_build_and_etsy_publish_routes_are_exposed():
    paths = {
        (route.path, method)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }
    assert ("/api/products/{product_id}/build-artifact", "POST") in paths
    assert ("/api/products/{product_id}/builds", "GET") in paths
    assert ("/api/products/{product_id}/artifact/download", "GET") in paths
    assert ("/api/publish/etsy/status", "GET") in paths
    assert ("/api/products/{product_id}/publish/etsy", "POST") in paths
    assert ("/api/products/{product_id}/publish-runs", "GET") in paths
