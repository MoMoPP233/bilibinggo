"""V30：complete URL 契约（Frontend path ↔ Backend route 表）+ 负向 404 防护（离线）。

目的：防止 frontend 再次请求不存在的 /api/accounts/onboarding/{id}/complete。
只使用真实 FastAPI app/route 表与 TestClient；0 外部网络。
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from web.app import app

client = TestClient(app)

FRONTEND_SOURCE = (
    Path(__file__).resolve().parents[1]
    / "web"
    / "frontend"
    / "src"
    / "profiles"
    / "index.ts"
)
COMPLETE_PATH = "/api/accounts/onboarding/complete"
LEGACY_COMPLETE_PATH = "/api/accounts/onboarding/{onboarding_id}/complete"


def _onboarding_routes() -> set[tuple[frozenset[str], str]]:
    pairs: set[tuple[frozenset[str], str]] = set()
    for route in app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/api/accounts/onboarding"):
            continue
        methods = frozenset(getattr(route, "methods", []) or [])
        pairs.add((methods, path))
    return pairs


def test_complete_route_registered_exactly() -> None:
    assert (frozenset({"POST"}), COMPLETE_PATH) in _onboarding_routes()


def test_legacy_id_complete_route_is_not_registered() -> None:
    assert all(path != LEGACY_COMPLETE_PATH for _methods, path in _onboarding_routes())


def test_legacy_id_complete_request_returns_404_not_found() -> None:
    response = client.post(
        "/api/accounts/onboarding/o1/complete",
        json={"onboarding_id": "o1"},
    )
    assert response.status_code == 404
    payload = response.json()
    assert payload["error"]["code"] == "NOT_FOUND"
    assert payload["error"]["message"] == "Not Found"


def test_frontend_source_uses_exact_complete_path() -> None:
    source = FRONTEND_SOURCE.read_text(encoding="utf-8")
    assert f'"{COMPLETE_PATH}"' in source
    assert "/api/accounts/onboarding/${encodeURIComponent(onboardingId)}/complete" not in source


def test_complete_route_requires_onboarding_id_in_body() -> None:
    response = client.post(COMPLETE_PATH, json={})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
