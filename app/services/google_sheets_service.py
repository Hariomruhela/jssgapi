from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from app.config import get_settings
from app.core.exceptions import (
    BadRequestException,
    ServiceUnavailableException,
    ValidationException,
)

logger = logging.getLogger(__name__)

# Read-only: the import never writes back to the sheet.
SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
# Reading member photos out of Drive needs this. The scope is requested
# separately rather than added to SHEETS_SCOPES: a service-account token can
# only be minted for scopes enabled on the Cloud project, so widening the
# Sheets scope could break a sync that works today. The Drive downloader falls
# back to an unauthenticated fetch for files shared "anyone with the link", so
# a project without the Drive scope still migrates those files.
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
SCOPES = SHEETS_SCOPES


def _credentials_from_inline_json(raw: str, scopes: list[str]) -> Any:
    from google.oauth2 import service_account

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValidationException(
            "GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON is not valid JSON: "
            f"{exc.msg} (line {exc.lineno})"
        ) from exc
    private_key = payload.get("private_key")
    if isinstance(private_key, str) and "\\n" in private_key:
        # Keys pasted into a single-line env var keep literal "\n".
        payload["private_key"] = private_key.replace("\\n", "\n")
    missing = [k for k in ("type", "client_email", "private_key") if not payload.get(k)]
    if missing:
        raise ValidationException(
            "GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON is missing: " + ", ".join(missing)
        )
    return service_account.Credentials.from_service_account_info(
        payload, scopes=scopes
    )


def _credentials_from_file(path: str, scopes: list[str]) -> Any:
    from google.oauth2 import service_account

    try:
        return service_account.Credentials.from_service_account_file(
            path, scopes=scopes
        )
    except OSError as exc:
        raise ValidationException(
            f"Service-account key file could not be read: {exc.strerror or exc}"
        ) from exc
    except ValueError as exc:
        raise ValidationException(
            f"Service-account key file is not a valid service-account JSON: {exc}"
        ) from exc


def _credentials_from_adc(scopes: list[str]) -> Any:
    import google.auth

    credentials, _ = google.auth.default(scopes=scopes)
    return credentials


def load_google_credentials(settings: Any, scopes: list[str]) -> Any:
    """Resolve service-account credentials for ``scopes``, without logging them.

    Single source of truth for every Google call in the project: the sheet sync
    and the Drive photo migration read the same env var and the same key file
    and differ only in the scopes they ask for.
    """
    inline = (settings.google_sheets_service_account_json or "").strip()
    if inline:
        logger.info("google credentials: inline service account json")
        return _credentials_from_inline_json(inline, scopes)
    path = (settings.google_application_credentials or "").strip()
    if path:
        logger.info("google credentials: key file")
        return _credentials_from_file(path, scopes)
    logger.info("google credentials: application default credentials")
    return _credentials_from_adc(scopes)

_SPREADSHEET_ID_RE = re.compile(r"^[A-Za-z0-9_-]{20,}$")
_SPREADSHEET_URL_RE = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]+)")

# Wide enough to read any header row in one call (ZZ = 702 columns).
MAX_COLUMN_LETTER = "ZZ"


@dataclass(frozen=True)
class SheetTable:
    """A sheet read as a header row plus the rows below it."""

    spreadsheet_id: str
    sheet_name: str
    header_row: int
    data_start_row: int
    columns: tuple[tuple[str, str], ...]
    rows: tuple[tuple[int, tuple[str, ...]], ...]


def column_letter(index: int) -> str:
    """0-based column index to its A1 notation letter (0 -> A)."""
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def extract_spreadsheet_id(value: str) -> str:
    """Accept either a bare spreadsheet id or a full Google Sheets URL."""
    candidate = (value or "").strip()
    if not candidate:
        return ""
    match = _SPREADSHEET_URL_RE.search(candidate)
    if match:
        return match.group(1)
    if _SPREADSHEET_ID_RE.match(candidate):
        return candidate
    return ""


def _quote_sheet_name(sheet_name: str) -> str:
    return "'" + sheet_name.replace("'", "''") + "'"


def _format_date(value: Any) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


