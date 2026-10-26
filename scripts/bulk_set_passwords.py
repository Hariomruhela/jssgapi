#!/usr/bin/env python3
"""One-time script: set default password for ALL members.

For members WITH a linked user account   → update password_hash
For members WITHOUT a user account       → create a User record, link it, update password_hash

Default password: Pass@1234

Usage:
    cd /home/hariom/Desktop/jssgapi
    python -m scripts.bulk_set_passwords          # dry-run (default)
    python -m scripts.bulk_set_passwords --apply   # actually write to DB
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Ensure the project root is on sys.path so `app` is importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.constants import MemberStatus, RoleName
from app.core.security import hash_password
from app.database import engine as async_engine, async_session_factory
from app.models.member import Member
from app.models.user import Role, User

DEFAULT_PASSWORD = "Pass@1234"


async def _get_member_role(session: AsyncSession) -> Role:
    """Fetch (or create) the MEMBER role row."""
    result = await session.execute(
        select(Role).where(Role.name == RoleName.MEMBER.value)
    )
    role = result.scalar_one_or_none()
    if role is None:
        role = Role(name=RoleName.MEMBER.value, description="Community member")
        session.add(role)
        await session.flush()
    return role


async def run(*, apply: bool = False) -> None:
    settings = get_settings()
    password_hash = hash_password(DEFAULT_PASSWORD)
    print(f"Database : {settings.database_url[:60]}…")
    print(f"Password : {DEFAULT_PASSWORD}")
    print(f"Mode     : {'APPLY (writing to DB)' if apply else 'DRY-RUN (no changes)'}")
    print()

    async with async_session_factory() as session:
        member_role = await _get_member_role(session)

        # ── 1. Members WITHOUT a user account (Google Sheets imports) ──
        result = await session.execute(
            select(Member).where(Member.user_id.is_(None))
        )
        orphan_members: list[Member] = list(result.scalars().all())
        print(f"Members without user account: {len(orphan_members)}")

        created_count = 0
        skipped_no_phone = 0
        skipped_dup_phone = 0
        for m in orphan_members:
            phone = m.contact_phone
            if not phone or not phone.strip():
                skipped_no_phone += 1
                continue

            phone = phone.strip()
            # Make sure no existing user already has this phone
            existing = await session.execute(
                select(User).where(User.phone_number == phone)
            )
            if existing.scalar_one_or_none() is not None:
                skipped_dup_phone += 1
                print(f"  SKIP (dup phone) member={m.id}  phone={phone}")
                continue

            # Check email uniqueness too (email column has UNIQUE constraint)
            email = m.contact_email.strip().lower() if m.contact_email else None
            if email:
                dup_email = await session.execute(
                    select(User).where(User.email == email)
                )
                if dup_email.scalar_one_or_none() is not None:
                    email = None  # skip email, still create user with phone only

            full_name = " ".join(
                p for p in [m.first_name, m.middle_name, m.last_name] if p
            ).strip() or "Member"

            if apply:
                user = User(
                    email=email,
                    phone_number=phone,
                    password_hash=password_hash,
                    full_name=full_name,
                    is_active=True,
                    is_email_verified=False,
                    is_phone_verified=True,
                    role_id=member_role.id,
                    group_id=m.group_id,
                )
                session.add(user)
                await session.flush()
                m.user_id = user.id
                # Mark as APPROVED since these are existing members
                m.membership_status = MemberStatus.APPROVED.value
                await session.flush()
            created_count += 1
            print(f"  {'CREATE' if apply else 'WOULD CREATE'} user for member={m.id}  phone={phone}  name={full_name}")

        print(f"\nUsers created for orphan members: {created_count}")
        print(f"Skipped (no phone)             : {skipped_no_phone}")
        print(f"Skipped (duplicate phone)      : {skipped_dup_phone}")

        # ── 2. Update ALL existing users' passwords ──
        result = await session.execute(select(User))
        all_users: list[User] = list(result.scalars().all())
        print(f"\nTotal users to update password : {len(all_users)}")

        updated_count = 0
        for u in all_users:
            if apply:
                u.password_hash = password_hash
            updated_count += 1

        if apply:
            await session.flush()
        print(f"Passwords updated              : {updated_count}")

        # ── Commit or rollback ──
        if apply:
            await session.commit()
            print("\n✅ All changes committed successfully.")
        else:
            await session.rollback()
            print("\n⚠️  DRY-RUN: no changes were made. Run with --apply to write.")


def main():
    parser = argparse.ArgumentParser(
        description="Set default password for all members (creating user accounts where needed)"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write changes to the database (default is dry-run)",
    )
    args = parser.parse_args()
    asyncio.run(run(apply=args.apply))


if __name__ == "__main__":
    main()
