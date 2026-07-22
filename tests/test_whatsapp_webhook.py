import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import patch

# Set up path so app is importable when running this file directly
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import path_setup
from routers.v1.whatsapp import router

@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)

def test_webhook_verification(client, monkeypatch):
    """Test webhook verification challenge."""
    from routers.v1.whatsapp import settings
    monkeypatch.setattr(settings, "aisensy_webhook_secret", "testsecret")
    
    # Missing/invalid token -> 403
    resp = client.get("/webhooks/whatsapp?token=invalid")
    assert resp.status_code == 403
    
    # Valid token -> 200 Verified
    resp = client.get("/webhooks/whatsapp?token=testsecret")
    assert resp.status_code == 200
    assert resp.text == "Verified"

@patch("services.aisensy_leads.ingest_webhook")
def test_webhook_post_dispatch(mock_ingest, client, monkeypatch):
    """Test webhook post payload dispatch to background task."""
    from routers.v1.whatsapp import settings
    monkeypatch.setattr(settings, "aisensy_webhook_secret", "testsecret")
    
    payload = {
        "topic": "message.created",
        "resource": {
            "message": {"sender": "user", "type": "text", "text": {"body": "Hi"}},
            "contact": {"phone": "919876543210", "name": "John"}
        }
    }
    
    # Valid
    resp = client.post("/webhooks/whatsapp?token=testsecret", json=payload)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