class GoogleSheetsService:
    """Read-only Google Sheets v4 access using service-account credentials."""

    def __init__(self) -> None:
        self.settings = get_settings()

    # ------------------------------------------------------------------
    # credentials
    # ------------------------------------------------------------------
    def _credentials_from_inline_json(self, raw: str) -> Any:
        return _credentials_from_inline_json(raw, SCOPES)

    def _credentials_from_file(self, path: str) -> Any:
        return _credentials_from_file(path, SCOPES)

    def _credentials_from_adc(self) -> Any:
        return _credentials_from_adc(SCOPES)

    def credentials(self) -> Any:
        """Resolve service-account credentials without ever logging them."""
        return load_google_credentials(self.settings, SCOPES)

    def _sheets_api(self) -> Any:
        try:
            from googleapiclient.discovery import build
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise ServiceUnavailableException(
                "google-api-python-client is not installed. "
                "Run: pip install -r requirements.txt"
            ) from exc
        try:
            return build(
                "sheets",
                "v4",
                credentials=self.credentials(),
                cache_discovery=False,
            )
        except Exception as exc:
            raise ServiceUnavailableException(
                f"Google Sheets authentication failed ({type(exc).__name__}): {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # reading
    # ------------------------------------------------------------------
    def _get(self, spreadsheet_id: str, range_: str) -> list[list[Any]]:
        try:
            response = (
                self._sheets_api()
                .spreadsheets()
                .values()
                .get(
                    spreadsheetId=spreadsheet_id,
                    range=range_,
                    valueRenderOption="FORMATTED_VALUE",
                    majorDimension="ROWS",
                )
                .execute()
            )
        except BadRequestException:
            raise
        except Exception as exc:  # googleapiclient errors
            raise ServiceUnavailableException(
                f"Google Sheets request failed ({type(exc).__name__}): {exc}"
            ) from exc
        return list(response.get("values") or [])

    def list_tabs(self, spreadsheet_id: str) -> list[str]:
        """Tab names in the spreadsheet, for error messages and diagnostics."""
        spreadsheet_id = extract_spreadsheet_id(spreadsheet_id)
        try:
            metadata = (
                self._sheets_api()
                .spreadsheets()
                .get(
                    spreadsheetId=spreadsheet_id,
                    fields="sheets.properties.title",
                )
                .execute()
            )
        except Exception as exc:  # pragma: no cover - diagnostics only
            logger.warning("could not list tabs: %s", exc)
            return []
        return [
            str((sheet.get("properties") or {}).get("title", ""))
            for sheet in metadata.get("sheets") or []
        ]

    def verify_access(self, spreadsheet_id: str, sheet_name: str = "") -> str:
        """Prove the credentials work and the sheet is shared with them."""
        spreadsheet_id = extract_spreadsheet_id(spreadsheet_id)
        if not spreadsheet_id:
            raise ValidationException(
                "A valid Google Sheets spreadsheet id is required"
            )
        try:
            metadata = (
                self._sheets_api()
                .spreadsheets()
                .get(spreadsheetId=spreadsheet_id, fields="properties.title")
                .execute()
            )
        except Exception as exc:
            raise ServiceUnavailableException(
                "Google Sheets access check failed "
                f"({type(exc).__name__}): {exc}. Is the Google Sheets API enabled "
                "and is the sheet shared with the service account email?"
            ) from exc
        title = (metadata.get("properties") or {}).get("title", "")
        logger.info("google sheets access verified for %s (%s)", spreadsheet_id, title)
        return title

    def read_table(
        self,
        spreadsheet_id: str,
        sheet_name: str,
        header_row: int,
        data_start_row: int,
    ) -> SheetTable:
        """Read the header row and every data row below it."""
        spreadsheet_id = extract_spreadsheet_id(spreadsheet_id)
        if not spreadsheet_id:
            raise ValidationException(
                "A valid Google Sheets spreadsheet id is required"
            )
        if not sheet_name.strip():
            raise ValidationException("A sheet (tab) name is required")

        override = (self.settings.google_sheets_range or "").strip()
        if override:
            header_values = self._get(spreadsheet_id, override)
            header_row = 1
            data_start_row = 2
            quoted = ""
        else:
            quoted = _quote_sheet_name(sheet_name)
            try:
                header_values = self._get(
                    spreadsheet_id,
                    f"{quoted}!A{header_row}:{MAX_COLUMN_LETTER}" f"{header_row}",
                )
            except ServiceUnavailableException as exc:
                # Almost always a tab-name typo: say so and list the real names.
                tabs = self.list_tabs(spreadsheet_id)
                detail = getattr(exc, "detail", None) or str(exc)
                raise ValidationException(
                    f"Could not read '{sheet_name}' from the spreadsheet."
                    + (f" Available tabs: {', '.join(tabs)}." if tabs else "")
                    + f" Original error: {detail}"
                ) from exc

        columns: list[tuple[str, str]] = []
        for index, cell in enumerate(header_values[0] if header_values else []):
            # Blank headers are kept as "" placeholders so every data cell below
            # stays aligned with its own column letter.
            columns.append((column_letter(index), _format_date(cell).strip()))
        if not any(text for _letter, text in columns):
            raise ValidationException(
                f"No headers found on row {header_row} of '{sheet_name}'"
            )

        last_letter = columns[-1][0]
        data_range = (
            override if override else f"{quoted}!A{data_start_row}:{last_letter}"
        )
        data_values = self._get(spreadsheet_id, data_range)

        rows: list[tuple[int, tuple[str, ...]]] = []
        width = len(columns)
        for offset, raw_row in enumerate(data_values):
            cells = [_format_date(cell).strip() for cell in raw_row][:width]
            cells += [""] * (width - len(cells))
            row_number = data_start_row + offset
            if not any(cells):
                continue
            rows.append((row_number, tuple(cells)))

        return SheetTable(
            spreadsheet_id=spreadsheet_id,
            sheet_name=sheet_name,
            header_row=header_row,
            data_start_row=data_start_row,
            columns=tuple(columns),
            rows=tuple(rows),
        )
