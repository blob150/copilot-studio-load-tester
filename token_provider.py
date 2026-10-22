"""
Pluggable Entra ID token acquisition for the Copilot Studio Direct-to-Engine (D2E) endpoint.

Scope: https://api.powerplatform.com/.default   (delegated CopilotStudio.Copilots.Invoke)

Providers
---------
- StaticTokenProvider : paste a bearer token (fastest smoke test; no app reg needed if you
                        can grab a token from browser devtools / `pac auth`).
- DeviceCodeProvider  : interactive, MFA-COMPLIANT device-code flow (MSAL public client).
                        Good fallback when ROPC is blocked; can be Playwright-harvested.
- RopcProvider        : non-interactive username/password (ROPC). Requires the account to be
                        EXCLUDED from MFA/Conditional Access and the app to allow public client
                        flows. This is the "code-based mass token" path for a user pool.
- TokenPool           : round-robins cached tokens for N accounts across many virtual users.

Design rule: mint ONCE per identity, cache, reuse. Never re-auth per request (AADSTS 429s).
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import msal

SCOPE = ["https://api.powerplatform.com/.default"]
AUTHORITY_TEMPLATE = "https://login.microsoftonline.com/{tenant_id}"
_CACHE_PATH = Path(__file__).parent / ".token_cache.bin"


def _load_cache() -> msal.SerializableTokenCache:
    cache = msal.SerializableTokenCache()
    if _CACHE_PATH.exists():
        cache.deserialize(_CACHE_PATH.read_text())
    return cache


def _save_cache(cache: msal.SerializableTokenCache) -> None:
    if cache.has_state_changed:
        _CACHE_PATH.write_text(cache.serialize())


class TokenProvider:
    """Base: get_token() returns a valid bearer string, refreshing when near expiry."""

    def get_token(self) -> str:  # pragma: no cover - interface
        raise NotImplementedError


class StaticTokenProvider(TokenProvider):
    def __init__(self, token: str):
        self._token = token

    def get_token(self) -> str:
        return self._token


class _MsalBase(TokenProvider):
    def __init__(self, tenant_id: str, client_id: str):
        self.tenant_id = tenant_id
        self.client_id = client_id
        self._lock = threading.Lock()
        self._token = None
        self._exp = 0.0

    def _store(self, result: dict) -> str:
        if "access_token" not in result:
            raise RuntimeError(
                f"Token acquisition failed: {result.get('error')} - "
                f"{result.get('error_description')}"
            )
        self._token = result["access_token"]
        # refresh 5 min before expiry
        self._exp = time.time() + int(result.get("expires_in", 3600)) - 300
        return self._token

    def _valid(self) -> bool:
        return self._token is not None and time.time() < self._exp


class DeviceCodeProvider(_MsalBase):
    """Interactive, MFA-compliant. Prints a code+URL once; caches the token."""

    def __init__(self, tenant_id: str, client_id: str,
                 prompt: Callable[[dict], None] | None = None):
        super().__init__(tenant_id, client_id)
        self._cache = _load_cache()
        self._app = msal.PublicClientApplication(
            client_id, authority=AUTHORITY_TEMPLATE.format(tenant_id=tenant_id),
            token_cache=self._cache,
        )
        self._prompt = prompt or (lambda f: print(f["message"], flush=True))

    def get_token(self) -> str:
        with self._lock:
            if self._valid():
                return self._token
            accounts = self._app.get_accounts()
            if accounts:
                silent = self._app.acquire_token_silent(SCOPE, account=accounts[0])
                if silent and "access_token" in silent:
                    _save_cache(self._cache)
                    return self._store(silent)
            flow = self._app.initiate_device_flow(scopes=SCOPE)
            if "user_code" not in flow:
                raise RuntimeError(f"Device flow failed to start: {flow}")
            self._prompt(flow)
            result = self._app.acquire_token_by_device_flow(flow)
            _save_cache(self._cache)
            return self._store(result)


class RopcProvider(_MsalBase):
    """Non-interactive username/password. Account MUST be MFA/CA-excluded."""

    def __init__(self, tenant_id: str, client_id: str, username: str, password: str):
        super().__init__(tenant_id, client_id)
        self.username = username
        self.password = password
        self._app = msal.PublicClientApplication(
            client_id, authority=AUTHORITY_TEMPLATE.format(tenant_id=tenant_id)
        )

    def get_token(self) -> str:
        with self._lock:
            if self._valid():
                return self._token
            result = self._app.acquire_token_by_username_password(
                self.username, self.password, scopes=SCOPE
            )
            return self._store(result)


@dataclass
class TokenPool:
    """Round-robins a set of providers (one per account) across many virtual users."""

    providers: list[TokenProvider]
    _idx: int = field(default=0, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def next_provider(self) -> TokenProvider:
        with self._lock:
            p = self.providers[self._idx % len(self.providers)]
            self._idx += 1
            return p

    @classmethod
    def from_ropc_accounts(cls, tenant_id: str, client_id: str,
                           accounts: list[dict]) -> "TokenPool":
        return cls([
            RopcProvider(tenant_id, client_id, a["username"], a["password"])
            for a in accounts
        ])


class AccountPoolManager(TokenProvider):
    """Interactive multi-account wizard: click 'Add account' N times, each triggers a
    device-code sign-in (MFA-compliant, no devtools/copy-paste needed). All signed-in
    accounts share one persisted MSAL cache and can be round-robined across virtual
    users, each VU getting a STABLE assigned account for its whole session (so a
    conversation never switches identity mid-stream) via get_token_for(key).

    Caveat: the device-code flow re-uses whatever browser session is already present.
    To add a genuinely different account, sign in from a private/incognito window
    (or sign out of the browser first) when prompted.
    """

    def __init__(self, tenant_id: str, client_id: str):
        self.tenant_id = tenant_id
        self.client_id = client_id
        self._lock = threading.Lock()
        self._cache = _load_cache()
        self._app = msal.PublicClientApplication(
            client_id, authority=AUTHORITY_TEMPLATE.format(tenant_id=tenant_id),
            token_cache=self._cache,
        )
        self._pending: dict | None = None
        self._rr_idx = 0
        self._expiry: dict[str, float] = {}   # home_account_id -> epoch seconds

    def list_accounts(self) -> list[dict]:
        """Each account's current token expiry, refreshing silently (cheap/cached by
        MSAL) so the UI can show a live, accurate countdown rather than a stale guess."""
        with self._lock:
            accounts = list(self._app.get_accounts())
        out = []
        for a in accounts:
            haid = a.get("home_account_id")
            exp = self._expiry.get(haid)
            status = "unknown"
            try:
                result = self._app.acquire_token_silent(SCOPE, account=a)
                if result and "access_token" in result:
                    exp = time.time() + int(result.get("expires_in", 3600))
                    with self._lock:
                        self._expiry[haid] = exp
                    status = "valid"
                else:
                    status = "expired"
            except Exception:
                status = "error"
            out.append({
                "username": a.get("username"),
                "home_account_id": haid,
                "status": status,
                "expires_at": exp,
                "expires_iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(exp))
                              if exp else None,
                "seconds_remaining": max(0, int(exp - time.time())) if exp else None,
            })
        return out

    def begin_add_account(self) -> dict:
        """Kick off a new device-code sign-in in the background; returns the
        code/URL to show the user immediately. Poll pending_status() for progress."""
        with self._lock:
            if self._pending and self._pending.get("status") == "pending":
                return dict(self._pending)
            flow = self._app.initiate_device_flow(scopes=SCOPE)
            if "user_code" not in flow:
                raise RuntimeError(f"Device flow failed to start: {flow}")
            self._pending = {"status": "pending", "user_code": flow["user_code"],
                             "verification_uri": flow.get("verification_uri"),
                             "message": flow.get("message")}
            pending_ref = self._pending

        def worker():
            try:
                result = self._app.acquire_token_by_device_flow(flow)
                _save_cache(self._cache)
                with self._lock:
                    if "access_token" in result:
                        claims = result.get("id_token_claims", {}) or {}
                        username = (claims.get("preferred_username") or claims.get("upn")
                                   or "signed in")
                        self._pending = {"status": "done", "username": username}
                    else:
                        self._pending = {"status": "error",
                                         "error": result.get("error_description")
                                         or str(result)}
            except Exception as e:
                with self._lock:
                    self._pending = {"status": "error", "error": str(e)}

        threading.Thread(target=worker, daemon=True).start()
        return pending_ref

    def pending_status(self) -> dict:
        with self._lock:
            return dict(self._pending) if self._pending else {"status": "none"}

    def remove_account(self, home_account_id: str) -> None:
        with self._lock:
            for a in self._app.get_accounts():
                if a.get("home_account_id") == home_account_id:
                    self._app.remove_account(a)
            _save_cache(self._cache)

    def _accounts(self) -> list:
        with self._lock:
            return self._app.get_accounts()

    def get_token_for(self, key: str) -> str:
        """Stable per-key assignment: the same key (e.g. a virtual-user id) always
        maps to the same pooled account, so one session keeps one identity throughout."""
        accounts = self._accounts()
        if not accounts:
            raise RuntimeError("No accounts in the pool yet. Add at least one via the wizard.")
        account = accounts[hash(key) % len(accounts)]
        result = self._app.acquire_token_silent(SCOPE, account=account)
        if not result or "access_token" not in result:
            raise RuntimeError(
                f"Silent refresh failed for {account.get('username')}; re-add this "
                f"account via the wizard.")
        return result["access_token"]

    def get_token(self) -> str:
        """Fallback for callers with no per-session key: round-robin on every call."""
        accounts = self._accounts()
        if not accounts:
            raise RuntimeError("No accounts in the pool yet. Add at least one via the wizard.")
        with self._lock:
            account = accounts[self._rr_idx % len(accounts)]
            self._rr_idx += 1
        result = self._app.acquire_token_silent(SCOPE, account=account)
        if not result or "access_token" not in result:
            raise RuntimeError(f"Silent refresh failed for {account.get('username')}")
        return result["access_token"]


def build_provider(cfg: dict) -> TokenProvider:
    """Factory from config dict. mode: static | devicecode | ropc."""
    mode = cfg.get("auth_mode", "devicecode").lower()
    if mode == "static":
        return StaticTokenProvider(cfg["static_token"])
    if mode == "ropc":
        acct = cfg["accounts"][0]
        return RopcProvider(cfg["tenant_id"], cfg["client_id"],
                            acct["username"], acct["password"])
    if mode == "devicecode":
        return DeviceCodeProvider(cfg["tenant_id"], cfg["client_id"])
    raise ValueError(f"Unknown auth_mode: {mode}")
