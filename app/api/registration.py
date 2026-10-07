from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, Request

from app.config import get_settings
from app.core.exceptions import UnauthorizedException
from app.core.permissions import Permission
from app.core.responses import ok
from app.dependencies import CurrentUser, DBSession, require_permission
from app.schemas.registration import SyncGoogleSheetRequest
from app.services.registration_sync_service import RegistrationSyncService

router = APIRouter(prefix="/registration", tags=["Registration"])

SYNC = Depends(require_permission(Permission.MEMBER_CREATE))


def _require_cron_token(request: Request) -> None:
    """Allow only Vercel Cron: ``Authorization: Bearer ${CRON_SECRET}``.

    A scheduler has no session or user, so the shared secret is the
    credential. With ``CRON_SECRET`` unset (the default) this endpoint refuses
    every request instead of running unauthenticated.
    """
    secret = get_settings().cron_secret
    provided = request.headers.get("authorization", "")
    if not secret or not secrets.compare_digest(provided, f"Bearer {secret}"):
        raise UnauthorizedException("Invalid or missing cron secret")


@router.post("/sync-google-sheet", dependencies=[SYNC])
async def sync_google_sheet(
    body: SyncGoogleSheetRequest,
    current_user: CurrentUser,
    db: DBSession,
) -> dict:
    """Import a registration Google Sheet into users/members.

    Read-only by default: ``dry_run`` is ``True`` unless the caller explicitly
    sends ``{"dry_run": false}``, and writing additionally requires
    ``GOOGLE_SHEETS_SYNC_ENABLED=true``.
    """
    result = await RegistrationSyncService(db).run(
        body, current_user_id=current_user.id
    )
    return ok(result.message, result.model_dump())


@router.get("/sync-google-sheet")
async def sync_google_sheet_cron(
    request: Request,
    db: DBSession,
    dry_run: bool = False,
) -> dict:
    """Scheduled run of the very same sync (Vercel Cron).

    Vercel calls this as ``GET /api/registration/sync-google-sheet`` every
    ``CRON`` interval with ``Authorization: Bearer ${CRON_SECRET}`` - see
    ``crons`` in ``vercel.json``. It reuses
    :class:`RegistrationSyncService` unchanged, so a scheduled run behaves
    exactly like the POST endpoint, including the write kill switch
    (``GOOGLE_SHEETS_SYNC_ENABLED=true``).

    ``?dry_run=true`` previews without writing anything.
    """
    _require_cron_token(request)
    result = await RegistrationSyncService(db).run(
        SyncGoogleSheetRequest(dry_run=dry_run), current_user_id=None
    )
    return ok(result.message, result.model_dump())
