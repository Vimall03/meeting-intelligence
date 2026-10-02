import json
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi import FastAPI

from app.api.routes.atlassian import router as atlassian_router
from app.services.atlassian_service import AtlassianAuthorizationRequiredError, AtlassianOAuthError, AtlassianService, AtlassianSettings
from app.services.google_drive_service import GoogleDriveSearchTerms


class AtlassianServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "token.json"
        self.requests = []

        def respond(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if request.url.path == "/oauth/token":
                body = json.loads(request.content)
                if body["grant_type"] == "refresh_token":
                    return httpx.Response(200, json={"access_token": "renewed", "refresh_token": "rotated", "expires_in": 3600})
                return httpx.Response(200, json={"access_token": "initial", "refresh_token": "first", "expires_in": 3600})
            if request.url.path.endswith("accessible-resources"):
                return httpx.Response(200, json=[
                    {"id": "cloud-1", "url": "https://team.atlassian.net", "scopes": ["read:jira-work", "write:jira-work"]},
                    {"id": "cloud-1", "url": "https://team.atlassian.net", "scopes": ["search:confluence"]},
                ])
            if request.url.path.endswith("/search/jql"):
                return httpx.Response(200, json={"issues": [{"key": "APP-42", "fields": {"summary": "Launch plan", "updated": "2026-09-29"}}]})
            if request.url.path.endswith("/wiki/rest/api/search"):
                return httpx.Response(200, json={"results": [{"content": {"id": "123", "title": "Launch plan", "space": {"key": "PROD", "name": "Product"}}}]})
            if request.url.path.endswith("/project/search"):
                start = request.url.params.get("startAt")
                return httpx.Response(200, json={"values": [{"id": "10001", "key": "APP", "name": "App project"}] if start == "0" else [], "isLast": True})
            if request.url.path.endswith("/issuetypes"):
                return httpx.Response(200, json={"issueTypes": [{"id": "10002", "name": "Task", "subtask": False}], "total": 1})
            if request.url.path.endswith("/issue") and request.method == "POST":
                return httpx.Response(201, json={"id": "20001", "key": "APP-42"})
            return httpx.Response(404)

        self.service = AtlassianService(AtlassianSettings(
            client_id="client", client_secret="secret", redirect_uri="http://localhost:8000/callback",
            oauth_state_secret="state-secret", token_path=str(self.path), site_url="https://team.atlassian.net",
        ), transport=httpx.MockTransport(respond))

    async def asyncTearDown(self) -> None:
        await self.service.close()
        self.directory.cleanup()

    async def test_oauth_and_both_search_sources(self) -> None:
        start = self.service.authorization_url()
        query = parse_qs(urlparse(start["authorization_url"]).query)
        self.assertIn("read:jira-work", query["scope"][0])
        self.assertIn("search:confluence", query["scope"][0])
        self.assertIn("offline_access", query["scope"][0])
        self.assertIn("write:jira-work", query["scope"][0])
        await self.service.complete_authorization("code", query["state"][0])
        self.assertTrue((await self.service.status())["connected"])
        result = await self.service.search_relevant_files(GoogleDriveSearchTerms(primary_terms=("launch plan",)))
        self.assertEqual(result["sources"]["jira"][0]["web_view_link"], "https://team.atlassian.net/browse/APP-42")
        self.assertEqual(result["sources"]["confluence"][0]["name"], "Launch plan")
        self.assertEqual(result["sources"]["confluence"][0]["matched_terms"], ["launch plan"])
        jira_request = next(request for request in self.requests if request.url.path.endswith("/search/jql"))
        self.assertIn('text ~ "launch plan"', json.loads(jira_request.content)["jql"])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    async def test_create_task_uses_selected_project_and_adf_description(self) -> None:
        start = self.service.authorization_url()
        state = parse_qs(urlparse(start["authorization_url"]).query)["state"][0]
        await self.service.complete_authorization("code", state)
        self.assertTrue((await self.service.status())["can_create_tasks"])
        self.assertEqual((await self.service.list_jira_projects())[0]["key"], "APP")
        with self.assertRaisesRegex(Exception, "Select a Jira project"):
            await self.service.create_jira_task(project_id="other", title="Plan launch", description="First step")
        result = await self.service.create_jira_task(project_id="10001", title="Plan launch", description="First step\nSecond step")
        self.assertEqual(result["url"], "https://team.atlassian.net/browse/APP-42")
        sent = next(request for request in self.requests if request.method == "POST" and request.url.path.endswith("/issue"))
        fields = json.loads(sent.content)["fields"]
        self.assertEqual(fields["project"], {"id": "10001"})
        self.assertEqual(fields["issuetype"], {"id": "10002"})
        self.assertEqual(fields["description"]["content"][1]["content"][0]["text"], "Second step")

    async def test_jira_only_grant_allows_search_but_not_creation(self) -> None:
        await self.service.token_store.write({
            "access_token": "initial", "refresh_token": "first", "expires_at": time.time() + 3600,
            "cloud_id": "cloud-1", "site_url": "https://team.atlassian.net", "scopes": ["read:jira-work"],
        })
        status = await self.service.status()
        self.assertEqual(status["sources"], ["jira"])
        self.assertFalse(status["can_create_tasks"])
        result = await self.service.search_relevant_files(GoogleDriveSearchTerms(primary_terms=("launch",)))
        self.assertEqual(result["sources"]["jira"][0]["name"], "Launch plan")
        self.assertFalse(any(request.url.path.endswith("/wiki/rest/api/search") for request in self.requests))
        with self.assertRaisesRegex(Exception, "Reconnect Atlassian"):
            await self.service.create_jira_task(project_id="10001", title="Task", description="Description")

    async def test_token_from_another_site_cannot_be_used(self) -> None:
        await self.service.token_store.write({
            "access_token": "initial", "refresh_token": "first", "expires_at": time.time() + 3600,
            "cloud_id": "other-cloud", "site_url": "https://other.atlassian.net",
            "scopes": ["read:jira-work", "write:jira-work"],
        })
        self.assertFalse((await self.service.status())["connected"])
        with self.assertRaises(AtlassianAuthorizationRequiredError):
            await self.service.list_jira_projects()

    async def test_http_project_picker_and_create_route(self) -> None:
        start = self.service.authorization_url()
        state = parse_qs(urlparse(start["authorization_url"]).query)["state"][0]
        await self.service.complete_authorization("code", state)
        app = FastAPI()
        app.state.atlassian = self.service
        app.include_router(atlassian_router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            projects = await client.get("/api/integrations/atlassian/jira/projects")
            self.assertEqual(projects.json()["projects"][0]["key"], "APP")
            created = await client.post("/api/integrations/atlassian/jira/tasks", json={
                "project_id": "10001", "title": "Follow up", "description": "Discuss next steps",
            })
            self.assertEqual(created.status_code, 201)
            self.assertEqual(created.json()["key"], "APP-42")

    async def test_refresh_rotates_token_and_bad_state_fails(self) -> None:
        with self.assertRaises(AtlassianOAuthError):
            await self.service.complete_authorization("code", "bad-state")
        await self.service.token_store.write({
            "access_token": "expired", "refresh_token": "first", "expires_at": time.time() - 1,
            "cloud_id": "cloud-1", "site_url": "https://team.atlassian.net",
        })
        token, _ = await self.service._access_token()
        self.assertEqual(token, "renewed")
        self.assertEqual((await self.service.token_store.read())["refresh_token"], "rotated")

    async def test_multiple_sites_need_explicit_site(self) -> None:
        self.service.settings = AtlassianSettings(
            client_id="client", client_secret="secret", redirect_uri="http://localhost:8000/callback",
            oauth_state_secret="state-secret", token_path=str(self.path),
        )
        resources = [
            {"id": key, "url": f"https://{key}.atlassian.net", "scopes": ["read:jira-work", "search:confluence"]}
            for key in ("one", "two")
        ]
        with self.assertRaisesRegex(AtlassianOAuthError, "Several Atlassian sites"):
            self.service._select_resource(resources)
