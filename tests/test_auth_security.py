"""Authentication hardening: no secret fallbacks, issuer enforced, no fallback to another account."""
import datetime
import os
import subprocess
import sys
import uuid

import jwt
import pytest

from tests.conftest import ROOT

PROTECTED = "/api/me/permissions"


def _token(**overrides) -> str:
    now = datetime.datetime.now(datetime.UTC)
    claims = {
        "sub": str(uuid.uuid4()),
        "iss": os.environ.get("JWT_ISSUER", "marketing-portal"),
        "iat": now,
        "exp": now + datetime.timedelta(minutes=5),
    }
    claims.update(overrides)
    secret = claims.pop("_secret", os.environ["JWT_SECRET"])
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, secret, algorithm="HS256")


def _get(client, token):
    return client.get(PROTECTED, headers={"Authorization": f"Bearer {token}"})


def test_valid_token_is_accepted(client, admin):
    assert client.get(PROTECTED, headers=admin["headers"]).status_code == 200


def test_service_token_is_not_treated_as_a_user(client, admin):
    # Previously fell back to the first super admin
    token = _token(sub="service:admin", is_service_token=True, service="admin", scopes=["*"])
    assert _get(client, token).status_code == 401


def test_token_for_unknown_user_is_rejected(client, admin):
    # Previously fell back to the first super admin / first active user
    assert _get(client, _token(is_service_token=True)).status_code == 401


def test_email_claim_does_not_select_a_user(client, admin):
    email = client.get(PROTECTED, headers=admin["headers"]).json()["user"]["email"]
    assert _get(client, _token(email=email)).status_code == 401


@pytest.mark.parametrize("issuer", ["zenatech-internal-portal", "someone-else", None])
def test_wrong_or_missing_issuer_is_rejected(client, admin, issuer):
    token = _token(sub=str(admin["id"]), iss=issuer)
    assert _get(client, token).status_code == 401


def test_token_signed_with_other_secret_is_rejected(client, admin):
    # The secret that used to be hard-coded in service_security_policy.py
    token = _token(sub=str(admin["id"]), _secret="OU2YW8HGoJJMb7+aAVjoxRXah2gSUtvPLPlzK8G6j9c=")
    assert _get(client, token).status_code == 401


def test_dev_login_unknown_email_does_not_sign_in(client):
    response = client.get("/api/auth/dev-login", params={"email": "nobody@zenatech.com"}, follow_redirects=False)
    assert response.status_code in (302, 307)
    assert "account_not_found" in response.headers["location"]
    assert "token=" not in response.headers["location"]


@pytest.mark.parametrize("app_env", ["production", "staging", ""])
def test_dev_login_only_exists_in_development(client, monkeypatch, app_env):
    monkeypatch.setenv("APP_ENV", app_env)
    assert client.post("/api/auth/dev-login", json={"email": "x@zenatech.com"}).status_code == 404
    assert client.get("/api/auth/dev-login", params={"email": "x@zenatech.com"}, follow_redirects=False).status_code == 404


# --- Startup checks: run in a fresh interpreter, no database needed ---

def _import_with_env(module: str, **env) -> subprocess.CompletedProcess:
    environment = {**os.environ, **env}
    # Stop .env files from re-supplying the secrets under test
    code = "import dotenv; dotenv.load_dotenv = lambda *a, **k: False; import " + module
    return subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=environment, capture_output=True, text=True, timeout=120, check=False)


@pytest.mark.parametrize("secret", ["", "too-short"])
def test_app_refuses_to_start_without_jwt_secret(secret):
    result = _import_with_env("services.auth_service", JWT_SECRET=secret, SESSION_SECRET="s" * 40)
    assert result.returncode != 0
    assert "JWT_SECRET must be configured" in result.stderr


def test_app_refuses_to_start_without_session_secret():
    result = _import_with_env("server", JWT_SECRET="j" * 40, SESSION_SECRET="")
    assert result.returncode != 0
    assert "SESSION_SECRET must be configured" in result.stderr
