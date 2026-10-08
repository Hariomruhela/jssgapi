from __future__ import annotations

from fastapi import APIRouter, Depends

from app.core.permissions import Permission
from app.core.responses import ok
from app.dependencies import CurrentUser, DBSession, require_permission
from app.schemas.registration import SyncGoogleSheetRequest
from app.services.registration_sync_service import RegistrationSyncService

router = APIRouter(prefix="/registration", tags=["Registration"])

SYNC = Depends(require_permission(Permission.MEMBER_CREATE))


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
