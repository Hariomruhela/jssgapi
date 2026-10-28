from __future__ import annotations

import asyncio
import logging
from sqlalchemy import select, text
from sqlalchemy.orm import selectinload

from app.database import async_session_factory
from app.models.member import Member
from app.services.profile_service import ProfileService

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("backfill")

async def run_backfill():
    async with async_session_factory() as session:
        # Load raw column values for all members
        raw_res = await session.execute(text("""
            SELECT id,
                   unmarried_son1_details,
                   dob_of_unmarried_son1,
                   unmarried_son1_educational_qualification,
                   unmarried_son1_occupation_profession,
                   dob_of_unmarried_daughter1,
                   unmarried_daughter1_educational_qualification,
                   unmarried_daughter1_occupation_profession,
                   unmarried_son2_details,
                   dob_of_unmarried_son2,
                   unmarried_son2_educational_qualification,
                   unmarried_son2_occupation_profession,
                   dob_of_unmarried_daughter2,
                   unmarried_daughter2_educational_qualification,
                   unmarried_daughter2_occupation_profession,
                   https_photos_app_goo_gl_s6xedsvhxdwxz2jg9
            FROM members
        """))
        raw_rows = {str(r["id"]): dict(r) for r in raw_res.mappings().all()}
        logger.info(f"Loaded {len(raw_rows)} raw database rows.")

        result = await session.execute(
            select(Member).options(selectinload(Member.family_members))
        )
        members = result.scalars().all()
        logger.info(f"Loaded {len(members)} ORM members.")

        profiles = ProfileService(session)
        updated_count = 0
        photo_fixed_count = 0

        for member in members:
            mid = str(member.id)
            raw = raw_rows.get(mid, {})
            values_to_sync = {}
            profile_data = member.profile_data or {}

            # --- Son 1 ---
            son1_name = raw.get("unmarried_son1_details")
            if son1_name and not profile_data.get("son_name"):
                values_to_sync["son_name"] = str(son1_name).strip()
                values_to_sync["unmarried_son1_details"] = str(son1_name).strip()

            son1_dob = raw.get("dob_of_unmarried_son1")
            if son1_dob and not profile_data.get("son_dob"):
                values_to_sync["son_dob"] = str(son1_dob).strip()
                values_to_sync["unmarried_son1_dob"] = str(son1_dob).strip()

            son1_edu = raw.get("unmarried_son1_educational_qualification")
            if son1_edu and not profile_data.get("son_education"):
                values_to_sync["son_education"] = str(son1_edu).strip()
                values_to_sync["unmarried_son1_education"] = str(son1_edu).strip()

            son1_occ = raw.get("unmarried_son1_occupation_profession")
            if son1_occ and not profile_data.get("son_occupation"):
                values_to_sync["son_occupation"] = str(son1_occ).strip()
                values_to_sync["unmarried_son1_occupation"] = str(son1_occ).strip()

            # --- Daughter 1 ---
            d1_dob = raw.get("dob_of_unmarried_daughter1")
            if d1_dob and not profile_data.get("daughter_dob"):
                values_to_sync["daughter_dob"] = str(d1_dob).strip()
                values_to_sync["unmarried_daughter1_dob"] = str(d1_dob).strip()

            d1_edu = raw.get("unmarried_daughter1_educational_qualification")
            if d1_edu and not profile_data.get("daughter_education"):
                values_to_sync["daughter_education"] = str(d1_edu).strip()
                values_to_sync["unmarried_daughter1_education"] = str(d1_edu).strip()

            d1_occ = raw.get("unmarried_daughter1_occupation_profession")
            if d1_occ and not profile_data.get("daughter_occupation"):
                values_to_sync["daughter_occupation"] = str(d1_occ).strip()
                values_to_sync["unmarried_daughter1_occupation"] = str(d1_occ).strip()

            # --- Son 2 ---
            son2_name = raw.get("unmarried_son2_details")
            if son2_name and not profile_data.get("unmarried_son2_details"):
                values_to_sync["unmarried_son2_details"] = str(son2_name).strip()

            son2_dob = raw.get("dob_of_unmarried_son2")
            if son2_dob and not profile_data.get("unmarried_son2_dob"):
                values_to_sync["unmarried_son2_dob"] = str(son2_dob).strip()

            son2_edu = raw.get("unmarried_son2_educational_qualification")
            if son2_edu and not profile_data.get("unmarried_son2_education"):
                values_to_sync["unmarried_son2_education"] = str(son2_edu).strip()

            son2_occ = raw.get("unmarried_son2_occupation_profession")
            if son2_occ and not profile_data.get("unmarried_son2_occupation"):
                values_to_sync["unmarried_son2_occupation"] = str(son2_occ).strip()

            # --- Daughter 2 ---
            d2_name = raw.get("unmarried_daughter2_details")
            if d2_name and not profile_data.get("unmarried_daughter2_details"):
                values_to_sync["unmarried_daughter2_details"] = str(d2_name).strip()

            d2_dob = raw.get("dob_of_unmarried_daughter2")
            if d2_dob and not profile_data.get("unmarried_daughter2_dob"):
                values_to_sync["unmarried_daughter2_dob"] = str(d2_dob).strip()

            d2_edu = raw.get("unmarried_daughter2_educational_qualification")
            if d2_edu and not profile_data.get("unmarried_daughter2_education"):
                values_to_sync["unmarried_daughter2_education"] = str(d2_edu).strip()

            d2_occ = raw.get("unmarried_daughter2_occupation_profession")
            if d2_occ and not profile_data.get("unmarried_daughter2_occupation"):
                values_to_sync["unmarried_daughter2_occupation"] = str(d2_occ).strip()

            # --- Photos (fix missing member photos from extra column) ---
            extra_photo = raw.get("https_photos_app_goo_gl_s6xedsvhxdwxz2jg9")
            if extra_photo and not member.profile_photo_url and not profile_data.get("member_photo_link"):
                values_to_sync["member_photo_link"] = str(extra_photo).strip()
                member.profile_photo_url = str(extra_photo).strip()
                photo_fixed_count += 1

            if values_to_sync:
                await profiles.apply_values(member, values_to_sync)
                updated_count += 1
                logger.info(f"Updated member {member.first_name} {member.last_name} ({member.id}) with {len(values_to_sync)} fields.")

        await session.commit()
        logger.info(f"Backfill finished! Total members updated: {updated_count}, Missing photos fixed: {photo_fixed_count}")

if __name__ == "__main__":
    asyncio.run(run_backfill())
