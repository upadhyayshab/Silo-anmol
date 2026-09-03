"""Login flow and protected-route enforcement."""

# Every route that mutates or reads dashboard data, or triggers a fetch.
PROTECTED_ROUTES = [
    ("GET", "/api/feed"),
    ("GET", "/api/mentions"),
    ("GET", "/api/mentions/recent"),
    ("GET", "/api/analytics/kpis"),
    ("GET", "/api/analytics/sentiment-trend"),
    ("POST", "/api/fetch/facebook"),
    ("POST", "/api/fetch/instagram"),
    ("POST", "/api/fetch/playstore"),
    ("POST", "/api/fetch/news"),
    ("POST", "/api/fetch/all"),
]


def test_login_with_correct_password_returns_token(client):
    res = client.post("/api/login", json={"password": "changeme"})
    assert res.status_code == 200
    assert "token" in res.json()
    assert len(res.json()["token"]) > 20


def test_login_with_wrong_password_is_rejected(client):
    res = client.post("/api/login", json={"password": "definitely-wrong"})
    assert res.status_code == 401


def test_protected_routes_reject_missing_token(client):
    for method, path in PROTECTED_ROUTES:
        res = client.request(method, path)
        assert res.status_code == 401, f"{method} {path} should require auth"


def test_protected_routes_reject_garbage_token(client):
    headers = {"Authorization": "Bearer this.is.not.a.real.token"}
    for method, path in PROTECTED_ROUTES:
        res = client.request(method, path, headers=headers)
        assert res.status_code == 401, f"{method} {path} should reject a bad token"


def test_protected_route_accepts_valid_token(client, auth_headers):
    res = client.get("/api/mentions", headers=auth_headers)
    assert res.status_code == 200


def test_valid_request_returns_a_renewed_token(client, auth_headers):
    """Sliding expiration: every authenticated request should hand back a fresh token."""
    res = client.get("/api/mentions", headers=auth_headers)
    assert "X-New-Token" in res.headers
    assert len(res.headers["X-New-Token"]) > 20


def test_webhook_route_does_not_require_dashboard_auth(client):
    """The Meta webhook is called by Meta's servers, not a logged-in browser - it
    should never be blocked by our "Not authenticated" check (it has its own
    separate verify-token mechanism instead)."""
    res = client.get("/webhook", params={
        "hub.mode": "subscribe",
        "hub.verify_token": "wrong-token",
        "hub.challenge": "12345",
    })
    # Rejected for the wrong verify token (403), NOT for missing dashboard auth (401).
    assert res.status_code != 401
