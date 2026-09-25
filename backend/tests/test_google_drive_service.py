from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

from app.services.google_drive_service import (
    GoogleDriveAPIError,
    GoogleDriveOAuthError,
    GoogleDriveSearchTerms,
    GoogleDriveService,
    GoogleDriveSettings,
)


class GoogleDriveServiceTests(unittest.IsolatedAsyncioTestCase):
    def make_settings(self, token_path: Path) -> GoogleDriveSettings:
        return GoogleDriveSettings(
            client_id="client-id",
            client_secret="client-secret",
            redirect_uri="http://localhost:8000/api/integrations/google-drive/oauth/callback",
            oauth_state_secret="state-secret",
            token_path=str(token_path),
            scopes="https://www.googleapis.com/auth/drive.readonly",
        )

    async def test_one_time_authorization_persists_refresh_token_and_searches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            requests: list[httpx.Request] = []

            async def respond(request: httpx.Request) -> httpx.Response:
                requests.append(request)
                if request.url.host == "oauth2.googleapis.com":
                    return httpx.Response(
                        200,
                        json={
                            "access_token": "access-token",
                            "refresh_token": "refresh-token",
                            "expires_in": 3600,
                        },
                    )
                return httpx.Response(
                    200,
                    json={
                        "nextPageToken": "next-page",
                        "files": [
                            {
                                "id": "file-1",
                                "name": "Meeting notes",
                                "mimeType": "application/vnd.google-apps.document",
                                "webViewLink": "https://docs.google.com/document/d/file-1",
                                "modifiedTime": "2026-09-26T10:00:00Z",
                            }
                        ],
                    },
                )

            service = GoogleDriveService(
                self.make_settings(Path(directory) / "token.json"),
                transport=httpx.MockTransport(respond),
            )
            try:
                auth = service.authorization_url()
                query = parse_qs(urlparse(auth["authorization_url"]).query)
                result = await service.complete_authorization(
                    "authorization-code", query["state"][0]
                )
                search = await service.search_files("meeting", page_size=10)
                status = await service.status()
            finally:
                await service.close()

            self.assertEqual(result["connected"], True)
            self.assertEqual(status["connected"], True)
            self.assertEqual(search["files"][0]["id"], "file-1")
            self.assertEqual(search["files"][0]["url"], "https://docs.google.com/document/d/file-1")
            self.assertEqual(search["next_page_token"], "next-page")
            self.assertEqual(len(requests), 2)
            self.assertEqual(requests[1].headers["authorization"], "Bearer access-token")

            stored = json.loads((Path(directory) / "token.json").read_text())
            self.assertEqual(stored["refresh_token"], "refresh-token")

    async def test_invalid_oauth_state_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = GoogleDriveService(
                self.make_settings(Path(directory) / "token.json"),
                transport=httpx.MockTransport(
                    lambda _: httpx.Response(200, json={"access_token": "unused"})
                ),
            )
            try:
                with self.assertRaises(GoogleDriveOAuthError):
                    await service.complete_authorization("code", "invalid-state")
            finally:
                await service.close()

    async def test_relevant_search_deduplicates_and_ranks_multi_term_results(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            requests: list[httpx.Request] = []

            async def respond(request: httpx.Request) -> httpx.Response:
                requests.append(request)
                if request.url.host == "oauth2.googleapis.com":
                    return httpx.Response(
                        200,
                        json={"access_token": "access-token", "expires_in": 3600},
                    )
                query = request.url.params["q"]
                if "stage database rollback" in query:
                    files = [
                        {
                            "id": "best",
                            "name": "Stage database rollback plan",
                            "mimeType": "application/pdf",
                            "webViewLink": "https://drive.google.com/best",
                        },
                        {
                            "id": "shared",
                            "name": "Database operations",
                            "mimeType": "text/plain",
                        },
                    ]
                else:
                    files = [
                        {
                            "id": "best",
                            "name": "Stage database rollback plan",
                            "mimeType": "application/pdf",
                            "webViewLink": "https://drive.google.com/best",
                        },
                        {
                            "id": "other",
                            "name": "Rollback testing notes",
                            "mimeType": "text/plain",
                        },
                    ]
                return httpx.Response(200, json={"files": files})

            service = GoogleDriveService(
                self.make_settings(Path(directory) / "token.json"),
                transport=httpx.MockTransport(respond),
            )
            try:
                await service.token_store.write(
                    {
                        "access_token": "access-token",
                        "refresh_token": "refresh-token",
                        "expires_at": 9999999999,
                    }
                )
                result = await service.search_relevant_files(
                    GoogleDriveSearchTerms(
                        primary_terms=("stage database rollback",),
                        alternate_terms=("rollback testing",),
                        entities=("stage", "database"),
                    ),
                    top_k=3,
                )
            finally:
                await service.close()

            self.assertEqual([file["id"] for file in result["files"]], ["best", "other", "shared"])
            self.assertIn("stage database rollback", result["files"][0]["matched_terms"])
            self.assertGreater(result["files"][0]["relevance_score"], result["files"][1]["relevance_score"])
            self.assertGreater(len(requests), 1)
            drive_requests = [request for request in requests if request.url.host == "www.googleapis.com"]
            self.assertTrue(drive_requests)
            self.assertTrue(all("orderBy" not in request.url.params for request in drive_requests))
            self.assertTrue(all("fullText contains" in request.url.params["q"] for request in drive_requests))

    async def test_drive_403_query_error_is_not_misreported_as_auth_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            async def respond(request: httpx.Request) -> httpx.Response:
                if request.url.host == "oauth2.googleapis.com":
                    return httpx.Response(200, json={"access_token": "unused", "expires_in": 3600})
                return httpx.Response(
                    403,
                    json={
                        "error": {
                            "message": "Sorting is not supported for queries with fullText terms.",
                            "errors": [{"reason": "badRequest"}],
                        }
                    },
                )

            service = GoogleDriveService(
                self.make_settings(Path(directory) / "token.json"),
                transport=httpx.MockTransport(respond),
            )
            try:
                await service.token_store.write(
                    {
                        "access_token": "access-token",
                        "refresh_token": "refresh-token",
                        "expires_at": 9999999999,
                    }
                )
                with self.assertRaisesRegex(GoogleDriveAPIError, "Sorting is not supported"):
                    await service.search_files("cost comparison")
            finally:
                await service.close()


if __name__ == "__main__":
    unittest.main()
