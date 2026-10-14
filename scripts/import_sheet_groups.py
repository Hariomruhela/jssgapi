"""Save the sheet's and members' group names into the ``social_groups`` table.

The Groups API reads ``social_groups``, but the group names arrived as free
text - on the registration sheet's ``social_group_name`` column and in each
member's ``profile_data`` - so most never had a row and the API returned only
the few groups that had been created by hand. This command materializes every
distinct name, links matching members to their group, and (optionally) purges
groups that were previously soft-deleted.

Run from the project root:

    # report what would be created, write nothing
    .venv/bin/python -m scripts.import_sheet_groups --dry-run

    # import the groups and link members
    .venv/bin/python -m scripts.import_sheet_groups --apply

    # also physically remove soft-deleted groups (e.g. test groups)
    .venv/bin/python -m scripts.import_sheet_groups --apply --purge-deleted

The import is idempotent: a name that already exists (ignoring case and
padding) is not duplicated, so the command is safe to re-run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from sqlalchemy import select

from app.database import async_session_factory, engine
from app.models.group import SocialGroup
from app.repositories.group_repository import GroupRepository
from app.services.sheet_groups_service import import_member_groups


async def _run(
    apply_changes: bool,
    include_sheet: bool,
    include_member_data: bool,
    link_members: bool,
    purge_deleted: bool,
) -> int:
    async with async_session_factory() as session:
        summary = await import_member_groups(
            session,
            sheet_names=None if include_sheet else (),
            include_member_data=include_member_data,
            link_members=link_members,
        )

        purged: list[str] = []
        if purge_deleted:
            repo = GroupRepository(session)
            deleted = (
                await session.execute(
                    select(SocialGroup).where(SocialGroup.is_deleted.is_(True))
                )
            ).scalars()
            for group in list(deleted):
                purged.append(group.name)
                await repo.hard_delete(group)

        if apply_changes:
            await session.commit()
        else:
            await session.rollback()

    report = summary.to_dict()
    report["dry_run"] = not apply_changes
    report["purged_deleted"] = purged
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if not apply_changes:
        print("Dry run: nothing was written. Re-run with --apply.")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="scripts.import_sheet_groups",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be created without writing anything (default).",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Actually create the groups and link members.",
    )
    parser.add_argument(
        "--no-sheet",
        action="store_true",
        help="Do not read the Google Sheet; use only member profile data.",
    )
    parser.add_argument(
        "--no-members",
        action="store_true",
        help="Do not read group names from member profile data.",
    )
    parser.add_argument(
        "--no-link",
        action="store_true",
        help="Create groups but do not link members to them.",
    )
    parser.add_argument(
        "--purge-deleted",
        action="store_true",
        help="Permanently remove groups that are already soft-deleted.",
    )
    return parser.parse_args(argv)


async def main(args: argparse.Namespace) -> int:
    try:
        return await _run(
            apply_changes=args.apply,
            include_sheet=not args.no_sheet,
            include_member_data=not args.no_members,
            link_members=not args.no_link,
            purge_deleted=args.purge_deleted,
        )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    if not sys.argv[1:]:
        print("No arguments given. Use --dry-run, --apply or --help.")
        raise SystemExit(2)
    raise SystemExit(asyncio.run(main(parse_args())))
