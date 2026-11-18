#!/usr/bin/env python3
"""Minimal client for the MiraclePlus investor API with incremental paging."""

from __future__ import annotations

import base64
import json
import os
import random
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

DEFAULT_BASE_URL = "https://api.miracleplus.com/build"
DEFAULT_SCOPE = "public funds"
DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_PAGE_SIZE = 100
DEFAULT_MAX_PAGES = 500
RETRY_STATUSES = {429, 500, 502, 503, 504}


class ApiError(RuntimeError):
    def __init__(self, status: int | None, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


@dataclass
class FetchResult:
    items: list[dict[str, Any]] = field(default_factory=list)
    pages: int = 0
    total: int = 0
    fetched: int = 0
    duplicates: int = 0
    skipped_without_timestamp: int = 0
    oldest_seen: str | None = None
    newest_seen: str | None = None
    stop_reason: str = "max_pages"

    @property
    def unique(self) -> int:
        return len(self.items)


def parse_datetime(value: Any) -> datetime | None:
    """Parse an ISO8601 timestamp and always return an aware UTC datetime."""
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def to_utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def normalized_text(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).strip().split()).casefold()


def load_env_file(path: Path) -> dict[str, str]:
    """Read KEY=VALUE lines without overriding values already in the environment."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def load_repo_env(root: Path) -> None:
    """Load the first .env found under the repository without overriding os.environ."""
    candidates = [
        Path(os.getenv("INVESTOR_SYNC_ENV", "")) if os.getenv("INVESTOR_SYNC_ENV") else None,
        root / ".env",
        root / ".agents" / "skills" / "fundraising-advisor" / ".env",
    ]
    for candidate in candidates:
        if candidate and candidate.is_file():
            for key, value in load_env_file(candidate).items():
                os.environ.setdefault(key, value)
            return


class InvestorApiClient:
    """Client-credentials client. Only GET requests, no credential logging."""

    def __init__(
        self,
        base_url: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        scope: str | None = None,
        timeout: float | None = None,
        user_agent: str = "InvestorDbMaintenance/1.0",
    ) -> None:
        self.base_url = (base_url or os.getenv("INVESTOR_API_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        if not self.base_url.startswith("https://"):
            raise ApiError(None, "invalid_base_url", "INVESTOR_API_BASE_URL must use HTTPS")
        self.client_id = client_id or os.getenv("INVESTOR_API_CLIENT_ID") or ""
        self.client_secret = client_secret or os.getenv("INVESTOR_API_CLIENT_SECRET") or ""
        if not self.client_id or not self.client_secret:
            raise ApiError(
                None,
                "missing_credentials",
                "missing INVESTOR_API_CLIENT_ID or INVESTOR_API_CLIENT_SECRET",
            )
        self.scope = scope or os.getenv("INVESTOR_API_SCOPE") or DEFAULT_SCOPE
        self.timeout = timeout or float(os.getenv("INVESTOR_API_TIMEOUT_SECONDS") or DEFAULT_TIMEOUT_SECONDS)
        self.user_agent = user_agent
        self._token: str | None = None
        self._token_expires_at = 0.0

    def _get_token(self, force: bool = False) -> str:
        if not force and self._token and time.time() < self._token_expires_at - 60:
            return self._token
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode("utf-8")).decode("ascii")
        body = urlencode({"grant_type": "client_credentials", "scope": self.scope}).encode("ascii")
        request = Request(
            f"{self.base_url}/oauth/token",
            data=body,
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "User-Agent": self.user_agent,
            },
            method="POST",
        )
        payload = self._send(request, allow_retry=False)
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not token:
            raise ApiError(None, "invalid_token_response", "token response did not contain access_token")
        self._token = str(token)
        self._token_expires_at = time.time() + int(payload.get("expires_in") or 3600)
        return self._token

    def get(self, path: str, params: dict[str, Any] | None = None, retry_auth: bool = True) -> Any:
        query = f"?{urlencode(params or {})}" if params else ""
        request = Request(
            f"{self.base_url}/open_api/v2/server{path}{query}",
            headers={
                "Authorization": f"Bearer {self._get_token()}",
                "Accept": "application/json",
                "User-Agent": self.user_agent,
            },
            method="GET",
        )
        try:
            return self._send(request, allow_retry=True)
        except ApiError as error:
            if error.status == 401 and retry_auth:
                self._get_token(force=True)
                return self.get(path, params, retry_auth=False)
            raise

    def _send(self, request: Request, allow_retry: bool) -> Any:
        attempts = 3 if allow_retry else 1
        for attempt in range(attempts):
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    content = response.read()
                    return json.loads(content.decode("utf-8")) if content else {}
            except HTTPError as error:
                content = error.read()
                payload: Any = None
                if content:
                    try:
                        payload = json.loads(content.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        payload = None
                code = payload.get("error") if isinstance(payload, dict) else None
                message = payload.get("message") if isinstance(payload, dict) else None
                if error.code in RETRY_STATUSES and attempt + 1 < attempts:
                    retry_after = error.headers.get("Retry-After")
                    delay = float(retry_after) if retry_after and retry_after.isdigit() else 0.5 * (2**attempt)
                    time.sleep(delay + random.uniform(0, 0.2))
                    continue
                if error.code == 403 and not message:
                    message = "insufficient scope or access denied"
                raise ApiError(error.code, str(code or f"http_{error.code}"), str(message or "API request failed"))
            except (URLError, TimeoutError) as error:
                if attempt + 1 < attempts:
                    time.sleep(0.5 * (2**attempt) + random.uniform(0, 0.2))
                    continue
                raise ApiError(None, "network_error", f"API network failure: {type(error).__name__}")
        raise ApiError(None, "request_failed", "API request failed")

    def iter_pages(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> Iterator[dict[str, Any]]:
        seen: set[str] = set()
        for page in range(1, max_pages + 1):
            request_params = dict(params or {})
            request_params.update({"page": page, "page_size": page_size})
            payload = self.get(path, request_params)
            if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
                raise ApiError(None, "invalid_schema", f"{path} returned an invalid response")
            items = payload["items"]
            if not items:
                return
            fresh = []
            for item in items:
                identifier = str(item.get("id") or "")
                if identifier and identifier not in seen:
                    seen.add(identifier)
                    fresh.append(item)
            yield {"page": page, "total": int(payload.get("total") or 0), "items": fresh}
            if len(fresh) < len(items) or len(items) < page_size:
                return
            if len(seen) >= int(payload.get("total") or 0):
                return


def fetch_updated(
    client: InvestorApiClient | Any,
    path: str,
    since: datetime,
    include: str | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> FetchResult:
    """Fetch records updated at or after ``since`` using updated_at_desc paging."""
    if since.tzinfo is None:
        raise ValueError("since must be timezone-aware")
    params: dict[str, Any] = {"order_by": "updated_at_desc"}
    if include:
        params["include"] = include
    result = FetchResult()
    seen: dict[str, dict[str, Any]] = {}
    oldest: datetime | None = None
    newest: datetime | None = None
    for page in client.iter_pages(path, params, page_size=page_size, max_pages=max_pages):
        result.pages = page["page"]
        result.total = page["total"]
        page_timestamps: list[datetime] = []
        for item in page["items"]:
            result.fetched += 1
            updated = parse_datetime(item.get("updated_at"))
            if updated is None:
                result.skipped_without_timestamp += 1
                continue
            page_timestamps.append(updated)
            if updated < since:
                continue
            identifier = str(item.get("id") or "")
            if not identifier or identifier in seen:
                continue
            seen[identifier] = item
        if page_timestamps:
            page_oldest = min(page_timestamps)
            page_newest = max(page_timestamps)
            oldest = page_oldest if oldest is None else min(oldest, page_oldest)
            newest = page_newest if newest is None else max(newest, page_newest)
            if page_oldest < since:
                result.stop_reason = "reached_cutoff"
                break
        if page["page"] * page_size >= page["total"]:
            result.stop_reason = "reached_total"
            break
    else:
        result.stop_reason = "max_pages"
    result.oldest_seen = to_utc_iso(oldest) if oldest else None
    result.newest_seen = to_utc_iso(newest) if newest else None
    result.items = list(seen.values())
    result.duplicates = result.fetched - len(result.items) - result.skipped_without_timestamp
    return result
