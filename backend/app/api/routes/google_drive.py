from __future__ import annotations

import logging
from html import escape
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from app.core.user_messages import product_error
from app.core.observability import log_event, error_fields
from app.services.integrations import google_service, google_callback

from app.services.google_drive_service import (
    GoogleDriveAPIError,
    GoogleDriveAuthorizationRequiredError,
    GoogleDriveError,
    GoogleDriveOAuthError,
    GoogleDriveService,
)


router = APIRouter(
    prefix="/api/integrations/google-drive",
    tags=["google-drive"],
)


Service = Annotated[Any, Depends(google_service)]
CallbackService = Annotated[Any, Depends(google_callback)]


@router.get("/status")
async def status(service: Service) -> dict[str, Any]:
    return await service.status()


@router.get("/auth/start")
async def start_auth(service: Service) -> dict[str, Any]:
    try:
        current_status = await service.status()
        if current_status["connected"]:
            return {**current_status, "authorization_url": None}
        return await service.authorization_url()
    except GoogleDriveError as exc:
        raise _http_error(exc) from exc


@router.get("/oauth/callback", response_class=HTMLResponse)
async def oauth_callback(
    service: CallbackService,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
) -> HTMLResponse:
    if error:
        log_event('integration.connect.cancelled', provider='google_drive', support_code='MI-DRV-003')
        return _oauth_result_page(
            "Google Drive was not connected",
            product_error("MI-DRV-003"),
            success=False,
            status_code=400,
        )
    try:
        await service.complete_authorization(code or "", state or "")
        return _oauth_result_page(
            "Google Drive connected",
            "You can return to Meeting Intelligence. Related files will appear during your meetings.",
            success=True,
        )
    except GoogleDriveError as exc:
        log_event('integration.connect.failed', level=logging.WARNING, provider='google_drive',
                  support_code='MI-DRV-002', **error_fields(exc))
        return _oauth_result_page(
            "Google Drive could not be connected",
            product_error("MI-DRV-002"),
            success=False,
            status_code=502,
        )


@router.post("/disconnect", status_code=204)
async def disconnect(service: Service) -> None:
    await service.disconnect()


@router.get("/files/search")
async def search_files(
    service: Service,
    query: str = Query(default="", max_length=500),
    page_size: int = Query(default=20, ge=1, le=100),
    page_token: str | None = Query(default=None),
) -> dict[str, Any]:
    try:
        return await service.search_files(
            query,
            page_size=page_size,
            page_token=page_token,
        )
    except GoogleDriveError as exc:
        raise _http_error(exc) from exc


@router.get("/files/{file_id}")
async def get_file(file_id: str, service: Service) -> dict[str, Any]:
    try:
        return await service.get_file(file_id)
    except GoogleDriveError as exc:
        raise _http_error(exc) from exc


def _http_error(error: GoogleDriveError) -> HTTPException:
    support_code = ('MI-AUTH-001' if isinstance(error, GoogleDriveAuthorizationRequiredError)
                    else 'MI-DRV-001' if isinstance(error, GoogleDriveAPIError) else 'MI-DRV-002')
    log_event('integration.request.failed', level=logging.WARNING, provider='google_drive',
              support_code=support_code, **error_fields(error))
    if isinstance(error, GoogleDriveAuthorizationRequiredError):
        return HTTPException(
            status_code=401,
            detail={"code": "google_drive_auth_required", "support_code": "MI-AUTH-001", "message": product_error("MI-AUTH-001")},
        )
    if isinstance(error, GoogleDriveOAuthError):
        return HTTPException(
            status_code=502,
            detail={"code": "google_drive_oauth_error", "support_code": "MI-DRV-002", "message": product_error("MI-DRV-002")},
        )
    if isinstance(error, GoogleDriveAPIError):
        return HTTPException(
            status_code=502,
            detail={"code": "google_drive_api_error", "support_code": "MI-DRV-001", "message": product_error("MI-DRV-001")},
        )
    return HTTPException(
        status_code=503,
        detail={"code": "google_drive_not_configured", "support_code": "MI-DRV-002", "message": product_error("MI-DRV-002")},
    )


def _oauth_result_page(
    title: str,
    message: str,
    *,
    success: bool,
    status_code: int = 200,
) -> HTMLResponse:
    safe_title = escape(title)
    safe_message = escape(message)
    tone = "success" if success else "error"
    symbol = "✓" if success else "!"
    html = f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{safe_title} · Meeting Intelligence</title>
    <style>
      :root {{ color-scheme: light; font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: #182230; background: #f6f8fb; }}
      body {{ display: grid; min-height: 100vh; margin: 0; place-items: center; }}
      main {{ width: min(440px, calc(100% - 40px)); padding: 30px; border: 1px solid #e2e7ef; border-radius: 18px; background: white; box-shadow: 0 12px 40px #14213d12; }}
      .mark {{ display: grid; width: 42px; height: 42px; place-items: center; border-radius: 13px; color: {"#07824e" if success else "#a12d2a"}; background: {"#e3f6ec" if success else "#fff0ef"}; font-size: 22px; font-weight: 750; }}
      h1 {{ margin: 18px 0 6px; font-size: 21px; line-height: 1.25; letter-spacing: -.3px; }}
      p {{ margin: 0; color: #68778c; }}
      .hint {{ margin-top: 20px; padding-top: 16px; border-top: 1px solid #edf0f4; font-size: 12px; }}
    </style>
  </head>
  <body><main class="{tone}">
    <div class="mark" aria-hidden="true">{symbol}</div>
    <h1>{safe_title}</h1>
    <p>{safe_message}</p>
    <p class="hint">You may close this tab and return to your meeting.</p>
  </main></body>
</html>"""
    return HTMLResponse(content=html, status_code=status_code)
