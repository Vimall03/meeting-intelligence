from __future__ import annotations

import base64
import asyncio
import hashlib
import hmac
import json
import os
import secrets
import time
from asyncio import Lock
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx


GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"
DRIVE_FILE_FIELDS = (
    "id,name,mimeType,webViewLink,modifiedTime,size,parents,iconLink,"
    "description,owners(displayName,emailAddress)"
)
AUTH_STATE_TTL_SECONDS = 10 * 60
TOKEN_REFRESH_SKEW_SECONDS = 60
MAX_RELEVANCE_SEARCH_TERMS = 8
MIN_RELEVANCE_SCORE = 2.0


class GoogleDriveError(RuntimeError):
    """Base error for expected Google Drive integration failures."""


class GoogleDriveNotConfiguredError(GoogleDriveError):
    pass


class GoogleDriveAuthorizationRequiredError(GoogleDriveError):
    pass


class GoogleDriveOAuthError(GoogleDriveError):
    pass


class GoogleDriveAPIError(GoogleDriveError):
    pass


@dataclass(frozen=True, slots=True)
class GoogleDriveSettings:
    client_id: str
    client_secret: str
    redirect_uri: str
    oauth_state_secret: str
    token_path: str
    scopes: str
    timeout_seconds: float = 8.0


@dataclass(frozen=True, slots=True)
class GoogleDriveSearchTerms:
    """Structured retrieval terms produced by the meeting LLM."""

    primary_terms: tuple[str, ...] = ()
    alternate_terms: tuple[str, ...] = ()
    entities: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "GoogleDriveSearchTerms":
        return cls(
            primary_terms=_clean_terms(value.get("primary_terms")),
            alternate_terms=_clean_terms(value.get("alternate_terms")),
            entities=_clean_terms(value.get("entities")),
        )

    @property
    def query(self) -> str:
        return self.primary_terms[0] if self.primary_terms else " ".join(self.entities)

    def weighted_terms(self) -> tuple[tuple[str, float], ...]:
        weighted: list[tuple[str, float]] = []
        seen: set[str] = set()
        for terms, weight in (
            (self.primary_terms, 4.0),
            (self.alternate_terms, 2.5),
            (self.entities, 1.5),
        ):
            for term in terms:
                if term in seen:
                    continue
                seen.add(term)
                weighted.append((term, weight))
        return tuple(weighted[:MAX_RELEVANCE_SEARCH_TERMS])


