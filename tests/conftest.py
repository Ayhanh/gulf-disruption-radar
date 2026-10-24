import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["DATABASE_URL"] = ""
os.environ["RAPIDAPI_KEY"] = "test_key"
os.environ["TELEGRAM_BOT_TOKEN"] = "test_token"
os.environ["ADMIN_EMAIL"] = "admin@test.com"
os.environ["APP_BASE_URL"] = "http://localhost:8000"
os.environ["SMTP_PASSWORD"] = ""

import pytest
from fastapi.testclient import TestClient

from app import app


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def db():
    from database import get_db
    conn = get_db()
    yield conn
    conn.close()


def test_index(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Gulf Disruption Radar" in response.text


def test_auth_page(client):
    response = client.get("/auth?mode=login")
    assert response.status_code == 200


def test_health_admin_requires_auth(client):
    response = client.get("/admin")
    assert response.status_code == 404
