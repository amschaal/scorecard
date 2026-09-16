"""CAS flow with the CAS server's validate endpoint stubbed by `responses` (python-cas uses requests)."""

from urllib.parse import parse_qs, urlparse

import pytest
import responses

VALIDATE_URL = "https://cas.ucdavis.edu/cas/p3/serviceValidate"
CAS_OK = """<cas:serviceResponse xmlns:cas='http://www.yale.edu/tp/cas'>
  <cas:authenticationSuccess><cas:user>amschaal</cas:user>
    <cas:attributes><cas:displayName>Adam</cas:displayName></cas:attributes>
  </cas:authenticationSuccess></cas:serviceResponse>"""
CAS_FAIL = """<cas:serviceResponse xmlns:cas='http://www.yale.edu/tp/cas'>
  <cas:authenticationFailure code='INVALID_TICKET'>Ticket ST-x not recognized</cas:authenticationFailure>
</cas:serviceResponse>"""


@pytest.fixture()
def cas_client(clean_db, settings_env, monkeypatch):
    from fastapi.testclient import TestClient

    from hpcusage.main import create_app

    monkeypatch.setattr(settings_env, "auth_mode", "cas")
    monkeypatch.setattr(settings_env, "allowed_users", "")
    with TestClient(create_app()) as c:
        yield c


def test_unauthenticated_page_redirects_to_login(cas_client):
    r = cas_client.get("/c/hive?from=2026-01-01", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].startswith("/auth/login?next=/c/hive")


def test_unauthenticated_api_is_401(cas_client):
    assert cas_client.get("/api/v1/clusters").status_code == 401


def test_login_redirects_to_cas(cas_client):
    r = cas_client.get("/auth/login", params={"next": "/c/hive"}, follow_redirects=False)
    assert r.status_code == 302
    loc = urlparse(r.headers["location"])
    assert f"{loc.scheme}://{loc.netloc}{loc.path}" == "https://cas.ucdavis.edu/cas/login"
    assert parse_qs(loc.query)["service"] == ["http://testserver/auth/callback?next=%2Fc%2Fhive"]


def test_open_redirect_is_blocked(cas_client):
    r = cas_client.get("/auth/login", params={"next": "https://evil.example"}, follow_redirects=False)
    service = parse_qs(urlparse(r.headers["location"]).query)["service"][0]
    assert service.endswith("/auth/callback?next=%2F")


@responses.activate
def test_callback_success_sets_session(cas_client):
    responses.get(VALIDATE_URL, body=CAS_OK, content_type="text/xml")
    r = cas_client.get("/auth/callback", params={"ticket": "ST-123", "next": "/c/hive"}, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/c/hive"
    assert len(responses.calls) == 1
    sent = parse_qs(urlparse(responses.calls[0].request.url).query)
    assert sent["ticket"] == ["ST-123"]
    assert sent["service"] == ["http://testserver/auth/callback?next=%2Fc%2Fhive"]  # identical to the login one
    # session cookie now grants API access
    assert cas_client.get("/api/v1/clusters").status_code == 200


@responses.activate
def test_callback_failure(cas_client):
    responses.get(VALIDATE_URL, body=CAS_FAIL, content_type="text/xml")
    r = cas_client.get("/auth/callback", params={"ticket": "ST-bad"}, follow_redirects=False)
    assert r.status_code == 401
    assert cas_client.get("/api/v1/clusters").status_code == 401


@responses.activate
def test_cas_unreachable_is_502(cas_client):
    responses.get(VALIDATE_URL, body=responses.ConnectionError("boom"))
    r = cas_client.get("/auth/callback", params={"ticket": "ST-1"}, follow_redirects=False)
    assert r.status_code == 502


@responses.activate
def test_allowlist(cas_client, settings_env, monkeypatch):
    monkeypatch.setattr(settings_env, "allowed_users", "someoneelse")
    responses.get(VALIDATE_URL, body=CAS_OK, content_type="text/xml")
    r = cas_client.get("/auth/callback", params={"ticket": "ST-1"}, follow_redirects=False)
    assert r.status_code == 403


def test_logout(cas_client):
    r = cas_client.get("/auth/logout", follow_redirects=False)
    assert r.status_code == 302
    loc = urlparse(r.headers["location"])
    assert f"{loc.scheme}://{loc.netloc}{loc.path}" == "https://cas.ucdavis.edu/cas/logout"
    assert parse_qs(loc.query)["service"] == ["http://testserver"]
