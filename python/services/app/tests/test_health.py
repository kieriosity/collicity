from fastapi.testclient import TestClient
from pydantic import SecretStr

from collicity_app.main import SECURITY_HEADERS, create_app
from collicity_app.settings import AppSettings
from collicity_db import Role
from collicity_testing.postgres import MigratedDatabase


def _client(database_url: str) -> TestClient:
    return TestClient(create_app(AppSettings(database_url=SecretStr(database_url))))


def test_healthz_needs_no_database():
    with _client("postgresql+psycopg://nobody:x@127.0.0.1:1/none") as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_is_ok_when_app_user_can_reach_the_database(database: MigratedDatabase):
    with _client(database.urls[Role.APP_USER]) as client:
        response = client.get("/readyz")
    assert response.status_code == 200


def test_readyz_is_unavailable_when_the_database_is_down():
    with _client("postgresql+psycopg://nobody:x@127.0.0.1:1/none") as client:
        response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}


def test_every_response_carries_the_api_security_headers():
    with _client("postgresql+psycopg://nobody:x@127.0.0.1:1/none") as client:
        for path in ("/healthz", "/readyz", "/no-such-route"):
            response = client.get(path)
            for header, value in SECURITY_HEADERS.items():
                assert response.headers[header] == value, (path, header)


def test_api_docs_are_served_only_locally():
    url = "postgresql+psycopg://nobody:x@127.0.0.1:1/none"
    local = create_app(AppSettings(database_url=SecretStr(url), environment="local"))
    prod = create_app(AppSettings(database_url=SecretStr(url), environment="prod"))
    with TestClient(local) as client:
        docs = client.get("/docs")
        assert docs.status_code == 200
        assert "Content-Security-Policy" not in docs.headers
    with TestClient(prod) as client:
        docs = client.get("/docs")
        assert docs.status_code == 404
        assert (
            docs.headers["Content-Security-Policy"] == SECURITY_HEADERS["Content-Security-Policy"]
        )
