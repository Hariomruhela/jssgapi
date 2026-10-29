import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from app.main import app, parse_integrity_error
from app.core.exceptions import (
    AlreadyExistsException,
    BadRequestException,
    ForbiddenException,
    NotFoundException,
    UnauthorizedException,
)

test_router = APIRouter(prefix="/test-errors")

@test_router.post("/raise-integrity-phone")
async def raise_integrity_phone():
    raise IntegrityError(
        statement="INSERT INTO users (phone_number) VALUES ('9876543210')",
        params={"phone_number": "9876543210"},
        orig=Exception('duplicate key value violates unique constraint "users_phone_number_key"\nDETAIL: Key (phone_number)=(+919876543210) already exists.'),
    )

@test_router.post("/raise-integrity-email")
async def raise_integrity_email():
    raise IntegrityError(
        statement="INSERT INTO users (email) VALUES ('test@example.com')",
        params={"email": "test@example.com"},
        orig=Exception('duplicate key value violates unique constraint "users_email_key"\nDETAIL: Key (email)=(test@example.com) already exists.'),
    )

@test_router.post("/raise-integrity-generic")
async def raise_integrity_generic():
    raise IntegrityError(
        statement="INSERT INTO sensitive_table (secret) VALUES ('123')",
        params={"secret": "123"},
        orig=Exception('duplicate key value violates unique constraint "uq_secret"'),
    )

@test_router.post("/raise-already-exists-phone")
async def raise_already_exists_phone():
    raise AlreadyExistsException("An account with this phone number already exists")

@test_router.post("/raise-already-exists-email")
async def raise_already_exists_email():
    raise AlreadyExistsException("An account with this email already exists")

@test_router.get("/raise-400")
async def raise_400():
    raise BadRequestException("Invalid input data provided")

@test_router.get("/raise-401")
async def raise_401():
    raise UnauthorizedException("Invalid credentials")

@test_router.get("/raise-403")
async def raise_403():
    raise ForbiddenException("Permission denied")

@test_router.get("/raise-404")
async def raise_404():
    raise NotFoundException("User", "123")


@pytest.fixture(autouse=True)
def _mock_bootstrap(monkeypatch):
    async def _noop(*args, **kwargs):
        pass
    monkeypatch.setattr("app.main.bootstrap_app", _noop)

@pytest.fixture
def client(monkeypatch):
    async def _noop(*args, **kwargs):
        pass
    monkeypatch.setattr("app.main.bootstrap_app", _noop)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def test_parse_integrity_error_phone():
    exc = IntegrityError(
        statement="INSERT INTO users (phone_number) VALUES ('9876543210')",
        params={},
        orig=Exception("Key (phone_number)=(9876543210) already exists."),
    )
    msg = parse_integrity_error(exc)
    assert msg == "This phone number is already registered."
    assert "INSERT INTO" not in msg
    assert "users" not in msg


def test_parse_integrity_error_email():
    exc = IntegrityError(
        statement="INSERT INTO users (email) VALUES ('test@example.com')",
        params={},
        orig=Exception("Key (email)=(test@example.com) already exists."),
    )
    msg = parse_integrity_error(exc)
    assert msg == "This email is already registered."
    assert "INSERT INTO" not in msg


def test_parse_integrity_error_generic():
    exc = IntegrityError(
        statement="INSERT INTO secret_table (data) VALUES ('abc')",
        params={},
        orig=Exception("duplicate key value violates unique constraint"),
    )
    msg = parse_integrity_error(exc)
    assert msg == "A record with this information already exists."
    assert "secret_table" not in msg


def test_endpoint_catches_integrity_phone(client):
    response = client.post("/test-errors/raise-integrity-phone")
    assert response.status_code == 409
    data = response.json()
    assert data["success"] is False
    assert data["message"] == "This phone number is already registered."
    assert "INSERT INTO" not in response.text
    assert "stack" not in response.text.lower()


def test_endpoint_catches_integrity_email(client):
    response = client.post("/test-errors/raise-integrity-email")
    assert response.status_code == 409
    data = response.json()
    assert data["success"] is False
    assert data["message"] == "This email is already registered."
    assert "INSERT INTO" not in response.text


def test_endpoint_catches_integrity_generic(client):
    response = client.post("/test-errors/raise-integrity-generic")
    assert response.status_code == 409
    data = response.json()
    assert data["success"] is False
    assert data["message"] == "A record with this information already exists."
    assert "sensitive_table" not in response.text


def test_endpoint_normalizes_already_exists_phone(client):
    response = client.post("/test-errors/raise-already-exists-phone")
    assert response.status_code == 409
    data = response.json()
    assert data["message"] == "This phone number is already registered."


def test_endpoint_normalizes_already_exists_email(client):
    response = client.post("/test-errors/raise-already-exists-email")
    assert response.status_code == 409
    data = response.json()
    assert data["message"] == "This email is already registered."


def test_endpoint_status_codes(client):
    r400 = client.get("/test-errors/raise-400")
    assert r400.status_code == 400
    assert r400.json()["message"] == "Invalid input data provided"

    r401 = client.get("/test-errors/raise-401")
    assert r401.status_code == 401
    assert r401.json()["message"] == "Invalid credentials"

    r403 = client.get("/test-errors/raise-403")
    assert r403.status_code == 403
    assert r403.json()["message"] == "Permission denied"

    r404 = client.get("/test-errors/raise-404")
    assert r404.status_code == 404
    assert r404.json()["message"] == "User with id '123' not found"


def test_validation_error_formatting(client):
    # Calling an endpoint with invalid JSON body
    response = client.post("/api/v1/auth/register", json={})
    assert response.status_code == 422
    data = response.json()
    assert data["success"] is False
    assert "detail" in data
    assert isinstance(data["detail"], list)
    assert isinstance(data["message"], str)
    assert len(data["message"]) > 0


if __name__ == "__main__":
    async def _noop(*args, **kwargs):
        pass
    import app.main as app_main
    app_main.bootstrap_app = _noop
    fastapi_app = app_main.app
    fastapi_app.include_router(test_router)

    test_parse_integrity_error_phone()
    test_parse_integrity_error_email()
    test_parse_integrity_error_generic()
    with TestClient(fastapi_app, raise_server_exceptions=False) as c:
        test_endpoint_catches_integrity_phone(c)
        test_endpoint_catches_integrity_email(c)
        test_endpoint_catches_integrity_generic(c)
        test_endpoint_normalizes_already_exists_phone(c)
        test_endpoint_normalizes_already_exists_email(c)
        test_endpoint_status_codes(c)
        test_validation_error_formatting(c)
    print("ALL TESTS PASSED SUCCESSFULLY!")
