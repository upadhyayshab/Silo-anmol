"""Manual fetch-trigger endpoints respond immediately instead of blocking on the
real scrape (which can take many minutes) - the underlying scrape/collector
functions are mocked out so these tests never hit real external APIs."""

import main
import youtube_service
import meta_service
import playstore_service
import news_service


def test_youtube_fetch_responds_immediately(client, auth_headers, monkeypatch):
    calls = []
    monkeypatch.setattr(youtube_service, "fetch_and_store_youtube_data", lambda keyword, db: calls.append(keyword))

    res = client.post("/api/fetch/youtube", params={"keyword": "test brand"}, headers=auth_headers)

    assert res.status_code == 200
    assert res.json()["status"] == "started"
    assert calls == ["test brand"]


def test_facebook_fetch_responds_immediately(client, auth_headers, monkeypatch):
    calls = []
    monkeypatch.setattr(meta_service, "fetch_facebook_comments", lambda db: calls.append(True))

    res = client.post("/api/fetch/facebook", headers=auth_headers)

    assert res.status_code == 200
    assert res.json()["status"] == "started"
    assert calls == [True]


def test_instagram_fetch_responds_immediately(client, auth_headers, monkeypatch):
    calls = []
    monkeypatch.setattr(meta_service, "fetch_instagram_comments", lambda db: calls.append(True))

    res = client.post("/api/fetch/instagram", headers=auth_headers)

    assert res.status_code == 200
    assert res.json()["status"] == "started"
    assert calls == [True]


def test_playstore_fetch_responds_immediately(client, auth_headers, monkeypatch):
    calls = []
    monkeypatch.setattr(playstore_service, "fetch_playstore_reviews", lambda pkg, db: calls.append(pkg))
    monkeypatch.setenv("PLAYSTORE_PACKAGE_NAMES", "com.example.app1,com.example.app2")

    res = client.post("/api/fetch/playstore", headers=auth_headers)

    assert res.status_code == 200
    assert res.json()["status"] == "started"
    assert calls == ["com.example.app1", "com.example.app2"]


def test_news_fetch_responds_immediately(client, auth_headers, monkeypatch):
    calls = []
    monkeypatch.setattr(news_service, "fetch_google_news", lambda kw, db: calls.append(kw))
    monkeypatch.setenv("TARGET_KEYWORDS", "Brand One,Brand Two")

    res = client.post("/api/fetch/news", headers=auth_headers)

    assert res.status_code == 200
    assert res.json()["status"] == "started"
    assert calls == ["Brand One", "Brand Two"]


def test_fetch_all_responds_immediately(client, auth_headers, monkeypatch):
    calls = []
    monkeypatch.setattr(main, "run_automation_cycle", lambda: calls.append(True))

    res = client.post("/api/fetch/all", headers=auth_headers)

    assert res.status_code == 200
    assert res.json()["status"] == "started"
    assert calls == [True]


def test_fetch_endpoints_require_auth(client):
    res = client.post("/api/fetch/facebook")
    assert res.status_code == 401
