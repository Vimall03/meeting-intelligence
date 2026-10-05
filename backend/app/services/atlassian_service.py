"""Atlassian Cloud OAuth and live Jira/Confluence search for one local user."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode, urlparse

import httpx

from app.services.google_drive_service import GoogleDriveSearchTerms, GoogleDriveTokenStore

AUTH_URL = "https://auth.atlassian.com/authorize"
TOKEN_URL = "https://auth.atlassian.com/oauth/token"
RESOURCES_URL = "https://api.atlassian.com/oauth/token/accessible-resources"
API_BASE = "https://api.atlassian.com/ex"
SCOPES = "read:jira-work write:jira-work search:confluence offline_access"
SOURCES = (("jira", "Jira"), ("confluence", "Confluence"))


class AtlassianError(RuntimeError):
    pass


class AtlassianNotConfiguredError(AtlassianError):
    pass


class AtlassianAuthorizationRequiredError(AtlassianError):
    pass


class AtlassianOAuthError(AtlassianError):
    pass


class AtlassianAPIError(AtlassianError):
    pass


@dataclass(frozen=True, slots=True)
class AtlassianSettings:
    client_id: str
    client_secret: str
    redirect_uri: str
    oauth_state_secret: str
    token_path: str
    site_url: str = ""
    timeout_seconds: float = 8.0


def _site_url(value: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme != "https" or not parsed.hostname or not parsed.hostname.endswith(".atlassian.net") or parsed.port or parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise AtlassianOAuthError("ATLASSIAN_SITE_URL must be a Cloud URL like https://team.atlassian.net")
    return f"https://{parsed.hostname.lower()}"


def _query_text(value: str) -> str:
    # JSON string escaping also produces the quoted string syntax used by JQL and CQL.
    return json.dumps(" ".join(value.split())[:150], ensure_ascii=False)


def _state_decode(value: str) -> dict[str, Any]:
    padded = value + "=" * (-len(value) % 4)
    return json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))


class AtlassianService:
    def __init__(self, settings: AtlassianSettings, *, transport: httpx.AsyncBaseTransport | None = None, token_store=None) -> None:
        self.settings = settings
        self.token_store = token_store if token_store is not None else GoogleDriveTokenStore(settings.token_path)
        self._refresh_lock = asyncio.Lock()
        self._distributed_refresh_lock = token_store.refresh_lock if token_store is not None else None
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.timeout_seconds, connect=3.0),
            transport=transport,
            headers={"User-Agent": "ai-meetings-atlassian/0.1"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    def _configured(self) -> bool:
        return bool(self.settings.client_id and self.settings.client_secret and self.settings.redirect_uri)

    def _require_config(self) -> None:
        if not self._configured():
            raise AtlassianNotConfiguredError("Set ATLASSIAN_CLIENT_ID and ATLASSIAN_CLIENT_SECRET in backend/app/.env")
        if self.settings.site_url:
            _site_url(self.settings.site_url)

    def _state_secret(self) -> bytes:
        return (self.settings.oauth_state_secret or self.settings.client_secret).encode("utf-8")

    def _token_matches_site(self, token: dict[str, Any] | None) -> bool:
        if not token or not self.settings.site_url:
            return bool(token)
        try:
            return _site_url(str(token.get("site_url", ""))) == _site_url(self.settings.site_url)
        except AtlassianOAuthError:
            return False

    def authorization_url(self) -> dict[str, Any]:
        self._require_config()
        encoded = base64.urlsafe_b64encode(json.dumps({"exp": int(time.time()) + 600, "nonce": secrets.token_urlsafe(24)}).encode()).decode().rstrip("=")
        signature = hmac.new(self._state_secret(), encoded.encode(), hashlib.sha256).hexdigest()
        params = {
            "audience": "api.atlassian.com", "client_id": self.settings.client_id,
            "scope": SCOPES, "redirect_uri": self.settings.redirect_uri,
            "state": f"{encoded}.{signature}", "response_type": "code", "prompt": "consent",
        }
        return {"connected": False, "authorization_url": f"{AUTH_URL}?{urlencode(params)}", "redirect_uri": self.settings.redirect_uri}

    def _verify_state(self, state: str) -> None:
        try:
            encoded, signature = state.split(".", 1)
            expected = hmac.new(self._state_secret(), encoded.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected) or int(_state_decode(encoded)["exp"]) < int(time.time()):
                raise ValueError("invalid state")
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise AtlassianOAuthError("Invalid or expired Atlassian OAuth state") from exc

    async def status(self) -> dict[str, Any]:
        token = await self.token_store.read()
        connected = bool(self._token_matches_site(token) and token and token.get("refresh_token") and token.get("cloud_id"))
        granted = set(token.get("scopes") or []) if connected else set()
        return {
            "provider": "atlassian", "configured": self._configured(),
            "connected": connected, "requires_auth": not connected,
            "site_url": token.get("site_url") if connected else (self.settings.site_url or None),
            "sources": [source for source, scope in (("jira", "read:jira-work"), ("confluence", "search:confluence")) if scope in granted],
            "can_create_tasks": "write:jira-work" in granted,
        }

    async def complete_authorization(self, code: str, state: str, *, state_verified=False) -> dict[str, Any]:
        self._require_config()
        if not code or not state:
            raise AtlassianOAuthError("Atlassian did not return an authorization code and state")
        if not state_verified:
            self._verify_state(state)
        try:
            response = await self._client.post(TOKEN_URL, json={
                "grant_type": "authorization_code", "client_id": self.settings.client_id,
                "client_secret": self.settings.client_secret, "code": code,
                "redirect_uri": self.settings.redirect_uri,
            })
            response.raise_for_status()
            payload = response.json()
            access_token = payload["access_token"]
            refresh_token = payload["refresh_token"]
            resources_response = await self._client.get(RESOURCES_URL, headers={"Authorization": f"Bearer {access_token}"})
            resources_response.raise_for_status()
            resources = resources_response.json()
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise AtlassianOAuthError("Atlassian authorization or site discovery failed") from exc
        if not isinstance(resources, list):
            raise AtlassianOAuthError("Atlassian returned an invalid site list")
        selected = self._select_resource(resources)
        await self.token_store.write({
            "access_token": access_token, "refresh_token": refresh_token,
            "expires_at": time.time() + int(payload.get("expires_in", 3600)),
            "cloud_id": selected["id"], "site_url": selected["url"],
            "scopes": selected["scopes"], "resources": resources,
        })
        return await self.status()

    def _select_resource(self, resources: list[dict[str, Any]]) -> dict[str, Any]:
        sites: dict[str, dict[str, Any]] = {}
        for resource in resources:
            try:
                site = _site_url(str(resource.get("url", "")))
                cloud_id = str(resource["id"])
            except (AtlassianOAuthError, KeyError):
                continue
            entry = sites.setdefault(cloud_id, {"id": cloud_id, "url": site, "scopes": set()})
            entry["scopes"].update(resource.get("scopes") or [])
        candidates = [site for site in sites.values() if {'read:jira-work', 'search:confluence'} & site['scopes']]
        if self.settings.site_url:
            desired = _site_url(self.settings.site_url)
            candidates = [site for site in candidates if site["url"] == desired]
        if not candidates:
            raise AtlassianOAuthError("No authorized Atlassian Cloud site is available.")
        candidates.sort(key=lambda site: site['url'])
        return {"id": candidates[0]["id"], "url": candidates[0]["url"], "scopes": sorted(candidates[0]["scopes"])}

    async def disconnect(self) -> None:
        await self.token_store.clear()

    async def list_sites(self):
        token = await self.token_store.read()
        if not token:
            raise AtlassianAuthorizationRequiredError()
        sites = {}
        for resource in token.get('resources', []):
            try:
                url = _site_url(str(resource.get('url', '')))
            except AtlassianOAuthError:
                continue
            if {'read:jira-work', 'search:confluence'} & set(resource.get('scopes', [])):
                entry = sites.setdefault(resource['id'], {'id': resource['id'], 'url': url, 'name': resource.get('name', url), 'scopes': set()})
                entry['scopes'].update(resource.get('scopes', []))
        return [{'id': s['id'], 'name': s['name'], 'url': s['url'], 'scopes': sorted(s['scopes'])} for s in sites.values()]

    async def select_site(self, cloud_id):
        lock = self._distributed_refresh_lock() if self._distributed_refresh_lock else self._refresh_lock
        async with lock:
            token = await self.token_store.read()
            site = next((s for s in await self.list_sites() if s['id'] == cloud_id), None)
            if not site:
                raise AtlassianAuthorizationRequiredError()
            await self.token_store.write({**token, 'cloud_id': site['id'], 'site_url': site['url'], 'scopes': site['scopes']})
        return await self.status()

    async def _access_token(self, *, force_refresh: bool = False) -> tuple[str, dict[str, Any]]:
        token = await self.token_store.read()
        if not token or not token.get("refresh_token") or not token.get("cloud_id"):
            raise AtlassianAuthorizationRequiredError("Connect Atlassian before searching Jira and Confluence")
        if not self._token_matches_site(token):
            raise AtlassianAuthorizationRequiredError("The saved Atlassian connection is for a different Cloud site. Reconnect this site.")
        if not force_refresh and token.get("access_token") and float(token.get("expires_at", 0)) > time.time() + 60:
            return str(token["access_token"]), token
        async with self._distributed_refresh_lock() if self._distributed_refresh_lock else self._refresh_lock:
            token = await self.token_store.read() or token
            if not force_refresh and token.get("access_token") and float(token.get("expires_at", 0)) > time.time() + 60:
                return str(token["access_token"]), token
            try:
                response = await self._client.post(TOKEN_URL, json={
                    "grant_type": "refresh_token", "client_id": self.settings.client_id,
                    "client_secret": self.settings.client_secret,
                    "refresh_token": token["refresh_token"],
                })
            except httpx.HTTPError as exc:
                raise AtlassianOAuthError("Could not refresh Atlassian access") from exc
            if response.status_code in (400, 401) and response.json().get('error') == 'invalid_grant':
                await self.token_store.clear()
                raise AtlassianAuthorizationRequiredError("Atlassian access expired or was revoked. Connect again.")
            if response.is_error:
                raise AtlassianOAuthError(f"Atlassian token refresh failed ({response.status_code})")
            payload = response.json()
            if not payload.get("access_token") or not payload.get("refresh_token"):
                raise AtlassianOAuthError("Atlassian token refresh returned incomplete credentials")
            updated = {**token, "access_token": payload["access_token"], "refresh_token": payload["refresh_token"], "expires_at": time.time() + int(payload.get("expires_in", 3600))}
            await self.token_store.write(updated)
            return str(updated["access_token"]), updated

    async def _get_or_post(self, method: str, url: str, *, params: dict[str, Any] | None = None, json_body: dict[str, Any] | None = None, operation: str = "request") -> dict[str, Any]:
        access_token, _ = await self._access_token()
        try:
            response = await self._client.request(method, url, params=params, json=json_body, headers={"Authorization": f"Bearer {access_token}"})
            if response.status_code == 401:
                access_token, _ = await self._access_token(force_refresh=True)
                response = await self._client.request(method, url, params=params, json=json_body, headers={"Authorization": f"Bearer {access_token}"})
            if response.is_error:
                raise AtlassianAPIError(f"Jira {operation} failed ({response.status_code}). Check your access and the project's required fields.")
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("invalid response")
            return payload
        except (httpx.HTTPError, ValueError) as exc:
            raise AtlassianAPIError(f"Could not complete the Atlassian {operation}. If you submitted a task, check Jira before retrying.") from exc

    async def list_jira_projects(self, *, cloud_id=None) -> list[dict[str, str]]:
        _, token = await self._access_token()
        cloud_id = cloud_id or token["cloud_id"]
        projects: list[dict[str, str]] = []
        start_at = 0
        while True:
            page = await self._get_or_post(
                "GET", f"{API_BASE}/jira/{cloud_id}/rest/api/3/project/search",
                params={"startAt": start_at, "maxResults": 100}, operation="project list",
            )
            values = page.get("values")
            if not isinstance(values, list):
                raise AtlassianAPIError("Jira returned an invalid project list")
            for project in values:
                if isinstance(project, dict) and all(isinstance(project.get(field), str) for field in ("id", "key", "name")):
                    projects.append({field: project[field] for field in ("id", "key", "name")})
            if page.get("isLast") is True or not values:
                break
            start_at += len(values)
            if start_at >= 1000:
                raise AtlassianAPIError("Too many Jira projects to show. Narrow the site or contact support.")
        return sorted(projects, key=lambda project: project["name"].casefold())

    async def create_jira_task(self, *, project_id: str, title: str, description: str, expected_site_url=None) -> dict[str, str]:
        title = " ".join(title.split())
        description = description.strip()
        if not title or len(title) > 255 or not description or len(description) > 5000:
            raise AtlassianAPIError("Task needs a title (up to 255 characters) and description (up to 5000 characters)")
        _, token = await self._access_token()
        if "write:jira-work" not in set(token.get("scopes") or []):
            raise AtlassianAuthorizationRequiredError("Reconnect Atlassian and approve Jira task creation")
        if expected_site_url is not None and token['site_url'] != _site_url(expected_site_url):
            raise AtlassianAPIError('Site changed. Reopen the task form.')
        projects = await self.list_jira_projects(cloud_id=token['cloud_id'])
        project = next((item for item in projects if item["id"] == project_id), None)
        if project is None:
            raise AtlassianAPIError("Select a Jira project from the list")
        cloud_id = token["cloud_id"]
        issue_types = await self._get_or_post(
            "GET", f"{API_BASE}/jira/{cloud_id}/rest/api/3/issue/createmeta/{quote(project_id, safe='')}/issuetypes",
            params={"maxResults": 100}, operation="task type lookup",
        )
        available_types = issue_types.get("issueTypes", issue_types.get("values"))
        if not isinstance(available_types, list):
            raise AtlassianAPIError("Jira returned an invalid work type list")
        task_type = next((item for item in available_types
                          if isinstance(item, dict) and isinstance(item.get("name"), str)
                          and item["name"].casefold() == "task" and not item.get("subtask")), None)
        if not task_type or not task_type.get("id"):
            raise AtlassianAPIError(f"{project['name']} does not offer a Task work type")
        paragraphs = [{"type": "paragraph", "content": [{"type": "text", "text": line}]} for line in description.splitlines() if line.strip()]
        created = await self._get_or_post(
            "POST", f"{API_BASE}/jira/{cloud_id}/rest/api/3/issue",
            json_body={"fields": {
                "project": {"id": project_id}, "issuetype": {"id": str(task_type["id"])},
                "summary": title,
                "description": {"type": "doc", "version": 1, "content": paragraphs},
            }}, operation="task creation",
        )
        issue_key = created.get("key")
        if not isinstance(issue_key, str) or not issue_key.startswith(project["key"] + "-"):
            raise AtlassianAPIError("Jira created a task but returned an unexpected issue key. Check the project before retrying.")
        return {"key": issue_key, "url": f"{token['site_url']}/browse/{quote(issue_key, safe='-')}", "project_key": project["key"]}

    async def _search_source(self, source: str, term: str, token: dict[str, Any]) -> list[dict[str, Any]]:
        cloud_id = token["cloud_id"]
        site = token["site_url"]
        if source == "jira":
            payload = await self._get_or_post("POST", f"{API_BASE}/jira/{cloud_id}/rest/api/3/search/jql", json_body={
                "jql": f"text ~ {_query_text(term)} ORDER BY updated DESC",
                "fields": ["summary", "updated", "issuetype", "project"], "maxResults": 8,
            })
            files = []
            for issue in payload.get("issues", []):
                key = issue.get("key")
                if not key or not isinstance(key, str):
                    continue
                fields = issue.get("fields") or {}
                files.append({
                    "id": f"jira:{key}", "name": fields.get("summary") or key,
                    "mime_type": "application/vnd.atlassian.jira.issue",
                    "web_view_link": f"{site}/browse/{quote(key, safe='-')}",
                    "modified_time": fields.get("updated"), "source": "jira",
                    "subtitle": key,
                })
            return files
        payload = await self._get_or_post("GET", f"{API_BASE}/confluence/{cloud_id}/wiki/rest/api/search", params={
            "cql": f"type = page AND text ~ {_query_text(term)}",
            "limit": 8,
        })
        files = []
        for item in payload.get("results", []):
            content = item.get("content") or {}
            content_id = content.get("id")
            if not content_id:
                continue
            files.append({
                "id": f"confluence:{content_id}",
                "name": content.get("title") or item.get("title") or "Untitled page",
                "mime_type": "application/vnd.atlassian.confluence.page",
                "web_view_link": f"{site}/wiki/spaces/{quote(str((content.get('space') or {}).get('key')), safe='')}/pages/{quote(str(content_id), safe='')}" if (content.get("space") or {}).get("key") else f"{site}/wiki/pages/viewpage.action?pageId={quote(str(content_id), safe='')}",
                "modified_time": None, "source": "confluence",
                "subtitle": (content.get("space") or {}).get("name") or "Confluence page",
            })
        return files

    async def search_relevant_files(self, terms: GoogleDriveSearchTerms, *, top_k: int = 5) -> dict[str, Any]:
        _, token = await self._access_token()
        weighted = terms.weighted_terms()[:3]
        granted = set(token.get("scopes") or [])
        enabled = [source for source, scope in (("jira", "read:jira-work"), ("confluence", "search:confluence")) if scope in granted]
        searches = [(source, term, weight) for source in enabled for term, weight in weighted]
        results = await asyncio.gather(*(self._search_source(source, term, token) for source, term, _ in searches), return_exceptions=True)
        grouped: dict[str, dict[str, dict[str, Any]]] = {source: {} for source, _ in SOURCES}
        errors: dict[str, str] = {}
        for (source, term, weight), result in zip(searches, results):
            if isinstance(result, BaseException):
                errors[source] = str(result)
                continue
            for file in result:
                current = grouped[source].get(file["id"])
                if current is None:
                    grouped[source][file["id"]] = {**file, "matched_terms": [term], "relevance_score": weight}
                elif term not in current["matched_terms"]:
                    current["matched_terms"].append(term)
                    current["relevance_score"] += weight
        return {
            "query": terms.query,
            "sources": {
                source: sorted(files.values(), key=lambda file: (-file["relevance_score"], file["name"]))[:top_k]
                for source, files in grouped.items()
            },
            "errors": errors,
        }
