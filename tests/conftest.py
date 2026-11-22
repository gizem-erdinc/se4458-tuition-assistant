"""Test fixtures.

Tests run against a real PostgreSQL server (no cloud, no LLM needed):
    docker compose up -d db          # or any local PostgreSQL
    pytest -q
TEST_DATABASE_URL points at a server where the tests may create a throw-away
database (default postgresql://postgres:postgres@localhost:5432/postgres).
A fresh database named tuition_test_<pid> is created per run and dropped after.
"""
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ADMIN_URL = os.environ.get("TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/postgres")
TEST_DB = f"tuition_test_{os.getpid()}"
TEST_URL = ADMIN_URL.rsplit("/", 1)[0] + "/" + TEST_DB
os.environ["DATABASE_URL"] = TEST_URL          # must be set before the app modules create pools

from gateway.main import PostgresDailyRateLimiter, create_app  # noqa: E402
from tuition_api import db  # noqa: E402
from tuition_api.main import app as tuition_app  # noqa: E402
from tuition_api.seed import seed  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def test_database():
    try:
        admin = psycopg.connect(ADMIN_URL, autocommit=True)
    except psycopg.OperationalError as e:
        pytest.exit(f"PostgreSQL not reachable at {ADMIN_URL} — start it with `docker compose up -d db` ({e})", 2)
    admin.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}"')
    admin.execute(f'CREATE DATABASE "{TEST_DB}"')
    db.init_db()
    yield
    db.close_pool()
    admin.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)')
    admin.close()


@pytest.fixture(autouse=True)
def fresh_data():
    seed()
    yield


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def api():
    with TestClient(tuition_app) as c:
        yield c


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 22, 10, 0, tzinfo=ZoneInfo("Europe/Istanbul"))

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def gateway_app(clock):
    return create_app(transport=httpx.ASGITransport(app=tuition_app),
                      limiter=PostgresDailyRateLimiter(TEST_URL, clock))


@pytest.fixture
def gw(gateway_app):
    with TestClient(gateway_app) as c:
        yield c


def token(client, username, password):
    r = client.post("/api/v1/auth/token", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['accessToken']}"}
