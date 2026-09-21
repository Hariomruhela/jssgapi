"""Promote an existing user to the ADMIN role.

Safe way to create/promote the FIRST admin without exposing a public
"make me admin" API. Run from the project root:

    .venv/bin/python -m scripts.promote_admin --phone +919876543210
    .venv/bin/python -m scripts.promote_admin --email someone@example.com --role ADMIN

The target user must already exist (e.g. an account created via signup).
"""
from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import select

from app.core.constants import RoleName
from app.core.exceptions import NotFoundException
from app.database import async_session_factory
from app.models.user import Role, User


async def promote(identifier: str, is_email: bool, role_name: str) -> None:
    async with async_session_factory() as session:
        role_result = await session.execute(
            select(Role).where(Role.name == role_name)
        )
        role = role_result.scalar_one_or_none()
        if role is None:
            available = "".join(f"  - {r.value}\n" for r in RoleName)
            raise NotFoundException(
                f"Role '{role_name}' does not exist. Known roles:\n{available}"
            )

        if is_email:
            user_result = await session.execute(
                select(User).where(User.email == identifier.lower().strip())
            )
        else:
            user_result = await session.execute(
                select(User).where(User.phone_number == identifier)
            )
        user = user_result.scalar_one_or_none()
        if user is None:
            raise NotFoundException("User")

        user.role_id = role.id
        await session.commit()
        print(
            f"Promoted {user.full_name!r} ({user.email or user.phone_number}) "
            f"to {role_name}"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Promote a user to ADMIN")
    parser.add_argument("--phone", help="Phone number of the target user")
    parser.add_argument("--email", help="Email of the target user")
    parser.add_argument(
        "--role", default="ADMIN", help="Role name to assign (default: ADMIN)"
    )
    args = parser.parse_args()

    if bool(args.phone) == bool(args.email):
        parser.error("Provide exactly one of --phone or --email")

    asyncio.run(
        promote(
            args.phone or args.email,
            is_email=bool(args.email),
            role_name=args.role,
        )
    )
