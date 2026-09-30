"""Copy member photos from Google Drive into Cloudflare R2.

The member directory was seeded from a Google Sheet whose photo column holds
``drive.google.com`` share links. Those links cannot be rendered by the mobile
app: an unshared file resolves to an ``accounts.google.com`` sign-in page, and a
shared one resolves to Drive's HTML preview. ``Image.network`` gets neither.

This service fetches the bytes once and stores them under the same project
storage that serves uploads, then points the member's photo fields at a URL that
returns ``image/*`` with no cookies and no headers.

Design notes:

* **Nothing in Drive changes.** No file is shared, moved, renamed or deleted.
* **The original link is kept** in ``*_photo_drive_url`` so a photo stays
  re-syncable from the sheet.
* **Idempotent.** A photo is migrated at most once; re-running reports it as
  ``already_migrated`` and re-uploads nothing.
* **One failure never stops the run.** Every member is migrated in its own
  nested transaction, and an unreadable Drive file is recorded and skipped.
* **Reuses the existing storage backend** - there is no second R2 client, and
  no new upload path.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import AuditAction, MediaType
from app.core.exceptions import ServiceUnavailableException
from app.models.media import Media
from app.models.member import Member
from app.repositories.media_repository import MediaRepository
from app.services.audit_service import AuditService
from app.services.google_drive_photos import (
    DrivePhotoDownloader,
    DrivePhotoError,
    PhotoLinkKind,
    is_direct_image_url,
    parse_photo_link,
)
from app.services.media_service import build_media_url
from app.services.media_storage import StorageBackend, get_storage

logger = logging.getLogger(__name__)

#: Deterministic object key prefix per member and photo slot. The extension is
#: added once the bytes have been inspected, so the prefix is what makes the
#: migration idempotent even across runs.
OBJECT_KEY_PREFIX = "members/{member_id}/photos/{role}"

#: Photos are read and copied one at a time; a serverless invocation has a wall
#: clock, so a single call is bounded rather than allowed to walk the directory.
DEFAULT_LIMIT = 50
MAX_LIMIT = 200

MIGRATED = "migrated"
ALREADY_MIGRATED = "already_migrated"
SKIPPED = "skipped"
FAILED = "failed"


@dataclass(frozen=True)
class PhotoField:
    """The column and JSONB entries backing one photo slot on a member."""

    role: str
    drive_attr: str
    key_attr: str
    url_attr: str
    json_key: str
    mirror_to_profile_photo_url: bool


PHOTO_FIELDS: tuple[PhotoField, ...] = (
    PhotoField(
        role="member",
        drive_attr="profile_photo_drive_url",
        key_attr="profile_photo_r2_object_key",
        url_attr="profile_photo_r2_url",
        json_key="member_photo_link",
        mirror_to_profile_photo_url=True,
    ),
    PhotoField(
        role="spouse",
        drive_attr="spouse_photo_drive_url",
        key_attr="spouse_photo_r2_object_key",
        url_attr="spouse_photo_r2_url",
        json_key="spouse_photo_link",
        mirror_to_profile_photo_url=False,
    ),
)


@dataclass
class PhotoOutcome:
    member_id: str
    role: str
    status: str
    reason: str | None = None
    source_url: str | None = None
    object_key: str | None = None
    r2_url: str | None = None
    file_name: str | None = None
    mime_type: str | None = None


@dataclass
class MigrationSummary:
    dry_run: bool = False
    scanned: int = 0
    migrated: int = 0
    already_migrated: int = 0
    skipped: int = 0
    failed: int = 0
    outcomes: list[PhotoOutcome] = field(default_factory=list)
    has_more: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "scanned": self.scanned,
            "migrated": self.migrated,
            "already_migrated": self.already_migrated,
            "skipped": self.skipped,
            "failed": self.failed,
            "has_more": self.has_more,
            "failures": [asdict(o) for o in self.outcomes if o.status == FAILED],
            "outcomes": [asdict(o) for o in self.outcomes],
        }


def _member_source_link(member: Member, field_: PhotoField) -> str:
    """The Drive link to copy from, for this slot.

    ``profile_data`` wins when it holds a Drive link, because the read path in
    ``profile_service`` overlays it on top of ``profile_photo_url`` - so it is
    the link the app is actually being served.

    Once a photo is migrated those fields hold the R2 URL instead, so the
    preserved ``*_drive_url`` is consulted next. That is what lets a photo whose
    stored object later disappears be copied again.
    """
    candidates: list[str] = []
    profile_data = member.profile_data or {}
    json_value = profile_data.get(field_.json_key)
    if isinstance(json_value, str):
        candidates.append(json_value.strip())
    if field_.mirror_to_profile_photo_url:
        candidates.append((member.profile_photo_url or "").strip())
    candidates.append((getattr(member, field_.drive_attr) or "").strip())

    for candidate in candidates:
        if parse_photo_link(candidate).kind is PhotoLinkKind.FILE_ID:
            return candidate
    return candidates[0] if candidates else ""


class DrivePhotoMigration:
    """Runs the Drive to R2 photo migration over members."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        storage: StorageBackend | None = None,
        downloader: DrivePhotoDownloader | None = None,
        base_url: str | None = None,
    ) -> None:
        self.session = session
        self.storage = storage or get_storage()
        self.downloader = downloader or DrivePhotoDownloader()
        self.media_repo = MediaRepository(session)
        self.audit = AuditService(session)
        self.base_url = base_url

    # ------------------------------------------------------------------
    async def pending_members(self, offset: int, limit: int) -> list[Member]:
        """Members with a photo that is not recorded as migrated yet.

        Both slots are considered: a member whose photo is done but whose
        spouse photo is not still needs a pass. The sheet writes the member
        photo link to both ``profile_photo_url`` and ``profile_data``, so both
        are checked.
        """
        has_link = [
            Member.profile_data[field_.json_key].as_string().isnot(None)
            for field_ in PHOTO_FIELDS
        ]
        has_link.append(Member.profile_photo_url.isnot(None))
        has_link.append(Member.profile_photo_url != "")

        key_columns = [getattr(Member, field_.key_attr) for field_ in PHOTO_FIELDS]
        not_migrated = or_(
            *[column.is_(None) for column in key_columns],
            *[column == "" for column in key_columns],
        )

        stmt = (
            select(Member)
            .where(or_(*has_link), not_migrated)
            .order_by(Member.id)
            .offset(offset)
            .limit(limit + 1)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().unique())

    # ------------------------------------------------------------------
    async def run(
        self,
        *,
        dry_run: bool = False,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
        member_id: uuid.UUID | None = None,
        user_id: uuid.UUID | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> MigrationSummary:
        summary = MigrationSummary(dry_run=dry_run)

        if not dry_run and not self.storage.available:
            raise ServiceUnavailableException(
                self.storage.unavailable_message, "MEDIA_STORAGE_UNAVAILABLE"
            )

        members = (
            [m for m in [await self._member(member_id)] if m is not None]
            if member_id
            else await self.pending_members(offset, min(limit, MAX_LIMIT))
        )
        if len(members) > limit:
            summary.has_more = True
            members = members[:limit]

        for member in members:
            summary.scanned += 1
            # Each member is its own transaction: a bad photo or a failed write
            # is rolled back on its own and the remaining members still commit.
            async with self.session.begin_nested():
                for field_ in PHOTO_FIELDS:
                    outcome = await self._migrate_one(member, field_, dry_run=dry_run)
                    if outcome is None:
                        continue
                    summary.outcomes.append(outcome)
                    setattr(
                        summary, outcome.status, getattr(summary, outcome.status) + 1
                    )

        if not dry_run:
            await self.session.flush()
            await self.audit.log(
                AuditAction.MEDIA_MIGRATE,
                user_id=user_id,
                entity_type="member",
                details={
                    "scanned": summary.scanned,
                    "migrated": summary.migrated,
                    "already_migrated": summary.already_migrated,
                    "skipped": summary.skipped,
                    "failed": summary.failed,
                },
                ip_address=ip_address,
                user_agent=user_agent,
            )
        return summary

    async def _member(self, member_id: uuid.UUID) -> Member | None:
        return await self.session.get(Member, member_id)

    # ------------------------------------------------------------------
    async def _migrate_one(
        self, member: Member, field_: PhotoField, *, dry_run: bool
    ) -> PhotoOutcome | None:
        member_id = str(member.id)
        source_url = _member_source_link(member, field_)
        recorded_key = (getattr(member, field_.key_attr) or "").strip()
        recorded_url = (getattr(member, field_.url_attr) or "").strip()

        def outcome(
            status: str,
            reason: str | None = None,
            **extra: Any,
        ) -> PhotoOutcome:
            return PhotoOutcome(
                member_id=member_id,
                role=field_.role,
                status=status,
                reason=reason,
                source_url=source_url or None,
                **extra,
            )

        # The recorded key is checked before the link is classified. After a
        # migration the stored link is the R2 URL, not the Drive link, so
        # classifying first would report a finished photo as "not a Drive
        # link" and hide whether its bytes are actually there.
        if recorded_key:
            if await asyncio.to_thread(self.storage.object_exists, recorded_key):
                return outcome(
                    ALREADY_MIGRATED,
                    "already migrated",
                    object_key=recorded_key,
                    r2_url=recorded_url or self._public_url(recorded_key),
                )
            # The row claims a migration but the bytes are gone; re-copy them.
            logger.warning(
                "recorded R2 object %s for member %s %s is gone; re-migrating",
                recorded_key,
                member_id,
                field_.role,
            )
            recorded_key = ""
            recorded_url = ""

        if not source_url:
            # Nothing stored for this slot. Not worth reporting against every
            # member with no photo.
            return None

        if is_direct_image_url(source_url):
            return outcome(
                SKIPPED,
                "already a direct image URL that serves bytes unauthenticated",
            )

        link = parse_photo_link(source_url)
        if link.kind is not PhotoLinkKind.FILE_ID:
            return outcome(SKIPPED, "not a Google Drive file link")

        existing = recorded_key or await asyncio.to_thread(
            self.storage.find_object_key, self._prefix(member, field_)
        )
        if existing:
            r2_url = await self._finish(
                member,
                field_,
                object_key=existing,
                r2_url=recorded_url or self._public_url(existing),
                source_url=source_url,
                dry_run=dry_run,
            )
            return outcome(
                ALREADY_MIGRATED,
                "object already present in storage",
                object_key=existing,
                r2_url=r2_url,
            )

        if dry_run:
            return outcome(MIGRATED, "would download from Drive and store in R2")

        try:
            photo = await asyncio.to_thread(self.downloader.download, link, field_.role)
        except DrivePhotoError as exc:
            return outcome(FAILED, exc.reason)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "photo download failed for member %s %s",
                member_id,
                field_.role,
                exc_info=True,
            )
            return outcome(FAILED, f"unexpected download error: {exc}")

        object_key = self._object_key(member, field_, photo.file_name)
        await asyncio.to_thread(
            self.storage.upload_bytes,
            object_key,
            photo.content,
            photo.mime_type,
        )

        await self._record_media(
            member,
            field_,
            object_key=object_key,
            file_name=photo.file_name,
            mime_type=photo.mime_type,
            size=len(photo.content),
        )

        r2_url = await self._finish(
            member,
            field_,
            object_key=object_key,
            r2_url=self._public_url(object_key),
            source_url=source_url,
            dry_run=dry_run,
        )
        return outcome(
            MIGRATED,
            object_key=object_key,
            r2_url=r2_url,
            file_name=photo.file_name,
            mime_type=photo.mime_type,
        )

    # ------------------------------------------------------------------
    def _prefix(self, member: Member, field_: PhotoField) -> str:
        return OBJECT_KEY_PREFIX.format(member_id=member.id, role=field_.role)

    def _object_key(self, member: Member, field_: PhotoField, file_name: str) -> str:
        suffix = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else "img"
        suffix = "".join(ch for ch in suffix if ch.isalnum()) or "img"
        return f"{self._prefix(member, field_)}.{suffix}"

    def _public_url(self, object_key: str) -> str:
        if self.storage.serves_public_urls:
            public_url = getattr(self.storage, "public_url", "")
            return f"{public_url.rstrip('/')}/{object_key}"
        return ""

    async def _record_media(
        self,
        member: Member,
        field_: PhotoField,
        *,
        object_key: str,
        file_name: str,
        mime_type: str,
        size: int,
    ) -> None:
        """Track the object in ``media`` so the normal route can serve it.

        The object key is unique, so an earlier row for the same photo is
        reused rather than duplicated.
        """
        existing = await self.media_repo.get_by_object_key(object_key)
        public_url = self._public_url(object_key)
        if existing is not None:
            existing.url = public_url or existing.url
            existing.file_size = size
            existing.mime_type = mime_type
            existing.file_name = file_name
            return

        media = Media(
            owner_type="member",
            owner_id=member.id,
            file_name=file_name,
            file_type=MediaType.IMAGE.value,
            mime_type=mime_type,
            file_size=size,
            r2_object_key=object_key,
            url=public_url,
            is_public=True,
            uploaded_by=None,
        )
        self.session.add(media)
        await self.session.flush()
        if not public_url:
            # No public bucket domain, so the photo is served by the media
            # content route instead.
            media.url = build_media_url(media.id, self.base_url)

    async def _finish(
        self,
        member: Member,
        field_: PhotoField,
        *,
        object_key: str,
        r2_url: str,
        source_url: str,
        dry_run: bool,
    ) -> str:
        """Write the migrated link onto the member.

        The original Drive link is preserved in ``*_photo_drive_url``; the
        serving URL is written both to the dedicated column and to the field
        the app reads (``profile_photo_url`` / ``profile_data``).
        """
        if dry_run:
            return r2_url

        setattr(member, field_.key_attr, object_key)
        if r2_url:
            setattr(member, field_.url_attr, r2_url)
            setattr(member, field_.drive_attr, source_url)
            profile_data = dict(member.profile_data or {})
            profile_data[field_.json_key] = r2_url
            member.profile_data = profile_data
            if field_.mirror_to_profile_photo_url:
                member.profile_photo_url = r2_url
        return r2_url
