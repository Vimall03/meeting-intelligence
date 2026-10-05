from __future__ import annotations

import logging
from html import escape
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.core.user_messages import product_error
from app.services.integrations import atlassian_service, atlassian_callback

from app.services.atlassian_service import (
    AtlassianAPIError, AtlassianAuthorizationRequiredError, AtlassianError,
    AtlassianNotConfiguredError, AtlassianOAuthError, AtlassianService,
)
from app.services.google_drive_service import GoogleDriveSearchTerms

router = APIRouter(prefix="/api/integrations/atlassian", tags=["atlassian"])


Service = Annotated[Any, Depends(atlassian_service)]
CallbackService = Annotated[Any, Depends(atlassian_callback)]


@router.get("/status")
async def status(service: Service) -> dict[str, Any]:
    return await service.status()


@router.get("/auth/start")
async def start_auth(service: Service, force: bool = False) -> dict[str, Any]:
    try:
        current = await service.status()
        return {**current, "authorization_url": None} if current["connected"] and not force else await service.authorization_url()
    except AtlassianError as exc:
        raise _http_error(exc) from exc


@router.get("/oauth/callback", response_class=HTMLResponse)
async def callback(service: CallbackService, code: str | None = None, state: str | None = None, error: str | None = None, error_description: str | None = None) -> HTMLResponse:
    if error:
        return _result_page("Sign-in wasn’t completed", product_error("MI-ATL-003"), 400)
    try:
        await service.complete_authorization(code or "", state or "")
        return _result_page("Jira and Confluence are connected", "Related Jira issues and Confluence pages can now appear in your meeting activity. You can also review and create suggested Jira tasks.", 200)
    except AtlassianError as exc:
        logging.getLogger(__name__).warning("MI-ATL-002: sign-in failed (%s)", type(exc).__name__)
        return _result_page("We couldn’t connect Jira and Confluence", product_error("MI-ATL-002"), 400)


@router.post("/disconnect", status_code=204)
async def disconnect(service: Service) -> None:
    await service.disconnect()


@router.get("/search")
async def search(service: Service, query: str = Query(min_length=1, max_length=150)) -> dict[str, Any]:
    try:
        return await service.search_relevant_files(GoogleDriveSearchTerms(primary_terms=(query,)))
    except AtlassianError as exc:
        raise _http_error(exc) from exc


class CreateJiraTask(BaseModel):
    project_id: str = Field(min_length=1)
    site_url: str = Field(min_length=1, max_length=253)
    title: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1, max_length=5000)


@router.get('/sites')
async def sites(service: Service):
    return {'sites': await service.list_sites()}


class SelectSite(BaseModel):
    cloud_id: str = Field(min_length=1, max_length=128)


@router.post('/site')
async def select_site(service: Service, choice: SelectSite):
    try:
        return await service.select_site(choice.cloud_id)
    except AtlassianError as exc:
        raise _http_error(exc) from exc


@router.get("/jira/projects")
async def jira_projects(service: Service) -> dict[str, Any]:
    try:
        return {"projects": await service.list_jira_projects()}
    except AtlassianError as exc:
        raise _http_error(exc) from exc


@router.post("/jira/tasks", status_code=201)
async def create_jira_task(service: Service, request: CreateJiraTask) -> dict[str, str]:
    try:
        return await service.create_jira_task(
            project_id=request.project_id, title=request.title, description=request.description, expected_site_url=request.site_url,
        )
    except AtlassianError as exc:
        raise _http_error(exc) from exc


def _http_error(exc: AtlassianError) -> HTTPException:
    if isinstance(exc, AtlassianAuthorizationRequiredError):
        status = 401
    elif isinstance(exc, AtlassianNotConfiguredError):
        status = 503
    elif isinstance(exc, (AtlassianOAuthError, AtlassianAPIError)):
        status = 502
    else:
        status = 500
    support_code = "MI-AUTH-001" if status == 401 else "MI-ATL-001"
    logging.getLogger(__name__).warning("%s: integration request failed (%s)", support_code, type(exc).__name__)
    return HTTPException(status_code=status, detail={"code": "atlassian_error", "support_code": support_code, "message": product_error(support_code)})


def _result_page(title: str, message: str, status: int) -> HTMLResponse:
    return HTMLResponse(
        f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{escape(title)}</title>'
        '<style>body{font:16px system-ui,sans-serif;max-width:34rem;margin:12vh auto;padding:2rem;color:#172b4d}'
        'main{padding:2rem;border:1px solid #dfe1e6;border-radius:14px}</style></head><body><main>'
        f'<h1>{escape(title)}</h1><p>{escape(message)}</p><p>You may close this tab and return to Meeting Intelligence.</p>'
        '</main></body></html>', status_code=status,
    )
