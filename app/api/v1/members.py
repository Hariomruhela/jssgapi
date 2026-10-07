from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from sqlalchemy import func, select

from app.core.constants import AuditAction, MemberStatus
from app.core.exceptions import (
    BadRequestException,
    ForbiddenException,
    NotFoundException,
)
from app.core.permissions import Permission
from app.core.responses import created, ok, paginated
from app.core.scope import (
    assert_group_access,
    get_access_scope,
    resolve_managed_group,
)
from app.dependencies import CurrentUser, DBSession, require_permission
from app.models.group import SocialGroup
from app.models.member import Member
from app.repositories.member_repository import (
    MEMBER_PROFILE_LOADS,
    MemberRepository,
    group_name_filter,
)
from app.schemas.member import (
    MemberApproveRequest,
    MemberCreate,
    MemberListItemOut,
    MemberOut,
    MemberUpdate,
)
from app.services.audit_service import AuditService
from app.services.member_photo_service import SLOTS, MemberPhotoService
from app.services.profile_service import ProfileService
from app.utils.file_upload import MAX_IMAGE_SIZE

router = APIRouter(prefix="/members", tags=["Members"])

READ = Depends(require_permission(Permission.MEMBER_READ))
CREATE = Depends(require_permission(Permission.MEMBER_CREATE))
UPDATE = Depends(require_permission(Permission.MEMBER_UPDATE))
DELETE = Depends(require_permission(Permission.MEMBER_DELETE))
APPROVE = Depends(require_permission(Permission.MEMBER_APPROVE))


def _member_list_item(member: Member, profiles: ProfileService) -> MemberListItemOut:
    return MemberListItemOut(
        **MemberOut.model_validate(member).model_dump(),
        **profiles.build_profile(member).model_dump(),
    )


def _split_full_name(full_name: str) -> tuple[str, str]:
    parts = full_name.split()
    if not parts:
        return full_name, ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[-1]


async def _reload_member(db: DBSession, member_id: UUID) -> Member:
    """Reload a member with the freshest columns and every relationship the
    profile view needs (``group``/``location`` are loaded lazily by default)."""
    result = await db.execute(
        select(Member)
        .options(*MEMBER_PROFILE_LOADS)
        .where(Member.id == member_id)
        .execution_options(populate_existing=True)
    )
    return result.scalar_one()


async def _get_member(db: DBSession, member_id: UUID) -> Member | None:
    """Load a member that exists and has not been deleted, relations included.

    A deleted member must look like a missing one to every read, so the flag is
    checked here rather than in each handler.
    """
    result = await db.execute(
        select(Member)
        .options(*MEMBER_PROFILE_LOADS)
        .where(Member.id == member_id, Member.is_deleted.is_(False))
    )
    return result.scalar_one_or_none()


def _photo_response(message: str, photo_url: str | None, **data) -> dict:
    """Photo envelope: the URL is in ``data`` and repeated at the top level so
    the panel can read either shape. Matches the profile photo route, which
    answers 200 with the URL as well."""
    payload = ok(message, {"photo_url": photo_url, **data})
    payload["photo_url"] = photo_url
    return payload


