import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from app import app

client = TestClient(app)


def test_root_returns_200():
    response = client.get("/")
    assert response.status_code == 200


def test_security_page():
    response = client.get("/security")
    assert response.status_code == 200


def test_auth_register_get():
    response = client.get("/auth?mode=register")
    assert response.status_code == 200


def test_auth_login_get():
    response = client.get("/auth?mode=login")
    assert response.status_code == 200
