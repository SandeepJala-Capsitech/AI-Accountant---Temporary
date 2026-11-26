"""The API with sign-in on: every /api route but health and sign-in needs the session cookie. No MongoDB needed."""
import pytest
from fakes import FakeModel
from fastapi.testclient import TestClient

from ledgersync.config import Settings
from server import create_app

ON = dict(auth_username="admin", auth_password="s3cret", auth_secret="k" * 32)
RIGHT = {"username": "admin", "password": "s3cret"}
WRONG = {"username": "admin", "password": "nope"}


class StandInStore:
    """Enough of a MongoStore for routes that never read it."""
    db = None

    def close(self):
        pass


def client_for(settings: Settings, headers=None) -> TestClient:
    app = create_app(settings, model_client=FakeModel(), store=StandInStore(), blobs=object())
    return TestClient(app, headers={"X-LedgerSync": "1"} if headers is None else headers)


@pytest.fixture
def api():
    with client_for(Settings(warmup=False, **ON)) as client:
        yield client


def test_without_a_cookie_every_api_route_is_401(api):
    for res in (api.get("/api/accounts"), api.post("/api/analyze", data={"text": "x"})):
        assert res.status_code == 401
        assert res.json()["detail"] == {"code": "not_signed_in", "message": "Sign in to use Super Accountant."}


def test_health_and_session_stay_public(api):
    assert api.get("/api/health").status_code == 200
    assert api.get("/api/auth/session").json() == {"auth_required": True, "signed_in": False}


def test_signing_in_opens_the_api(api):
    assert api.post("/api/auth/login", json=RIGHT).status_code == 204
    assert api.get("/api/accounts").status_code == 200
    assert api.get("/api/auth/session").json() == {"auth_required": True, "signed_in": True}


def test_the_cookie_is_httponly_lax_and_only_secure_behind_https(api):
    plain = api.post("/api/auth/login", json=RIGHT).headers["set-cookie"]
    assert "ledgersync_session=" in plain and "HttpOnly" in plain and "max-age=604800" in plain.lower()
    assert "samesite=lax" in plain.lower() and "Secure" not in plain and "domain=" not in plain.lower()
    behind_https = api.post("/api/auth/login", json=RIGHT, headers={"X-Forwarded-Proto": "https"})
    assert "Secure" in behind_https.headers["set-cookie"]


def test_a_wrong_password_is_401_wrong_credentials(api):
    res = api.post("/api/auth/login", json=WRONG)
    assert res.status_code == 401
    assert res.json()["detail"] == {"code": "wrong_credentials", "message": "That username and password don't match."}
    assert "set-cookie" not in res.headers


def test_the_eleventh_attempt_is_throttled(api):
    for _ in range(10):
        assert api.post("/api/auth/login", json=WRONG).status_code == 401
    res = api.post("/api/auth/login", json=RIGHT)
    assert res.status_code == 429
    assert res.json()["detail"] == {"code": "too_many_attempts",
                                    "message": "Too many wrong attempts. Wait 15 minutes and try again."}


def test_login_needs_the_pages_header():
    with client_for(Settings(warmup=False, **ON), headers={}) as client:
        res = client.post("/api/auth/login", json=RIGHT)
    assert res.status_code == 403
    assert res.json()["detail"]["code"] == "refused_request"


def test_signing_out_closes_the_api(api):
    api.post("/api/auth/login", json=RIGHT)
    assert api.post("/api/auth/logout").status_code == 204
    assert api.get("/api/accounts").status_code == 401


def test_with_sign_in_off_nothing_changes():
    with client_for(Settings(warmup=False)) as client:
        assert client.get("/api/accounts").status_code == 200
        assert client.get("/api/auth/session").json() == {"auth_required": False, "signed_in": True}


def test_half_set_credentials_refuse_to_start():
    with pytest.raises(ValueError, match="LEDGERSYNC_AUTH_PASSWORD"):
        create_app(Settings(warmup=False, auth_username="admin"), model_client=FakeModel(), store=StandInStore(),
                   blobs=object())
