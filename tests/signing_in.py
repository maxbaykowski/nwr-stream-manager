"""Signing in for tests: a session cookie straight from the service, or through the sign-in page."""

from __future__ import annotations

import http.client
import re
from http.cookies import SimpleCookie
from urllib.parse import urlencode

AUTH_SESSION_COOKIE_NAME = "nwrstmgr_session"
FORM_TOKEN_COOKIE_NAME = "nwrstmgr_form"


def session_cookie(service, username: str, password: str | None = None) -> str:
    """A Cookie header value signed in as username, or "" if password is given and wrong, as
    signing in would be."""
    accounts = service.accounts
    if password is not None:
        account = accounts.verify_password(username, password)
    else:
        account = next((record for record in map(accounts.account_by_id, range(1, 100)) if record and record.username == username), None)
    if account is None:
        return ""
    token = service.auth_sessions.create(account.id)
    return f"{AUTH_SESSION_COOKIE_NAME}_{accounts.cookie_id}={token}"


def sign_in(host: str, port: int, username: str, password: str, *, next_path: str = "/", headers: dict[str, str] | None = None, cookie: str = ""):
    """Signs in through the sign-in page, as a browser would, with the cookies in cookie if it
    already has some. The response to the form, its body, and the cookies it leaves the browser
    with, as a Cookie header value."""
    headers = dict(headers or {})
    cookies = dict(part.strip().split("=", 1) for part in cookie.split(";") if "=" in part)
    connection = http.client.HTTPConnection(host, port, timeout=10)
    connection.request("GET", "/sign-in", headers={**headers, "Cookie": cookie_header(cookies)})
    page = connection.getresponse()
    page_body = page.read().decode("utf-8")
    cookies = _cookies(page, cookies)
    token = re.search(r'name="form_token" value="([^"]+)"', page_body).group(1)
    form = urlencode({"form_token": token, "next": next_path, "username": username, "password": password})
    connection.request(
        "POST",
        "/sign-in",
        body=form,
        headers={**headers, "Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie_header(cookies)},
    )
    response = connection.getresponse()
    body = response.read().decode("utf-8")
    cookies = _cookies(response, cookies)
    connection.close()
    return response, body, cookie_header(cookies)


def needs_sign_in(response) -> bool:
    """Whether the server answered that signing in is needed: a page sent to the sign-in page, or a 401."""
    return response.status == 401 or (response.status == 303 and (response.getheader("Location") or "").startswith("/sign-in"))


def cookie_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{name}={value}" for name, value in cookies.items())


def _cookies(response, cookies: dict[str, str]) -> dict[str, str]:
    cookies = dict(cookies)
    for header in response.headers.get_all("Set-Cookie") or []:
        for name, morsel in SimpleCookie(header).items():
            if "max-age=0" in header.lower():
                cookies.pop(name, None)
            else:
                cookies[name] = morsel.value
    return cookies
