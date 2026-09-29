from __future__ import annotations

import logging
import re
import secrets
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.constants import MemberStatus, RoleName
from app.core.exceptions import BadRequestException
from app.core.security import hash_password
from app.models.member import Member
from app.models.user import Role, User
from app.schemas.registration import (
    RowIssue,
    RowResult,
    SyncGoogleSheetRequest,
    SyncGoogleSheetResponse,
)
from app.services.audit_service import AuditService
from app.services.google_sheets_service import GoogleSheetsService, SheetTable
from app.services.profile_service import PROFILE_FIELDS, ProfileService
from app.utils.validators import normalize_phone_number, validate_email

logger = logging.getLogger(__name__)

SYNC_AUDIT_ACTION = "REGISTRATION_SYNC"

# Rows / issues echoed back to the caller; the counters are always complete.
PREVIEW_LIMIT = 200

NAME_MAX = 255
PHONE_MAX = 20
EMAIL_MAX = 255
TEXT_MAX = 255
ADDRESS_MAX = 2000

# Sheet column (normalised) -> registration field. Several aliases per field so
# small header rewordings do not break the import.
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "timestamp": ("timestamp", "submitted at", "submission time"),
    "full_name": ("full name", "name", "member name", "name of member"),
    "member_dob": (
        "date of birth member",
        "member date of birth",
        "dob member",
        "member dob",
    ),
    "spouse_dob": (
        "date of birth spouse",
        "spouse date of birth",
        "dob spouse",
        "spouse dob",
    ),
    "anniversary_date": (
        "date of anniverssary",
        "date of anniversary",
        "anniverssary date",
        "anniversary date",
        "marriage anniversary date",
    ),
    "member_photo_link": (
        "profile photo member",
        "member photo",
        "member photo link",
        "photo of member",
    ),
    "spouse_name": ("spouse name", "name of spouse"),
    "spouse_mobile": (
        "spouse mobile no",
        "spouse mobile number",
        "spouse mobile",
        "spouse contact number",
    ),
    "spouse_photo_link": (
        "profile photo spouse",
        "spouse photo",
        "spouse photo link",
        "photo of spouse",
    ),
    "family_member_count": (
        "number of family members",
        "no of family members",
        "family members",
        "family member count",
    ),
    "son_name": (
        "unmarried son details",
        "unmarried son",
        "son details",
        "son name",
    ),
    "son_dob": (
        "dob of unmarried son",
        "unmarried son dob",
        "dob of son",
        "son dob",
    ),
    "son_education": (
        "unmarried son educational qualification",
        "son educational qualification",
    ),
    "son_occupation": (
        "unmarried son occupation profession",
        "unmarried son occupation",
        "son occupation profession",
        "son occupation",
    ),
    "daughter_name": (
        "unmarried daughter details",
        "unmarried daughter",
        "daughter details",
        "daughter name",
    ),
    "daughter_dob": (
        "dob of unmarried daughter",
        "unmarried daughter dob",
        "dob of daughter",
        "daughter dob",
    ),
    "daughter_education": (
        "unmarried daughter educational qualification",
        "daughter educational qualification",
    ),
    "daughter_occupation": (
        "unmarried daughter occupation profession",
        "unmarried daughter occupation",
        "daughter occupation profession",
        "daughter occupation",
    ),
    "spouse_education": (
        "spouse educational qualification",
        "spouse education",
    ),
    "spouse_occupation": (
        "spouse occupation profession",
        "spouse occupation",
    ),
    "address": ("address location", "address", "residential address"),
    "city": ("city", "city name"),
    "area": ("area", "area locality", "locality"),
    "phone_number": (
        "mobile number",
        "mobile",
        "member mobile number",
        "contact number",
        "phone number",
        "contact mobile number",
    ),
    "email": ("email id", "email", "email address", "e mail id"),
    "member_education": (
        "member educational qualification",
        "member education",
        "educational qualification",
    ),
    "member_occupation": (
        "member occupation profession",
        "member occupation",
        "occupation profession",
        "occupation",
    ),
    "company_name": (
        "company business name",
        "company business",
        "business name",
        "company",
    ),
    "group_designation": ("group designation", "designation"),
    "social_group_name": (
        "social group association name",
        "social group association",
        "social group",
        "association name",
        "group name",
    ),
    "interest_fields": (
        "interest field of social activities",
        "interest field of social activity",
        "interest fields",
        "interest field",
        "areas of interest",
    ),
}