class GoogleDriveTokenStore:
    """Atomic file store for the single-user backend refresh token."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self._lock = Lock()

    async def read(self) -> dict[str, Any] | None:
        async with self._lock:
            return self._read_unlocked()

    async def write(self, token: dict[str, Any]) -> None:
        async with self._lock:
            self._write_unlocked(token)

    async def clear(self) -> None:
        async with self._lock:
            self._write_unlocked({})

    def _read_unlocked(self) -> dict[str, Any] | None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            raise GoogleDriveError(f"Could not read Google Drive token store: {exc}") from exc
        return payload if isinstance(payload, dict) else None

    def _write_unlocked(self, token: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.path.with_name(f".{self.path.name}.{secrets.token_hex(8)}.tmp")
        try:
            temporary_path.write_text(json.dumps(token), encoding="utf-8")
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, self.path)
            os.chmod(self.path, 0o600)
        finally:
            temporary_path.unlink(missing_ok=True)


class GoogleDriveService:
    """Low-latency Google Drive OAuth + metadata/search adapter."""

    def __init__(
        self,
        settings: GoogleDriveSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.token_store = GoogleDriveTokenStore(settings.token_path)
        self._token_refresh_lock = Lock()
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.timeout_seconds, connect=3.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            transport=transport,
            headers={"User-Agent": "ai-meetings-google-drive/0.1"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    def _ensure_oauth_configured(self) -> None:
        missing = [
            name
            for name, value in (
                ("GOOGLE_DRIVE_CLIENT_ID", self.settings.client_id),
                ("GOOGLE_DRIVE_CLIENT_SECRET", self.settings.client_secret),
                ("GOOGLE_DRIVE_REDIRECT_URI", self.settings.redirect_uri),
            )
            if not value
        ]
        if missing:
            raise GoogleDriveNotConfiguredError(
                f"Google Drive OAuth is not configured; missing {', '.join(missing)}"
            )

    def _state_secret(self) -> str:
        secret = self.settings.oauth_state_secret or self.settings.client_secret
        if not secret:
            raise GoogleDriveNotConfiguredError(
                "Set GOOGLE_DRIVE_OAUTH_STATE_SECRET before starting Google Drive OAuth"
            )
        return secret

    def _create_state(self) -> str:
        payload = {
            "exp": int(time.time()) + AUTH_STATE_TTL_SECONDS,
            "nonce": secrets.token_urlsafe(24),
        }
        encoded_payload = _base64_json(payload)
        signature = hmac.new(
            self._state_secret().encode("utf-8"),
            encoded_payload.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        return f"{encoded_payload}.{signature}"

    def _verify_state(self, state: str) -> None:
        try:
            encoded_payload, signature = state.split(".", 1)
            expected_signature = hmac.new(
                self._state_secret().encode("utf-8"),
                encoded_payload.encode("ascii"),
                hashlib.sha256,
            ).hexdigest()
            if not hmac.compare_digest(signature, expected_signature):
                raise ValueError("signature mismatch")
            payload = json.loads(_decode_base64_json(encoded_payload))
            if int(payload["exp"]) < int(time.time()):
                raise ValueError("state expired")
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise GoogleDriveOAuthError("Invalid or expired Google Drive OAuth state") from exc

    def authorization_url(self) -> dict[str, Any]:
        self._ensure_oauth_configured()
        state = self._create_state()
        query = {
            "client_id": self.settings.client_id,
            "redirect_uri": self.settings.redirect_uri,
            "response_type": "code",
            "access_type": "offline",
            "include_granted_scopes": "true",
            # Explicit consent guarantees Google returns a refresh token during
            # the one-time authorization, even if access was granted before.
            "prompt": "consent",
            "scope": self.settings.scopes,
            "state": state,
        }
        return {
            "connected": False,
            "requires_auth": True,
            "authorization_url": f"{GOOGLE_AUTH_URL}?{urlencode(query)}",
            "redirect_uri": self.settings.redirect_uri,
        }

    async def status(self) -> dict[str, Any]:
        token = await self.token_store.read()
        connected = bool(token and token.get("refresh_token"))
        return {
            "provider": "google_drive",
            "connected": connected,
            "requires_auth": not connected,
            "scope": self.settings.scopes,
        }

    async def complete_authorization(self, code: str, state: str) -> dict[str, Any]:
        self._ensure_oauth_configured()
        if not code or not state:
            raise GoogleDriveOAuthError("Google did not return an authorization code and state")
        self._verify_state(state)
        response = await self._client.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": self.settings.client_id,
                "client_secret": self.settings.client_secret,
                "redirect_uri": self.settings.redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        if response.is_error:
            raise GoogleDriveOAuthError(_response_error(response, "Google token exchange failed"))
        payload = response.json()
        refresh_token = payload.get("refresh_token")
        if not refresh_token:
            existing = await self.token_store.read()
            refresh_token = existing.get("refresh_token") if existing else None
        if not refresh_token:
            raise GoogleDriveOAuthError(
                "Google did not return a refresh token; revoke the app grant and authorize again"
            )
        token = _token_payload(payload, refresh_token=refresh_token)
        await self.token_store.write(token)
        return {"provider": "google_drive", "connected": True, "requires_auth": False}

    async def disconnect(self) -> None:
        token = await self.token_store.read()
        if token and token.get("access_token"):
            try:
                await self._client.post(
                    "https://oauth2.googleapis.com/revoke",
                    params={"token": token["access_token"]},
                )
            except httpx.HTTPError:
                # Local disconnect must still remove the server-side credential.
                pass
        await self.token_store.clear()

    async def search_files(
        self,
        query: str = "",
        *,
        page_size: int = 20,
        page_token: str | None = None,
    ) -> dict[str, Any]:
        params = _file_list_params(query, page_size=page_size)
        if page_token:
            params["pageToken"] = page_token
        payload = await self._drive_get(params)
        return {
            "files": [_normalize_file(file) for file in payload.get("files", [])],
            "next_page_token": payload.get("nextPageToken"),
            "query": query,
        }

    async def search_relevant_files(
        self,
        search_terms: GoogleDriveSearchTerms | Mapping[str, Any],
        *,
        top_k: int = 5,
    ) -> dict[str, Any]:
        """Search Drive for structured terms, then deduplicate and rank files."""
        if not isinstance(search_terms, GoogleDriveSearchTerms):
            search_terms = GoogleDriveSearchTerms.from_mapping(search_terms)

        weighted_terms = search_terms.weighted_terms()
        search_started = time.perf_counter()
        if not weighted_terms:
            return {
                "query": search_terms.query,
                "files": [],
                "search_latency_ms": 0.0,
                "ranking_latency_ms": 0.0,
            }

        responses = await asyncio.gather(
            *(
                self._search_term(term, page_size=max(20, top_k * 4))
                for term, _ in weighted_terms
            )
        )
        search_latency_ms = (time.perf_counter() - search_started) * 1000

        ranking_started = time.perf_counter()
        candidates: dict[str, dict[str, Any]] = {}
        for (term, weight), files in zip(weighted_terms, responses):
            for raw_file in files:
                file_id = raw_file.get("id")
                if not file_id:
                    continue
                normalized = candidates.setdefault(
                    file_id,
                    {
                        **_normalize_file(raw_file),
                        "matched_terms": [],
                        "relevance_score": 0.0,
                    },
                )
                normalized["matched_terms"] = sorted(
                    set(normalized["matched_terms"]) | {term}
                )
                normalized["relevance_score"] += _score_file(
                    normalized,
                    term,
                    weight,
                )

        ranked_files = sorted(
            (
                file
                for file in candidates.values()
                if file["relevance_score"] >= MIN_RELEVANCE_SCORE
            ),
            key=lambda file: (-file["relevance_score"], file["name"].casefold()),
        )[: max(1, min(top_k, 10))]
        for file in ranked_files:
            file["relevance_score"] = round(file["relevance_score"], 4)
        ranking_latency_ms = (time.perf_counter() - ranking_started) * 1000
        return {
            "query": search_terms.query,
            "files": ranked_files,
            "search_latency_ms": round(search_latency_ms, 2),
            "ranking_latency_ms": round(ranking_latency_ms, 2),
        }

    async def _search_term(self, term: str, *, page_size: int) -> list[dict[str, Any]]:
        payload = await self._drive_get(_file_list_params(term, page_size=page_size))
        return [file for file in payload.get("files", []) if isinstance(file, dict)]

    async def get_file(self, file_id: str) -> dict[str, Any]:
        if not file_id.strip():
            raise GoogleDriveAPIError("file_id must not be empty")
        payload = await self._drive_get(
            {"fields": DRIVE_FILE_FIELDS},
            file_id=file_id,
        )
        return _normalize_file(payload)

    async def _drive_get(
        self,
        params: dict[str, Any],
        *,
        file_id: str | None = None,
    ) -> dict[str, Any]:
        access_token = await self._access_token()
        url = GOOGLE_DRIVE_FILES_URL if file_id is None else f"{GOOGLE_DRIVE_FILES_URL}/{file_id}"
        response = await self._client.get(
            url,
            params=params,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if response.status_code == 401:
            access_token = await self._access_token(force_refresh=True)
            response = await self._client.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {access_token}"},
            )
        if response.status_code == 401:
            raise GoogleDriveAuthorizationRequiredError(
                _response_error(response, "Google Drive authorization is no longer valid")
            )
        if response.is_error:
            raise GoogleDriveAPIError(_response_error(response, "Google Drive request failed"))
        payload = response.json()
        if not isinstance(payload, dict):
            raise GoogleDriveAPIError("Google Drive returned an invalid response")
        return payload

    async def _access_token(self, *, force_refresh: bool = False) -> str:
        token = await self.token_store.read()
        if not token or not token.get("refresh_token"):
            raise GoogleDriveAuthorizationRequiredError(
                "Authorize Google Drive once before searching files"
            )
        expires_at = float(token.get("expires_at", 0))
        if (
            not force_refresh
            and token.get("access_token")
            and expires_at > time.time() + TOKEN_REFRESH_SKEW_SECONDS
        ):
            return str(token["access_token"])

        async with self._token_refresh_lock:
            # Another request may have refreshed the file while this request
            # waited for the lock.
            token = await self.token_store.read() or token
            expires_at = float(token.get("expires_at", 0))
            if (
                not force_refresh
                and token.get("access_token")
                and expires_at > time.time() + TOKEN_REFRESH_SKEW_SECONDS
            ):
                return str(token["access_token"])
            response = await self._client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "client_id": self.settings.client_id,
                    "client_secret": self.settings.client_secret,
                    "refresh_token": token["refresh_token"],
                    "grant_type": "refresh_token",
                },
            )
            if response.is_error:
                if response.status_code == 400:
                    await self.token_store.clear()
                    raise GoogleDriveAuthorizationRequiredError(
                        "Google Drive authorization expired or was revoked"
                    )
                raise GoogleDriveOAuthError(_response_error(response, "Google token refresh failed"))
            refreshed = _token_payload(
                response.json(), refresh_token=str(token["refresh_token"])
            )
            await self.token_store.write(refreshed)
            return str(refreshed["access_token"])


def _token_payload(payload: dict[str, Any], *, refresh_token: str) -> dict[str, Any]:
    access_token = payload.get("access_token")
    if not access_token:
        raise GoogleDriveOAuthError("Google token response did not contain an access token")
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": time.time() + int(payload.get("expires_in", 3600)),
        "token_type": payload.get("token_type", "Bearer"),
        "scope": payload.get("scope"),
    }


def _normalize_file(file: dict[str, Any]) -> dict[str, Any]:
    # Keep this response small and stable for the extension. It is metadata
    # only; content is fetched later when the UI needs it.
    return {
        "id": file.get("id"),
        "name": file.get("name", ""),
        "mime_type": file.get("mimeType", ""),
        "url": file.get("webViewLink"),
        "web_view_link": file.get("webViewLink"),
        "modified_time": file.get("modifiedTime"),
        "size": file.get("size"),
        "parents": file.get("parents", []),
        "icon_link": file.get("iconLink"),
        "description": file.get("description"),
        "owners": file.get("owners", []),
        "source": "google_drive",
    }


def _clean_terms(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return ()
    terms: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        term = " ".join(item.split()).strip().casefold()
        if not term or len(term) > 120 or term in seen:
            continue
        seen.add(term)
        terms.append(term)
    return tuple(terms)


def _file_list_params(query: str, *, page_size: int) -> dict[str, Any]:
    escaped_query = _escape_drive_query(query.strip())
    drive_query = "trashed = false"
    if escaped_query:
        drive_query += (
            f" and (name contains '{escaped_query}' "
            f"or fullText contains '{escaped_query}')"
        )
    params = {
        "q": drive_query,
        "pageSize": max(1, min(page_size, 100)),
        "spaces": "drive",
        "fields": f"nextPageToken,files({DRIVE_FILE_FIELDS})",
        "includeItemsFromAllDrives": "true",
        "supportsAllDrives": "true",
    }
    # Drive rejects orderBy whenever q contains a fullText term; without it,
    # Drive returns its own relevance order and our service ranks the candidates.
    if not escaped_query:
        params["orderBy"] = "modifiedTime desc,name"
    return params


def _score_file(file: dict[str, Any], term: str, weight: float) -> float:
    name = str(file.get("name") or "").casefold()
    description = str(file.get("description") or "").casefold()
    normalized_term = term.casefold()
    tokens = [token for token in normalized_term.split() if token]
    name_token_hits = sum(token in name for token in tokens)
    description_token_hits = sum(token in description for token in tokens)

    score = weight
    if normalized_term in name:
        score += 6.0 * weight
    elif tokens and name_token_hits == len(tokens):
        score += 3.5 * weight
    elif name_token_hits:
        score += 1.5 * weight * (name_token_hits / len(tokens))
    if normalized_term in description:
        score += 2.0 * weight
    elif description_token_hits:
        score += 0.5 * weight * (description_token_hits / len(tokens))
    return score


def _escape_drive_query(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _response_error(response: httpx.Response, fallback: str) -> str:
    try:
        payload = response.json()
        error = payload.get("error", payload) if isinstance(payload, dict) else payload
        if isinstance(error, dict):
            description = error.get("message") or error.get("error_description")
            if description:
                return str(description)
        if isinstance(error, str):
            return error
    except (ValueError, json.JSONDecodeError):
        pass
    return f"{fallback} (HTTP {response.status_code})"


def _base64_json(payload: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")


def _decode_base64_json(value: str) -> str:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(f"{value}{padding}").decode("utf-8")
