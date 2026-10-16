from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query

from app.core.permissions import Permission
from app.core.responses import ok
from app.core.scope import get_access_scope
from app.dependencies import CurrentUser, DBSession, require_permission
from app.services.celebration_service import CelebrationService

router = APIRouter(prefix="/celebrations", tags=["Celebrations"])

READ = Depends(require_permission(Permission.MEMBER_READ))


@router.get("/birthdays", dependencies=[READ])
async def list_todays_birthdays(
    db: DBSession,
    current_user: CurrentUser,
    on: date | None = Query(
        default=None,
        alias="date",
        description="Day to celebrate (YYYY-MM-DD); defaults to today",
    ),
):
    """Today's birthdays, for the member and their spouse.

    Each item carries both people (name + photo) and marks whether it is the
    member's or the spouse's birthday, plus their group and location. Scope
    follows the member directory: group admins see their own group only.
    """
    target = on or date.today()
    scope = await get_access_scope(db, current_user)
    items = await CelebrationService(db).birthdays(scope, target)
    return ok(
        "Today's birthdays fetched successfully",
        [item.model_dump(mode="json") for item in items],
    )


@router.get("/anniversaries", dependencies=[READ])
async def list_todays_anniversaries(
    db: DBSession,
    current_user: CurrentUser,
    on: date | None = Query(
        default=None,
        alias="date",
        description="Day to celebrate (YYYY-MM-DD); defaults to today",
    ),
):
    """Today's wedding anniversaries.

    Each item carries the couple (name + photo), how many years they have been
    married, and their group and location.
    """
    target = on or date.today()
    scope = await get_access_scope(db, current_user)
    items = await CelebrationService(db).anniversaries(scope, target)
    return ok(
        "Today's anniversaries fetched successfully",
        [item.model_dump(mode="json") for item in items],
    )
