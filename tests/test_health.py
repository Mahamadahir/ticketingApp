from sqlalchemy.exc import OperationalError

from app.database import get_session
from app.main import app


def test_health_reports_ok_without_signing_in(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


def test_health_returns_503_when_database_is_unreachable(client):
    class UnreachableSession:
        def exec(self, statement):
            raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    app.dependency_overrides[get_session] = lambda: UnreachableSession()
    try:
        response = client.get("/health")
    finally:
        app.dependency_overrides.pop(get_session)

    assert response.status_code == 503
    assert response.json()["database"] == "unreachable"
