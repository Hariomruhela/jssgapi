from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class SyncGoogleSheetRequest(BaseModel):
    """Options for ``POST /api/registration/sync-google-sheet``.

    ``dry_run`` defaults to ``True``: the sheet is read, validated and reported
    without a single row being written to PostgreSQL.
    """

    model_config = ConfigDict(extra="forbid")

    dry_run: bool = True
    spreadsheet_id: str | None = None
    sheet_name: str | None = None
    header_row: int | None = Field(default=None, ge=1)
    data_start_row: int | None = Field(default=None, ge=2)
    limit: int | None = Field(default=None, ge=1, le=5000)


class RowIssue(BaseModel):
    """A validation problem found on one sheet row."""

    row: int
    column: str | None = None
    message: str


class RowResult(BaseModel):
    """What happened (or would happen) to one sheet row."""

    row: int
    action: str
    name: str | None = None
    phone: str | None = None
    reason: str | None = None


class SyncGoogleSheetResponse(BaseModel):
    success: bool = True
    message: str
    dry_run: bool
    spreadsheet_id: str
    sheet_name: str
    header_row: int
    data_start_row: int
    total_rows: int
    inserted: int = 0
    linked_existing_user: int = 0
    skipped_duplicate: int = 0
    failed: int = 0
    columns: list[str] = []
    unmapped_columns: list[str] = []
    missing_columns: list[str] = []
    errors: list[RowIssue] = []
    warnings: list[RowIssue] = []
    rows: list[RowResult] = []