# A row cannot be imported without these (both are NOT NULL / login identity).
REQUIRED_FIELDS = ("full_name", "phone_number")

# dd/mm/yyyy first: this is an Indian community registration form. The trial
# Google Form is en-US (m/d/yyyy), so the order is re-detected from the data in
# RegistrationSyncService._detect_day_first() before any row is parsed.
UNAMBIGUOUS_DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%d %B %Y",
    "%d %b %Y",
    "%B %d, %Y",
    "%b %d, %Y",
)
DAY_FIRST_FORMATS = (
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%d.%m.%Y",
    "%d/%m/%y",
    "%d-%m-%y",
    *UNAMBIGUOUS_DATE_FORMATS,
)
MONTH_FIRST_FORMATS = (
    "%m/%d/%Y",
    "%m-%d-%Y",
    "%m.%d.%Y",
    "%m/%d/%y",
    "%m-%d-%y",
    *UNAMBIGUOUS_DATE_FORMATS,
)
DATE_FIELDS = frozenset(
    {"member_dob", "spouse_dob", "son_dob", "daughter_dob", "anniversary_date"}
)
AMBIGUOUS_DAY_MONTH = re.compile(r"^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})$")


def _matches_any(text: str, formats: tuple[str, ...]) -> bool:
    return any(_safe_strptime(text, fmt) is not None for fmt in formats)


def _safe_strptime(text: str, fmt: str) -> date | None:
    try:
        return datetime.strptime(text, fmt).date()
    except ValueError:
        return None


