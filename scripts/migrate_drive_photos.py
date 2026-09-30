"""Copy member photos from Google Drive into Cloudflare R2.

The member directory was seeded from a Google Sheet whose photo column holds
``drive.google.com`` share links. Those links cannot be rendered by the mobile
app - an unshared file resolves to an ``accounts.google.com`` sign-in page and a
shared one to Drive's HTML preview - so the bytes are copied into the project's
R2 bucket and the member's photo fields are repointed at them.

This is the batch version of ``POST /api/v1/media/migrate-drive-photos``. Use
it when the directory is larger than one request can process in a serverless
invocation; the endpoint is the better choice for a small, one-off migration.

Run from the project root:

    # see what would happen, change nothing
    .venv/bin/python -m scripts.migrate_drive_photos --dry-run

    # migrate the first batch for real
    .venv/bin/python -m scripts.migrate_drive_photos --apply --limit 50

    # keep going until nothing is left
    .venv/bin/python -m scripts.migrate_drive_photos --apply --all

    # one member
    .venv/bin/python -m scripts.migrate_drive_photos --apply --member-id <uuid>

The command is idempotent: a photo is copied at most once, and a second pass
reports it as ``already_migrated`` without re-uploading. Nothing in Drive is
modified - no file is shared, renamed, moved or deleted - and the original link
is kept on the member as ``*_photo_drive_url``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid

from app.database import async_session_factory
from app.services.drive_photo_migration import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    DrivePhotoMigration,
)


async def _run_batch(
    apply_changes: bool, limit: int, offset: int, member_id: str | None
) -> bool:
    """One batch. Returns True when more work may remain."""
    async with async_session_factory() as session:
        migration = DrivePhotoMigration(session)
        summary = await migration.run(
            dry_run=not apply_changes,
            limit=limit,
            offset=offset,
            member_id=uuid.UUID(member_id) if member_id else None,
        )
        if apply_changes:
            await session.commit()
        else:
            await session.rollback()
        print(json.dumps(summary.to_dict(), indent=2))
        return summary.has_more or summary.migrated > 0


async def main(args: argparse.Namespace) -> int:
    if not args.all:
        await _run_batch(args.apply, args.limit, args.offset, args.member_id)
        return 0

    # A migration run is not blocked by an unreadable photo, but a deployment
    # error (storage misconfigured, database unreachable) should stop the loop
    # rather than spin through every batch.
    batches = 0
    while batches < args.max_batches:
        batches += 1
        more = await _run_batch(True, args.limit, args.offset, None)
        if not more:
            break
    print(f"stopped after {batches} batch(es)")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="scripts.migrate_drive_photos",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing anything (default).",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Actually copy the photos and update the members.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"Members per batch (default {DEFAULT_LIMIT}, max {MAX_LIMIT}).",
    )
    parser.add_argument("--offset", type=int, default=0, help="Skip this many members.")
    parser.add_argument(
        "--all",
        action="store_true",
        help="Keep processing batches until nothing is pending.",
    )
    parser.add_argument(
        "--max-batches",
        type=int,
        default=100,
        help="Safety stop for --all (default 100).",
    )
    parser.add_argument(
        "--member-id", default=None, help="Migrate a single member by id."
    )
    args = parser.parse_args(argv)
    if args.member_id:
        try:
            uuid.UUID(args.member_id)
        except ValueError:
            parser.error("--member-id must be a UUID")
        args.all = False
    return args


if __name__ == "__main__":
    if not sys.argv[1:]:
        print("No arguments given. Use --dry-run, --apply, --all or --help.")
        raise SystemExit(2)
    raise SystemExit(asyncio.run(main(parse_args())))
