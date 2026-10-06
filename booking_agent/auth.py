"""Authenticated session against the CPS identity server.

Credentials live in the macOS Keychain and are read at runtime. Nothing
sensitive is stored in this repo, printed, or logged.

Seed the Keychain yourself — the -w flag prompts interactively so the password
never lands in shell history:

    security add-generic-password -a <email> -s georgewright-cps -w
"""

from __future__ import annotations

import http.client
import json
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

from booking_agent.adapters.base import AuthExpired, TransientError

HOST = "https://georgewright.cps.golf"
TOKEN_ENDPOINT = f"{HOST}/identityapi/connect/token"

# Both are public values shipped in the SPA's assets/env.js — not secrets.
CLIENT_ID = "js1"
CLIENT_SECRET = "v4secret"

# Verified against the live identity server 2026-08-10. Note the omission of
# `offline_access`: it is advertised in the discovery document but the server
# rejects it with invalid_scope, so there is no refresh token. Access tokens
# last an hour, which comfortably outlives any single run.
SCOPES = (
    "openid onlinereservation profile sale customer references email sh recommend"
)
KEYCHAIN_SERVICE = "georgewright-cps"

WEBSITE_ID = "50827848-110c-4067-0beb-08da6c0028fc"
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0 Safari/537.36"
)

# The token endpoint rejects a bare OAuth post with `403 error code: 1010`.
# It wants the same tenant headers as the rest of the API.
_TOKEN_HEADERS = {
    "x-websiteid": WEBSITE_ID,
    "x-productid": "1",
    "x-componentid": "1",
    "x-siteid": "2",
    "x-ismobile": "false",
    "x-timezone-offset": "240",
    "x-timezoneid": "America/New_York",
    "Origin": HOST,
    "Referer": f"{HOST}/onlineresweb/auth/login",
    "User-Agent": _UA,
}


def keychain_password(account: str, service: str = KEYCHAIN_SERVICE) -> str:
    """Read a password from the login Keychain. Never logged, never cached."""
    try:
        # Absolute path: launchd runs with a minimal PATH, and a bare
        # "security" can resolve to nothing at 07:00 on a Tuesday.
        out = subprocess.run(
            ["/usr/bin/security", "find-generic-password",
             "-a", account, "-s", service, "-w"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AuthExpired(f"keychain unavailable: {exc}") from exc
    if out.returncode != 0:
        raise AuthExpired(
            f"no keychain entry for {account}/{service}. Add one with:\n"
            f"  security add-generic-password -a {account} -s {service} -w"
        )
    return out.stdout.rstrip("\n")


class AuthSession:
    """Password-grant session with refresh. Tokens are held in memory only."""

    def __init__(self, account: str, *, service: str = KEYCHAIN_SERVICE,
                 timeout: float = 15.0) -> None:
        self.account = account
        self.service = service
        self.timeout = timeout
        self._access: str | None = None
        self._refresh: str | None = None
        self._expires_at = 0.0
        self.golfer_id: int | None = None

    @property
    def bearer(self) -> str:
        if self._access and time.monotonic() < self._expires_at - 60:
            return self._access
        if self._refresh:
            try:
                return self._grant({"grant_type": "refresh_token",
                                    "refresh_token": self._refresh})
            except AuthExpired:
                self._refresh = None  # fall through to a fresh password grant
        return self._grant({
            "grant_type": "password",
            "username": self.account,
            "password": keychain_password(self.account, self.service),
        })

    def _grant(self, extra: dict) -> str:
        body = urllib.parse.urlencode({
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "scope": SCOPES,
            **extra,
        }).encode()
        req = urllib.request.Request(TOKEN_ENDPOINT, data=body, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        for key, value in _TOKEN_HEADERS.items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            if exc.code >= 500:
                # The identity server having a bad moment is not a bad password.
                raise TransientError(f"token grant: server error {exc.code}") from exc
            detail = exc.read().decode(errors="replace")[:200]
            # Never echo the body verbatim beyond the error code — it can
            # contain hints about the credential.
            raise AuthExpired(
                f"token grant failed ({exc.code}). "
                f"Check the Keychain entry for {self.account}. {detail[:80]}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError,
                http.client.HTTPException) as exc:
            # Was AuthExpired: a network blip at 06:58 aborted the run and
            # blamed the Keychain password. It's retryable, not a login problem.
            raise TransientError(
                f"token grant: network: {getattr(exc, 'reason', exc)}"
            ) from exc

        self._access = data["access_token"]
        self._refresh = data.get("refresh_token")
        self._expires_at = time.monotonic() + float(data.get("expires_in", 3600))
        return self._access

    def invalidate(self) -> None:
        self._access = None
        self._expires_at = 0.0

    def __repr__(self) -> str:  # keep tokens out of tracebacks
        state = "authenticated" if self._access else "unauthenticated"
        return f"<AuthSession {self.account} {state}>"