def normalize_header(value: str) -> str:
    """Lower-case and strip symbols so "Date of Anniverssary ->" normalises.

    Non-latin text is dropped, so the bilingual photo headers
    ("Profile Photo Spouse ... कृपया ... / Please enter Google Drive photo link")
    reduce to "profile photo spouse please enter google drive photo link" and are
    still matched by the alias-prefix lookup in :func:`build_column_index`.
    """
    cleaned = re.sub(r"[^a-z0-9]+", " ", value.lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def build_column_index() -> dict[str, list[str]]:
    """Normalised header -> registration field, for every alias of that field.

    Returns a list of normalised aliases per field so a sheet header can be
    matched by prefix ("profile photo spouse" matches "profile photo spouse
    please enter google drive photo link").
    """
    index: dict[str, list[str]] = {}
    for field_key, aliases in COLUMN_ALIASES.items():
        seen: list[str] = []
        for alias in aliases:
            normalized = normalize_header(alias)
            if normalized and normalized not in seen:
                seen.append(normalized)
        # Longest alias first so a specific header beats a generic one.
        index[field_key] = sorted(seen, key=len, reverse=True)
    return index


def match_field(column_index: dict[str, list[str]], header: str) -> str | None:
    """Return the field a header maps to, matching the longest alias prefix."""
    normalized = normalize_header(header)
    if not normalized:
        return None
    best: tuple[int, str] | None = None
    for field_key, aliases in column_index.items():
        for alias in aliases:
            if normalized == alias or normalized.startswith(f"{alias} "):
                if best is None or len(alias) > best[0]:
                    best = (len(alias), field_key)
                break
    return best[1] if best else None


def phone_key(value: str | None) -> str:
    """Digits-only comparison key so +91XXXXXXXXXX and XXXXXXXXXX match."""
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    return digits


def truncate(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    return value if len(value) <= limit else value[:limit]


def split_full_name(full_name: str) -> tuple[str, str]:
    parts = full_name.split()
    if not parts:
        return full_name, ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[-1]


@dataclass
class ParsedRow:
    row_number: int
    values: dict[str, str]
    full_name: str | None = None
    phone: str | None = None
    email: str | None = None
    member_dob: date | None = None
    dates: dict[str, date] = field(default_factory=dict)
    errors: list[RowIssue] = field(default_factory=list)
    warnings: list[RowIssue] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.errors

    def masked_phone(self) -> str | None:
        if not self.phone:
            return None
        return f"{self.phone[:5]}*****{self.phone[-3:]}"


class RegistrationSyncService:
    """Imports a registration Google Sheet into the existing users/members tables."""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.settings = get_settings()
        self.sheets = GoogleSheetsService()
        self.profiles = ProfileService(session)
        self.column_index = build_column_index()
        # dd/mm by default; _detect_day_first() overrides it from the sheet data.
        self.day_first = True

    # ------------------------------------------------------------------
    # reading + parsing
    # ------------------------------------------------------------------
    def _parse_date(
        self,
        raw: str,
        row_number: int,
        column: str,
        day_first: bool = True,
    ) -> date | None:
        text = raw.strip()
        if not text:
            return None
        formats = DAY_FIRST_FORMATS if day_first else MONTH_FIRST_FORMATS
        parsed: date | None = None
        for fmt in formats:
            try:
                parsed = datetime.strptime(text, fmt).date()
                break
            except ValueError:
                continue
        if parsed is None:
            try:
                parsed = date.fromisoformat(text[:10])
            except ValueError:
                parsed = None
        if parsed is None:
            return None
        ambiguous = AMBIGUOUS_DAY_MONTH.match(text)
        if ambiguous:
            first, second = int(ambiguous.group(1)), int(ambiguous.group(2))
            if first <= 12 and second <= 12 and first != second:
                logger.info(
                    "ambiguous %s date on sheet row %s column %s: %s",
                    "dd/mm" if day_first else "mm/dd",
                    row_number,
                    column,
                    text,
                )
        return parsed

    def _detect_day_first(self, cells: tuple[str, ...], columns: list[str]) -> bool:
        """Decide dd/mm vs mm/dd from the sheet's own data.

        The trial form is a Google Form in en-US locale, so it emits m/d/yyyy
        ("2/14/1964"). Rather than hard-coding a locale, every numeric date cell
        is tried both ways: if one order parses all of them and the other does
        not, that order wins. Only genuinely ambiguous sheets (all parts <= 12)
        fall back to the dd/mm default.
        """
        day_first_ok = month_first_ok = True
        seen = 0
        for position, cell in enumerate(cells):
            if position >= len(columns):
                break
            field_key = match_field(self.column_index, columns[position])
            if field_key not in DATE_FIELDS:
                continue
            text = cell.strip()
            if not AMBIGUOUS_DAY_MONTH.match(text):
                continue
            seen += 1
            day_first_ok &= _matches_any(text, DAY_FIRST_FORMATS)
            month_first_ok &= _matches_any(text, MONTH_FIRST_FORMATS)
        if not seen:
            return self.day_first
        previous = self.day_first
        if day_first_ok and not month_first_ok:
            self.day_first = True
        elif month_first_ok and not day_first_ok:
            self.day_first = False
        else:
            self.day_first = True
        if self.day_first != previous:
            logger.info(
                "sheet date order: %s (decided from %d date cells)",
                "dd/mm/yyyy" if self.day_first else "mm/dd/yyyy",
                seen,
            )
        return self.day_first

    def _parse_row(
        self,
        row_number: int,
        cells: tuple[str, ...],
        columns: list[str],
    ) -> ParsedRow:
        values: dict[str, str] = {}
        for position, cell in enumerate(cells):
            if position >= len(columns):
                break
            field_key = match_field(self.column_index, columns[position])
            if field_key is None or not cell.strip():
                continue
            # A form can repeat a header (the trial sheet has two identical
            # "Profile Photo Spouse" columns); keep the first non-empty cell.
            if field_key not in values:
                values[field_key] = cell.strip()

        row = ParsedRow(row_number=row_number, values=values)

        name = values.get("full_name", "").strip()
        if not name:
            row.errors.append(
                RowIssue(
                    row=row_number, column="full_name", message="Full Name is empty"
                )
            )
        elif len(name) > NAME_MAX:
            row.warnings.append(
                RowIssue(
                    row=row_number,
                    column="full_name",
                    message=f"Full Name truncated to {NAME_MAX} characters",
                )
            )
            name = name[:NAME_MAX]
        row.full_name = name or None

        raw_phone = values.get("phone_number", "").strip()
        if not raw_phone:
            row.errors.append(
                RowIssue(
                    row=row_number,
                    column="phone_number",
                    message="Mobile Number is empty",
                )
            )
        else:
            try:
                row.phone = normalize_phone_number(raw_phone)
            except Exception as exc:
                detail = getattr(exc, "detail", str(exc))
                row.errors.append(
                    RowIssue(
                        row=row_number,
                        column="phone_number",
                        message=f"Mobile Number is invalid ({detail})",
                    )
                )
            else:
                if len(row.phone) > PHONE_MAX:
                    row.warnings.append(
                        RowIssue(
                            row=row_number,
                            column="phone_number",
                            message=(
                                "Mobile Number truncated to " f"{PHONE_MAX} characters"
                            ),
                        )
                    )
                    row.phone = row.phone[:PHONE_MAX]

        email = values.get("email", "").strip()
        if email:
            if not validate_email(email):
                row.errors.append(
                    RowIssue(
                        row=row_number,
                        column="email",
                        message=f"Email ID is invalid ({email})",
                    )
                )
            elif len(email) > EMAIL_MAX:
                row.warnings.append(
                    RowIssue(
                        row=row_number,
                        column="email",
                        message=f"Email ID truncated to {EMAIL_MAX} characters",
                    )
                )
                email = email[:EMAIL_MAX]
        row.email = email or None

        for field_key in PROFILE_FIELDS:
            raw = values.get(field_key)
            if not raw:
                continue
            if field_key.endswith("_dob") or field_key == "anniversary_date":
                parsed = self._parse_date(raw, row_number, field_key, self.day_first)
                if parsed is None:
                    row.warnings.append(
                        RowIssue(
                            row=row_number,
                            column=field_key,
                            message=(
                                f"{field_key} kept as text ({raw!r}); "
                                "not a recognised date"
                            ),
                        )
                    )
                else:
                    row.dates[field_key] = parsed
        row.member_dob = row.dates.get("member_dob")
        return row

    # ------------------------------------------------------------------
    # duplicate detection
    # ------------------------------------------------------------------
    async def _existing_users(
        self, rows: list[ParsedRow]
    ) -> tuple[dict[str, User], dict[str, str]]:
        """Return (users by phone key, email -> phone key of the owning user).

        Duplicate detection is keyed on ``users.phone_number`` — the column that
        already carries a UNIQUE index and that every login resolves against.
        Both the E.164 and the bare 10-digit form are matched so a legacy row
        written without the ``+91`` prefix is not imported twice.
        """
        phones = {row.phone for row in rows if row.phone}
        emails = {row.email for row in rows if row.email}
        phone_forms = {
            form for value in phones for form in (value, phone_key(value)) if form
        }
        if not phone_forms and not emails:
            return {}, {}

        users_by_phone: dict[str, User] = {}
        emails_owner: dict[str, str] = {}
        batch_size = 500
        phone_list = sorted(phone_forms)
        email_list = sorted(emails)
        for start in range(0, len(phone_list), batch_size):
            result = await self.session.execute(
                select(User).where(
                    User.is_deleted.is_(False),
                    User.phone_number.in_(phone_list[start : start + batch_size]),
                )
            )
            for user in result.scalars().all():
                users_by_phone[phone_key(user.phone_number)] = user
        for start in range(0, len(email_list), batch_size):
            result = await self.session.execute(
                select(User.email, User.phone_number).where(
                    User.is_deleted.is_(False),
                    User.email.in_(email_list[start : start + batch_size]),
                )
            )
            for email, phone in result.all():
                if email:
                    emails_owner[email.lower()] = phone_key(phone)
        return users_by_phone, emails_owner

    async def _member_user_ids(self, users: list[User]) -> set[Any]:
        user_ids = [user.id for user in users]
        if not user_ids:
            return set()
        result = await self.session.execute(
            select(Member.user_id).where(
                Member.user_id.in_(user_ids), Member.is_deleted.is_(False)
            )
        )
        return set(result.scalars().all())

    # ------------------------------------------------------------------
    # payload building
    # ------------------------------------------------------------------
    def _member_columns(self, row: ParsedRow) -> dict[str, Any]:
        first_name, last_name = split_full_name(row.full_name or "")
        return {
            "first_name": truncate(first_name, 100),
            "last_name": truncate(last_name, 100)
            or truncate((row.full_name or "").split()[0], 100),
            "date_of_birth": row.member_dob,
            "contact_phone": truncate(row.phone, PHONE_MAX),
            "contact_email": truncate(row.email, EMAIL_MAX),
            "address_line": truncate(row.values.get("address"), ADDRESS_MAX),
            "occupation_summary": truncate(
                row.values.get("member_occupation"), TEXT_MAX
            ),
            "profile_photo_url": truncate(
                row.values.get("member_photo_link"), ADDRESS_MAX
            ),
        }

    def _profile_values(self, row: ParsedRow) -> dict[str, str | None]:
        values: dict[str, str | None] = {}
        for field_key in PROFILE_FIELDS:
            if field_key == "full_name":
                values[field_key] = truncate(row.full_name, NAME_MAX)
                continue
            if field_key == "phone_number":
                values[field_key] = truncate(row.phone, PHONE_MAX)
                continue
            if field_key in row.dates:
                values[field_key] = row.dates[field_key].isoformat()
                continue
            raw = row.values.get(field_key)
            if raw is None:
                continue
            limit = (
                ADDRESS_MAX
                if field_key in {"address", "member_photo_link"}
                else TEXT_MAX
            )
            values[field_key] = truncate(raw, limit)
        return values

    # ------------------------------------------------------------------
    # run
    # ------------------------------------------------------------------
    async def run(
        self,
        options: SyncGoogleSheetRequest,
        *,
        current_user_id: Any = None,
    ) -> SyncGoogleSheetResponse:
        if not options.dry_run and not self.settings.google_sheets_sync_enabled:
            raise BadRequestException(
                "Writing is disabled. Set GOOGLE_SHEETS_SYNC_ENABLED=true once the "
                "dry-run report has been reviewed."
            )

        spreadsheet_id = (
            options.spreadsheet_id or self.settings.google_sheets_spreadsheet_id
        )
        sheet_name = options.sheet_name or self.settings.google_sheets_sheet_name
        header_row = options.header_row or self.settings.google_sheets_header_row
        data_start_row = (
            options.data_start_row or self.settings.google_sheets_data_start_row
        )

        table: SheetTable = self.sheets.read_table(
            spreadsheet_id, sheet_name, header_row, data_start_row
        )
        if options.limit:
            table = SheetTable(
                spreadsheet_id=table.spreadsheet_id,
                sheet_name=table.sheet_name,
                header_row=table.header_row,
                data_start_row=table.data_start_row,
                columns=table.columns,
                rows=table.rows[: options.limit],
            )

        headers = [header for _letter, header in table.columns]
        keys = [match_field(self.column_index, header) for header in headers]
        unmapped = [
            header.replace("\n", " ").strip()
            for header, key in zip(headers, keys, strict=True)
            if key is None and header.strip()
        ]
        missing = [
            field_key
            for field_key in REQUIRED_FIELDS
            if field_key not in {key for key in keys if key}
        ]
        if missing:
            raise BadRequestException(
                "Required sheet columns are missing: "
                + ", ".join(sorted(missing))
                + ". Found: "
                + ", ".join(headers)
            )

        for _number, cells in table.rows:
            self._detect_day_first(cells, headers)

        rows = [self._parse_row(number, cells, headers) for number, cells in table.rows]
        valid_rows = [row for row in rows if row.valid]

        users_by_phone, emails_owner = await self._existing_users(valid_rows)
        member_user_ids = await self._member_user_ids(list(users_by_phone.values()))

        member_role_id: Any = None
        if not options.dry_run:
            member_role_id = (
                await Role.get_or_create(self.session, RoleName.MEMBER)
            ).id

        results: list[RowResult] = []
        errors: list[RowIssue] = []
        warnings: list[RowIssue] = []
        inserted = linked = skipped = failed = 0
        seen_phones: dict[str, int] = {}

        for row in rows:
            errors.extend(row.errors)
            warnings.extend(row.warnings)
            if not row.valid:
                failed += 1
                results.append(
                    RowResult(
                        row=row.row_number,
                        action="failed",
                        name=row.full_name,
                        phone=row.masked_phone(),
                        reason=row.errors[0].message
                        if row.errors
                        else "validation failed",
                    )
                )
                continue

            assert row.phone is not None
            key = phone_key(row.phone)
            first_seen = seen_phones.get(key)
            if first_seen is not None:
                skipped += 1
                results.append(
                    RowResult(
                        row=row.row_number,
                        action="skipped",
                        name=row.full_name,
                        phone=row.masked_phone(),
                        reason=(
                            "duplicate Mobile Number "
                            f"(first seen on row {first_seen})"
                        ),
                    )
                )
                continue
            seen_phones[key] = row.row_number

            if row.email and emails_owner.get(row.email.lower(), key) != key:
                failed += 1
                issue = RowIssue(
                    row=row.row_number,
                    column="email",
                    message=f"Email ID already belongs to another member ({row.email})",
                )
                errors.append(issue)
                results.append(
                    RowResult(
                        row=row.row_number,
                        action="failed",
                        name=row.full_name,
                        phone=row.masked_phone(),
                        reason=issue.message,
                    )
                )
                continue

            user = users_by_phone.get(key)
            if user is not None and user.id in member_user_ids:
                skipped += 1
                results.append(
                    RowResult(
                        row=row.row_number,
                        action="skipped",
                        name=row.full_name,
                        phone=row.masked_phone(),
                        reason=(
                            "already imported " "(member exists for this mobile number)"
                        ),
                    )
                )
                continue

            action = "insert" if user is None else "link"
            if options.dry_run:
                if action == "insert":
                    inserted += 1
                else:
                    linked += 1
                results.append(
                    RowResult(
                        row=row.row_number,
                        action=action,
                        name=row.full_name,
                        phone=row.masked_phone(),
                        reason=(
                            None
                            if action == "insert"
                            else "mobile number already registered; member record "
                            "would be created for the existing user"
                        ),
                    )
                )
                continue

            try:
                async with self.session.begin_nested():
                    member = await self._insert_row(
                        row, user, member_role_id, current_user_id
                    )
                member_user_ids.add(member.user_id)
                if user is None:
                    users_by_phone[key] = member.user
                    inserted += 1
                else:
                    linked += 1
            except Exception as exc:  # noqa: BLE001 - one bad row must not abort the batch
                logger.exception(
                    "registration sync failed on sheet row %s", row.row_number
                )
                failed += 1
                issue = RowIssue(
                    row=row.row_number,
                    column=None,
                    message=f"{type(exc).__name__}: {exc}",
                )
                errors.append(issue)
                results.append(
                    RowResult(
                        row=row.row_number,
                        action="failed",
                        name=row.full_name,
                        phone=row.masked_phone(),
                        reason=issue.message,
                    )
                )
                continue
            results.append(
                RowResult(
                    row=row.row_number,
                    action=action,
                    name=row.full_name,
                    phone=row.masked_phone(),
                )
            )

        # A dry run must not touch PostgreSQL at all, so no audit row either.
        if not options.dry_run:
            await AuditService(self.session).log(
                SYNC_AUDIT_ACTION,
                user_id=current_user_id,
                entity_type="member",
                entity_id=None,
                details={
                    "spreadsheet_id": table.spreadsheet_id,
                    "sheet_name": table.sheet_name,
                    "total_rows": len(rows),
                    "inserted": inserted,
                    "linked_existing_user": linked,
                    "skipped_duplicate": skipped,
                    "failed": failed,
                },
            )

        if options.dry_run:
            message = (
                f"Dry run complete: {len(rows)} rows read, {inserted} would be "
                f"inserted, {linked} would be linked to an existing user, "
                f"{skipped} skipped, {failed} failed. Nothing was written."
            )
        else:
            message = (
                f"Sync complete: {len(rows)} rows read, {inserted} inserted, "
                f"{linked} linked to an existing user, {skipped} skipped, "
                f"{failed} failed"
            )
        return SyncGoogleSheetResponse(
            dry_run=options.dry_run,
            spreadsheet_id=table.spreadsheet_id,
            sheet_name=table.sheet_name,
            header_row=table.header_row,
            data_start_row=table.data_start_row,
            total_rows=len(rows),
            inserted=inserted,
            linked_existing_user=linked,
            skipped_duplicate=skipped,
            failed=failed,
            columns=headers,
            unmapped_columns=unmapped,
            missing_columns=missing,
            errors=errors[:PREVIEW_LIMIT],
            warnings=warnings[:PREVIEW_LIMIT],
            rows=results[:PREVIEW_LIMIT],
            message=message,
        )

    # ------------------------------------------------------------------
    # writing (only reached when dry_run is false and the kill switch is on)
    # ------------------------------------------------------------------
    async def _insert_row(
        self,
        row: ParsedRow,
        user: User | None,
        member_role_id: Any,
        current_user_id: Any,
    ) -> Member:
        columns = self._member_columns(row)
        if user is None:
            user = User(
                phone_number=row.phone,
                full_name=row.full_name,
                email=row.email,
                # NOT NULL column: a random secret so password login is possible
                # for nobody. Imported members sign in with phone OTP.
                password_hash=hash_password(secrets.token_urlsafe(32)),
                is_active=True,
                is_email_verified=bool(row.email),
                is_phone_verified=False,
                role_id=member_role_id,
                created_by=current_user_id,
                updated_by=current_user_id,
            )
            self.session.add(user)
            await self.session.flush()

        member = Member(
            user_id=user.id,
            first_name=columns["first_name"],
            last_name=columns["last_name"],
            date_of_birth=columns["date_of_birth"],
            contact_phone=columns["contact_phone"],
            contact_email=columns["contact_email"],
            address_line=columns["address_line"],
            occupation_summary=columns["occupation_summary"],
            profile_photo_url=columns["profile_photo_url"],
            membership_status=MemberStatus.PENDING.value,
            created_by=current_user_id,
            updated_by=current_user_id,
        )
        # Assigned so ProfileService never has to lazy-load the relationship
        # inside the async session.
        member.user = user
        self.session.add(member)
        await self.session.flush()

        await self.profiles.apply_values(member, self._profile_values(row))
        logger.info(
            "registration sync inserted member %s for sheet row %s",
            member.id,
            row.row_number,
        )
        return member
