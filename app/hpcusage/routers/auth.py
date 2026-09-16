"""UC Davis CAS login, using python-cas (https://github.com/python-cas/python-cas).

The library owns the protocol: it builds the CAS login/logout URLs and validates
service tickets (CAS 1/2/3 or CAS 2 + SAML 1.1), returning the username and any
attributes CAS releases. This module only binds it to FastAPI routes and stores the
result in the signed session cookie.

Flow: /auth/login -> CAS login page -> /auth/callback?ticket=ST-... -> validate -> cookie.
"""

import logging
from urllib.parse import urlencode

import requests
from cas import CASClient
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from .. import authz
from ..settings import Settings, get_settings

log = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


def safe_next(next_url: str | None) -> str:
    """Only allow same-site relative redirects."""
    if not next_url or not next_url.startswith("/") or next_url.startswith("//") or "\\" in next_url:
        return "/"
    return next_url


def service_url(settings: Settings, next_url: str) -> str:
    """The CAS 'service' — must be byte-identical at login and validation time."""
    return f"{settings.app_base_url.rstrip('/')}/auth/callback?{urlencode({'next': next_url})}"


def cas_client(settings: Settings, next_url: str = "/") -> CASClient:
    """A client bound to this request's service URL.

    python-cas urljoins 'login', 'logout' and 'p3/serviceValidate' onto server_url, so the
    base must end with '/' or the last path segment ('/cas') would be dropped.
    """
    return CASClient(
        version=settings.cas_version,
        service_url=service_url(settings, next_url),
        server_url=settings.cas_base.rstrip("/") + "/",
    )


def _first(value) -> str | None:
    """CAS may release an attribute once (str) or several times (list)."""
    if isinstance(value, list):
        return value[0] if value else None
    return value


@router.get("/login")
def login(request: Request, next: str = "/", settings: Settings = Depends(get_settings)):
    nxt = safe_next(next)
    if settings.auth_mode == "dev":
        request.session["user"] = settings.dev_user
        return RedirectResponse(nxt, status_code=302)
    return RedirectResponse(cas_client(settings, nxt).get_login_url(), status_code=302)


@router.get("/callback")
def callback(request: Request, ticket: str | None = None, next: str = "/",
             settings: Settings = Depends(get_settings)):
    nxt = safe_next(next)
    if settings.auth_mode == "dev":
        request.session["user"] = settings.dev_user
        return RedirectResponse(nxt, status_code=302)
    if not ticket:
        raise HTTPException(status_code=400, detail="missing ticket")
    try:
        username, attrs, _pgtiou = cas_client(settings, nxt).verify_ticket(ticket)
    except (requests.RequestException, SyntaxError) as e:  # network error, or unparseable XML from CAS
        log.warning("CAS validation request failed: %s", e)
        raise HTTPException(status_code=502, detail="could not validate ticket with CAS") from e
    if not username:
        raise HTTPException(status_code=401, detail="CAS rejected the ticket (invalid, expired or already used)")
    if not authz.is_allowed(username):
        log.warning("CAS user %s authenticated but is not allowed", username)
        raise HTTPException(status_code=403, detail=f"{username} is not authorized to use this application")
    request.session["user"] = username
    display = _first(attrs.get("displayName") or attrs.get("cn") or attrs.get("givenName"))
    if display:
        request.session["display_name"] = display
    log.info("CAS login: %s", username)
    return RedirectResponse(nxt, status_code=302)


@router.get("/logout")
def logout(request: Request, settings: Settings = Depends(get_settings)):
    request.session.clear()
    if settings.auth_mode == "dev":
        return RedirectResponse("/", status_code=302)
    # CAS 3 sends the browser back to `service` after logging out.
    return RedirectResponse(cas_client(settings).get_logout_url(redirect_url=settings.app_base_url), status_code=302)