@router.get("", dependencies=[READ])
async def list_members(
    db: DBSession,
    current_user: CurrentUser,
    group_id: UUID | None = Query(default=None),
    group_name: str | None = Query(default=None, max_length=100),
    status: str | None = Query(default=None),
    query: str | None = Query(default=None, max_length=200),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    repo = MemberRepository(db)
    profiles = ProfileService(db)
    scope = await get_access_scope(db, current_user)
    # An empty filter means "no group filter", not "groups with an empty name".
    group_name = (group_name or "").strip() or None

    # A caller-supplied group_id may only narrow the result, never widen it: a
    # GROUP_ADMIN asking for another group is refused rather than redirected, so
    # a client bug surfaces instead of quietly returning the wrong group's data.
    if group_id is not None and not scope.unrestricted and group_id != scope.group_id:
        raise ForbiddenException("Operation is limited to your own group")
    # The panel's Group filter passes group_id; without it a SUPER_ADMIN would
    # get every member no matter which group was picked. A GROUP_ADMIN without
    # an explicit choice keeps seeing only its own group.
    effective_group: UUID | None
    if group_id is not None:
        effective_group = group_id
    else:
        effective_group = None if scope.unrestricted else scope.group_id

    if query:
        # The search envelope deliberately has no `pagination` key: the panel's
        # Members page already handles its absence and a search result set is a
        # single page by contract.
        members = await repo.search(
            query,
            group_id=effective_group,
            membership_status=status,
            group_name=group_name,
            limit=page_size,
            offset=(page - 1) * page_size,
        )
        return ok(
            "Members fetched successfully",
            [_member_list_item(m, profiles) for m in members],
        )

    stmt = (
        select(Member)
        .where(Member.is_deleted.is_(False))
        .options(*MEMBER_PROFILE_LOADS)
    )
    stmt = repo.apply_filters(
        stmt,
        {
            k: v
            for k, v in {
                "group_id": effective_group,
                "membership_status": status,
            }.items()
            if v is not None
        },
    )
    if group_name:
        stmt = stmt.where(group_name_filter(group_name))
    if scope.member_id is not None and scope.group_id is None:
        stmt = stmt.where(Member.id == scope.member_id)
    total = (
        await db.execute(select(func.count()).select_from(stmt.subquery()))
    ).scalar_one()
    result = await db.execute(
        stmt.order_by(Member.first_name).limit(page_size).offset((page - 1) * page_size)
    )
    items = list(result.scalars().all())
    return paginated(
        "Members fetched successfully",
        [_member_list_item(i, profiles) for i in items],
        {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": (total + page_size - 1) // page_size,
        },
    )


@router.post("", dependencies=[CREATE])
async def create_member(body: MemberCreate, current_user: CurrentUser, db: DBSession):
    repo = MemberRepository(db)
    # Only enforced when the admin links an account; imported community members
    # legitimately have no User at all.
    if body.user_id is not None and await repo.get_by_user_id(body.user_id):
        raise BadRequestException("A member profile already exists for this user")
    data = body.member_columns()
    first_name, last_name = _split_full_name(body.full_name)
    data["first_name"] = data.get("first_name") or first_name
    data["last_name"] = data.get("last_name") or last_name
    data["group_id"] = await resolve_managed_group(db, current_user, body.group_id)
    member = await repo.create(**data)
    profiles = ProfileService(db)
    await profiles.apply_values(member, body.profile_values())
    await AuditService(db).log(
        AuditAction.MEMBER_CREATE, entity_type="member", entity_id=member.id
    )
    await db.flush()
    return created(
        "Member created successfully",
        _member_list_item(await _reload_member(db, member.id), profiles),
    )


@router.get("/social-groups", dependencies=[READ])
async def list_social_group_names(db: DBSession, current_user: CurrentUser):
    """Distinct social-group names the member directory can filter by.

    The name is stored either as free text in ``members.profile_data`` or on a
    linked ``social_groups`` row, so both sources are collected - the panel's
    group filter must offer exactly the values members actually carry. This
    route is declared before ``GET /{member_id}`` so ``social-groups`` is never
    read as a member id."""
    scope = await get_access_scope(db, current_user)
    conditions = [Member.is_deleted.is_(False)]
    if not scope.unrestricted and scope.group_id is not None:
        conditions.append(Member.group_id == scope.group_id)
    if (
        not scope.unrestricted
        and scope.group_id is None
        and scope.member_id is not None
    ):
        conditions.append(Member.id == scope.member_id)

    profile_names = (
        await db.execute(
            select(func.distinct(Member.profile_data["social_group_name"].astext)).where(
                *conditions,
                Member.profile_data["social_group_name"].astext.isnot(None),
                Member.profile_data["social_group_name"].astext != "",
            )
        )
    ).scalars()
    linked_names = (
        await db.execute(
            select(func.distinct(SocialGroup.name))
            .join(Member, Member.group_id == SocialGroup.id)
            .where(*conditions, SocialGroup.is_deleted.is_(False))
        )
    ).scalars()
    names = {
        name.strip()
        for name in (*profile_names, *linked_names)
        if name and name.strip()
    }
    return ok("Social groups fetched successfully", sorted(names, key=str.casefold))


@router.get("/{member_id}", dependencies=[READ])
async def get_member(member_id: UUID, db: DBSession, current_user: CurrentUser):
    member = await _get_member(db, member_id)
    if member is None:
        raise NotFoundException("Member", str(member_id))
    scope = await get_access_scope(db, current_user)
    if not scope.unrestricted:
        # A member outside the caller's group must look like a missing record, not
        # a forbidden one: a 403 would confirm the id exists.
        if scope.member_id is not None and member.id == scope.member_id:
            pass
        elif scope.group_id is None or member.group_id != scope.group_id:
            raise NotFoundException("Member", str(member_id))
    # The profile fields are part of the member record the panel renders, so a
    # single fetch answers with the same shape as a list item.
    return ok(
        "Member fetched successfully",
        _member_list_item(member, ProfileService(db)),
    )


@router.patch("/{member_id}", dependencies=[UPDATE])
async def update_member(
    member_id: UUID, body: MemberUpdate, current_user: CurrentUser, db: DBSession
):
    repo = MemberRepository(db)
    member = await repo.get_by_id(member_id)
    if member is None:
        raise NotFoundException("Member", str(member_id))
    await assert_group_access(db, current_user, member.group_id)
    if body.group_id is not None:
        await assert_group_access(db, current_user, body.group_id)
    columns = body.member_columns()
    member = await repo.update(member, **columns)
    # Profile fields live in ``members.profile_data``; a Group Admin has no access to
    # ``PUT /profile`` (that writes the caller's own profile), so the admin edit path
    # must write them too or the panel could never maintain a full member profile.
    await ProfileService(db).apply_values(member, body.profile_values())
    # The panel edits a name through the first/middle/last columns, while the
    # directory displays the profile ``full_name``. Keep them in step, or a
    # rename would silently not show up - unless the caller sent full_name
    # itself, which is then the value that wins.
    if {"first_name", "middle_name", "last_name"} & set(
        columns
    ) and "full_name" not in body.model_fields_set:
        derived_name = " ".join(
            part
            for part in (member.first_name, member.middle_name, member.last_name)
            if part
        ).strip()
        if derived_name:
            profile_data = dict(member.profile_data or {})
            profile_data["full_name"] = derived_name
            member.profile_data = profile_data
    required_fields = ["first_name", "last_name", "contact_email"]
    member.is_profile_complete = all(getattr(member, f) for f in required_fields)
    await AuditService(db).log(
        AuditAction.MEMBER_UPDATE, entity_type="member", entity_id=member.id
    )
    await db.flush()
    # `updated_at` is a server-side onupdate column, so it is expired after the
    # flush and reading it would trigger lazy IO outside the async context -
    # reload before serializing.
    member = await _reload_member(db, member.id)
    profiles = ProfileService(db)
    return ok(
        "Member updated successfully",
        MemberListItemOut(
            **MemberOut.model_validate(member).model_dump(),
            **profiles.build_profile(member).model_dump(),
        ),
    )


@router.post("/{member_id}/photo/{role}", dependencies=[UPDATE])
async def upload_member_photo(
    member_id: UUID,
    role: str,
    request: Request,
    db: DBSession,
    current_user: CurrentUser,
    photo: UploadFile | None = File(default=None),
    file: UploadFile | None = File(default=None),
):
    """Upload or replace the member's (``role=member``) or their spouse's
    (``role=spouse``) photo.

    The bytes go to object storage; only the URL and the object key are stored
    in Postgres. Replacing a photo uploads the new object first, so a storage
    failure leaves the previous photo in place. The file is accepted as either
    ``photo`` or ``file``, matching the two field names the API already uses.
    """
    upload = photo or file
    if upload is None:
        raise BadRequestException("A photo file is required", "INVALID_PHOTO")
    if role not in SLOTS:
        raise BadRequestException(
            f"Unknown photo slot '{role}'; use 'member' or 'spouse'",
            "INVALID_PHOTO",
        )
    member = await _get_member(db, member_id)
    if member is None:
        raise NotFoundException("Member", str(member_id))
    await assert_group_access(db, current_user, member.group_id)

    ip_address = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")
    # One byte past the limit is enough to detect an oversized upload without
    # buffering an arbitrarily large file into memory first.
    content = await upload.read(MAX_IMAGE_SIZE + 1)
    url = await MemberPhotoService(db).upload(
        member,
        role=role,
        file_name=upload.filename,
        content=content,
        mime_type=upload.content_type,
        uploaded_by=current_user.id,
        ip_address=ip_address,
        user_agent=user_agent,
        base_url=str(request.base_url),
    )
    await AuditService(db).log(
        AuditAction.MEMBER_UPDATE,
        user_id=current_user.id,
        entity_type="member",
        entity_id=member.id,
        details={"field": f"{role}_photo", "photo_url": url},
        ip_address=ip_address,
        user_agent=user_agent,
    )
    await db.flush()
    # `updated_at` is server-side, so the row is expired by the flush above and
    # must be reloaded before it is serialized.
    member = await _reload_member(db, member.id)
    return _photo_response(
        "Member photo uploaded successfully",
        url,
        role=role,
        member=_member_list_item(member, ProfileService(db)),
    )


@router.delete("/{member_id}/photo/{role}", dependencies=[UPDATE])
async def delete_member_photo(
    member_id: UUID,
    role: str,
    request: Request,
    db: DBSession,
    current_user: CurrentUser,
):
    """Remove one photo: clear its URL from the member, then delete the stored
    object and its media row (best effort - storage trouble is logged, not
    turned into a failed request)."""
    if role not in SLOTS:
        raise BadRequestException(
            f"Unknown photo slot '{role}'; use 'member' or 'spouse'",
            "INVALID_PHOTO",
        )
    member = await _get_member(db, member_id)
    if member is None:
        raise NotFoundException("Member", str(member_id))
    await assert_group_access(db, current_user, member.group_id)

    ip_address = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")
    await MemberPhotoService(db).remove(member, role=role)
    await AuditService(db).log(
        AuditAction.MEMBER_UPDATE,
        user_id=current_user.id,
        entity_type="member",
        entity_id=member.id,
        details={"field": f"{role}_photo", "photo_url": None},
        ip_address=ip_address,
        user_agent=user_agent,
    )
    await db.flush()
    return _photo_response("Member photo deleted successfully", None, role=role)


@router.post("/{member_id}/approve")
async def approve_member(
    member_id: UUID,
    body: MemberApproveRequest,
    db: DBSession,
    current_user=APPROVE,
):
    repo = MemberRepository(db)
    member = await repo.get_by_id(member_id)
    if member is None:
        raise NotFoundException("Member", str(member_id))
    await assert_group_access(db, current_user, member.group_id)
    if body.status in (
        MemberStatus.APPROVED,
        MemberStatus.REJECTED,
        MemberStatus.BLOCKED,
    ):
        if body.status != MemberStatus.APPROVED and not body.rejection_reason:
            raise BadRequestException("A rejection reason is required")
        member.membership_status = body.status.value
        if body.status == MemberStatus.APPROVED:
            member.approved_at = datetime.now(UTC)
            member.joined_at = datetime.now(UTC)
            member.approved_by = current_user.id
            member.rejection_reason = None
        else:
            member.rejection_reason = body.rejection_reason
        action = (
            AuditAction.MEMBER_APPROVE
            if body.status == MemberStatus.APPROVED
            else AuditAction.MEMBER_REJECT
            if body.status == MemberStatus.REJECTED
            else AuditAction.MEMBER_BLOCK
        )
        await AuditService(db).log(
            action, user_id=current_user.id, entity_type="member", entity_id=member.id
        )
        await db.flush()
        await db.refresh(member)
    else:
        raise BadRequestException("Use update endpoint for status changes")
    return ok("Member status updated successfully", MemberOut.model_validate(member))


@router.delete("/{member_id}", dependencies=[DELETE])
async def delete_member(member_id: UUID, current_user: CurrentUser, db: DBSession):
    repo = MemberRepository(db)
    member = await repo.get_by_id(member_id)
    if member is None:
        raise NotFoundException("Member", str(member_id))
    await assert_group_access(db, current_user, member.group_id)
    # Soft delete first: if object storage cannot be reached, the member is
    # still gone from the API and only an orphan object is left behind.
    await repo.delete(member)
    await MemberPhotoService(db).remove_all(member)
    await db.flush()
    return ok("Member deleted successfully")
