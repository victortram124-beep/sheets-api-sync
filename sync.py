"""
Sheets ↔ REST API bidirectional sync engine.

Replaces the "I copy-paste from the spreadsheet into the CRM every Monday"
workflow with a one-command sync that:
  1. Pulls the current state of both sides
  2. Diffs by primary id
  3. Resolves conflicts with a pluggable strategy (see conflict.py)
  4. Pushes deltas back to each side
  5. Logs every change to an audit DB (see audit.py)

Usage:
    python sync.py sync                  # run a full sync now
    python sync.py dry-run               # show what would change, don't write
    python sync.py audit --output a.csv  # dump the audit log
    python sync.py sync --loop 600       # loop forever, every 10 minutes

Environment (see .env.example):
    GOOGLE_CREDENTIALS_JSON_PATH, SHEET_ID,
    API_BASE_URL, API_TOKEN,
    CONFLICT_STRATEGY, SYNC_INTERVAL_SECONDS, AUDIT_DB_PATH
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

import click
import httpx
import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError

from audit import AuditLog
from conflict import ConflictContext, ConflictError, get_strategy

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("sync")


# ---------- Config ----------

class ColumnMap(BaseModel):
    """One column in the sheet ↔ one field in the API payload."""
    sheet: str
    api: str


class SyncConfig(BaseModel):
    id_column: str = Field(..., description="Column/field used as the row primary key")
    direction: str = Field("bidirectional", pattern="^(bidirectional|sheet-to-api|api-to-sheet)$")
    columns: list[ColumnMap]
    sheet_range: str = "Sheet1"
    api_resource: str = Field("items", description="REST resource path appended to API_BASE_URL")
    conflict_strategy: Optional[str] = None  # overrides env if set

    def sheet_to_api(self, row: dict[str, Any]) -> dict[str, Any]:
        """Translate a sheet row (col-name keyed) into an API payload."""
        return {c.api: row.get(c.sheet) for c in self.columns}

    def api_to_sheet(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Translate an API record into a sheet row dict."""
        return {c.sheet: payload.get(c.api) for c in self.columns}


def load_config(path: str) -> SyncConfig:
    p = Path(path)
    if not p.exists():
        log.error("Config file %s not found. Copy config.example.yaml to %s.", path, path)
        sys.exit(1)
    with p.open() as f:
        raw = yaml.safe_load(f) or {}
    try:
        return SyncConfig(**raw)
    except ValidationError as e:
        log.error("Invalid config:\n%s", e)
        sys.exit(2)


# ---------- Google Sheets adapter ----------
#
# Wrapped in a thin abstraction so (a) tests can swap in an in-memory dict
# without needing real credentials, and (b) the rest of the engine never
# has to think about the Google API surface.

class SheetClient:
    """Adapter over the Google Sheets API. Falls back to an in-memory store
    when GOOGLE_CREDENTIALS_JSON_PATH is unset — useful for dry runs and CI."""

    def __init__(self, sheet_id: str, creds_path: Optional[str], range_a1: str):
        self.sheet_id = sheet_id
        self.range_a1 = range_a1
        self._service = None
        if creds_path and Path(creds_path).exists():
            self._service = self._build_service(creds_path)
        else:
            # The in-memory store gives the rest of the engine something to
            # talk to during development. It's keyed by row index so writes
            # round-trip cleanly.
            log.warning(
                "No Google credentials found at %s — running in IN-MEMORY mode. "
                "Real Sheets I/O is disabled.",
                creds_path,
            )
            self._memory: list[dict[str, Any]] = []

    @staticmethod
    def _build_service(creds_path: str):
        # Import lazily so the in-memory mode doesn't need the dependency.
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = service_account.Credentials.from_service_account_file(
            creds_path, scopes=scopes,
        )
        # cache_discovery=False quiets a noisy oauth2client warning on modern installs.
        return build("sheets", "v4", credentials=creds, cache_discovery=False)

    def read_rows(self) -> list[dict[str, Any]]:
        """Return the sheet as a list of column-name-keyed dicts."""
        if self._service is None:
            return list(self._memory)
        try:
            resp = (
                self._service.spreadsheets()
                .values()
                .get(spreadsheetId=self.sheet_id, range=self.range_a1)
                .execute()
            )
        except Exception as e:
            log.exception("Sheets read failed: %s", e)
            raise
        values = resp.get("values", [])
        if not values:
            return []
        header, *body = values
        rows: list[dict[str, Any]] = []
        for raw in body:
            # Pad short rows so missing trailing cells don't drop columns.
            padded = raw + [""] * (len(header) - len(raw))
            rows.append(dict(zip(header, padded)))
        return rows

    def write_rows(self, rows: list[dict[str, Any]], header: list[str]) -> None:
        """Replace the sheet's data area with `rows`. Header preserved on top."""
        if self._service is None:
            self._memory = list(rows)
            return
        body = [header] + [[r.get(col, "") for col in header] for r in rows]
        try:
            self._service.spreadsheets().values().update(
                spreadsheetId=self.sheet_id,
                range=self.range_a1,
                valueInputOption="RAW",
                body={"values": body},
            ).execute()
        except Exception as e:
            log.exception("Sheets write failed: %s", e)
            raise


# ---------- REST API adapter ----------

class APIClient:
    """A minimal sync httpx wrapper. The token is sent as a bearer header,
    which is the most common pattern — swap to API-key headers as needed."""

    def __init__(self, base_url: str, token: Optional[str], resource: str, timeout: float = 15.0):
        self.base = base_url.rstrip("/")
        self.resource = resource.strip("/")
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.Client(headers=headers, timeout=timeout)

    @property
    def _url(self) -> str:
        return f"{self.base}/{self.resource}"

    def list(self) -> list[dict[str, Any]]:
        try:
            r = self._client.get(self._url)
            r.raise_for_status()
        except httpx.HTTPError as e:
            log.exception("API list failed: %s", e)
            raise
        data = r.json()
        # Tolerate both {"items": [...]} and a bare list response shape.
        if isinstance(data, dict) and "items" in data:
            return data["items"]
        if isinstance(data, list):
            return data
        log.warning("Unexpected API list shape: %r", type(data).__name__)
        return []

    def upsert(self, record: dict[str, Any], id_field: str) -> None:
        """PUT to /{resource}/{id} if id is present, POST to /{resource} otherwise."""
        rid = record.get(id_field)
        try:
            if rid:
                r = self._client.put(f"{self._url}/{rid}", json=record)
            else:
                r = self._client.post(self._url, json=record)
            r.raise_for_status()
        except httpx.HTTPError as e:
            log.exception("API upsert failed for id=%r: %s", rid, e)
            raise

    def delete(self, rid: str) -> None:
        try:
            r = self._client.delete(f"{self._url}/{rid}")
            r.raise_for_status()
        except httpx.HTTPError as e:
            log.exception("API delete failed for id=%r: %s", rid, e)
            raise

    def close(self) -> None:
        self._client.close()


# ---------- Diff engine ----------

@dataclass
class Diff:
    to_api_upserts: list[dict[str, Any]] = field(default_factory=list)
    to_sheet_upserts: list[dict[str, Any]] = field(default_factory=list)
    conflicts: list[ConflictContext] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{len(self.to_api_upserts)} → API, "
            f"{len(self.to_sheet_upserts)} → Sheet, "
            f"{len(self.conflicts)} conflicts"
        )


def _index(rows: Iterable[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        rid = row.get(key)
        # Skip blank ids — those are unsaved/draft rows the user is still typing.
        if rid in (None, ""):
            continue
        out[str(rid)] = row
    return out


def compute_diff(
    sheet_rows: list[dict[str, Any]],
    api_rows: list[dict[str, Any]],
    config: SyncConfig,
) -> Diff:
    """Pure function — no I/O, easy to test."""
    diff = Diff()
    by_sheet = _index(sheet_rows, config.id_column)
    # API records are keyed by their API-side id field name.
    api_id_field = next((c.api for c in config.columns if c.sheet == config.id_column), config.id_column)
    by_api = _index(api_rows, api_id_field)

    all_ids = set(by_sheet) | set(by_api)
    for rid in sorted(all_ids):
        s = by_sheet.get(rid)
        a = by_api.get(rid)

        if s and not a:
            diff.to_api_upserts.append(s)
            continue
        if a and not s:
            diff.to_sheet_upserts.append(a)
            continue
        # Both present — compare each mapped field.
        for col in config.columns:
            sv = s.get(col.sheet)  # type: ignore[union-attr]
            av = a.get(col.api)    # type: ignore[union-attr]
            if _normalize(sv) == _normalize(av):
                continue
            diff.conflicts.append(ConflictContext(
                row_id=rid, field=col.sheet, sheet_value=sv, api_value=av,
            ))
    return diff


def _normalize(v: Any) -> str:
    """Comparison-friendly form. Sheets returns everything as strings; APIs
    return native types. We compare on stringified, trimmed values to avoid
    spurious '5' vs 5 conflicts."""
    if v is None:
        return ""
    return str(v).strip()


# ---------- Sync orchestrator ----------

def run_sync(
    sheet: SheetClient,
    api: APIClient,
    config: SyncConfig,
    audit: AuditLog,
    *,
    dry_run: bool = False,
) -> Diff:
    log.info("Pulling sheet…")
    sheet_rows = sheet.read_rows()
    audit.record("read", "sheet", note=f"{len(sheet_rows)} rows")

    log.info("Pulling API…")
    api_rows = api.list()
    audit.record("read", "api", note=f"{len(api_rows)} rows")

    diff = compute_diff(sheet_rows, api_rows, config)
    log.info("Diff: %s", diff.summary())

    if dry_run:
        for c in diff.conflicts:
            log.info("  CONFLICT row=%s field=%s sheet=%r api=%r",
                     c.row_id, c.field, c.sheet_value, c.api_value)
        return diff

    strategy = get_strategy(config.conflict_strategy or os.environ.get("CONFLICT_STRATEGY", "last_write_wins"))

    # Resolve conflicts first so the resolved value flows into the upsert below.
    resolved: dict[tuple[str, str], Any] = {}
    for ctx in diff.conflicts:
        try:
            chosen = strategy.resolve(ctx)
            resolved[(ctx.row_id, ctx.field)] = chosen
            audit.record(
                "conflict", "sync",
                row_id=ctx.row_id, field=ctx.field,
                old_value=ctx.api_value, new_value=chosen,
                note=f"strategy={strategy.name}",
            )
        except ConflictError as e:
            audit.record("skip", "sync", row_id=ctx.row_id, field=ctx.field,
                         note=f"manual review required: {e}")
            log.warning("Conflict needs manual review: %s", e)

    # Apply API → Sheet (only when direction permits).
    if config.direction in ("bidirectional", "api-to-sheet"):
        _apply_to_sheet(sheet, sheet_rows, diff, resolved, config, audit)

    # Apply Sheet → API (only when direction permits).
    if config.direction in ("bidirectional", "sheet-to-api"):
        _apply_to_api(api, diff, resolved, config, audit)

    return diff


def _apply_to_sheet(
    sheet: SheetClient,
    sheet_rows: list[dict[str, Any]],
    diff: Diff,
    resolved: dict[tuple[str, str], Any],
    config: SyncConfig,
    audit: AuditLog,
) -> None:
    by_id = _index(sheet_rows, config.id_column)
    for record in diff.to_sheet_upserts:
        row = config.api_to_sheet(record)
        rid = str(row.get(config.id_column, ""))
        by_id[rid] = row
        audit.record("write", "sheet", row_id=rid, note="upsert from api")

    # Push resolved conflict values back into the sheet copy.
    for (rid, field), value in resolved.items():
        if rid in by_id:
            by_id[rid][field] = value

    header = [c.sheet for c in config.columns]
    try:
        sheet.write_rows(list(by_id.values()), header)
    except Exception:
        audit.record("error", "sheet", note="write_rows failed")
        raise


def _apply_to_api(
    api: APIClient,
    diff: Diff,
    resolved: dict[tuple[str, str], Any],
    config: SyncConfig,
    audit: AuditLog,
) -> None:
    api_id_field = next((c.api for c in config.columns if c.sheet == config.id_column), config.id_column)

    for sheet_row in diff.to_api_upserts:
        # Apply any resolved values before pushing.
        rid = str(sheet_row.get(config.id_column, ""))
        merged = dict(sheet_row)
        for (cid, field), value in resolved.items():
            if cid == rid:
                merged[field] = value
        payload = config.sheet_to_api(merged)
        try:
            api.upsert(payload, api_id_field)
            audit.record("write", "api", row_id=rid, note="upsert from sheet")
        except Exception:
            audit.record("error", "api", row_id=rid, note="upsert failed")
            # Don't abort the entire run — log and keep going.
            continue


# ---------- CLI ----------

def _build_clients(config_path: str) -> tuple[SheetClient, APIClient, SyncConfig, AuditLog]:
    config = load_config(config_path)
    sheet = SheetClient(
        sheet_id=os.environ.get("SHEET_ID", ""),
        creds_path=os.environ.get("GOOGLE_CREDENTIALS_JSON_PATH"),
        range_a1=config.sheet_range,
    )
    api = APIClient(
        base_url=os.environ.get("API_BASE_URL", "http://localhost:8000"),
        token=os.environ.get("API_TOKEN"),
        resource=config.api_resource,
    )
    audit = AuditLog(os.environ.get("AUDIT_DB_PATH", "audit.db"))
    return sheet, api, config, audit


@click.group()
def cli() -> None:
    """Sheets ↔ API sync — see README for setup."""


@cli.command("sync")
@click.option("--config", "config_path", default="config.yaml", show_default=True)
@click.option("--loop", "loop_seconds", type=int, default=0,
              help="If > 0, run repeatedly with this interval (seconds).")
def cmd_sync(config_path: str, loop_seconds: int) -> None:
    """Run one sync cycle (or loop forever if --loop is set)."""
    sheet, api, config, audit = _build_clients(config_path)

    def _once() -> None:
        try:
            run_sync(sheet, api, config, audit)
        except Exception:
            log.exception("Sync cycle raised; will retry on next interval.")

    if loop_seconds <= 0:
        _once()
        api.close()
        return

    stop = False
    def _stop(*_): nonlocal stop; stop = True  # noqa: E704
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    log.info("Loop mode: every %d seconds", loop_seconds)
    while not stop:
        _once()
        # Sleep in 1-sec slices so SIGTERM doesn't have to wait the full interval.
        for _ in range(loop_seconds):
            if stop:
                break
            time.sleep(1)
    api.close()
    log.info("Shutdown complete.")


@cli.command("dry-run")
@click.option("--config", "config_path", default="config.yaml", show_default=True)
def cmd_dry_run(config_path: str) -> None:
    """Show what would change without writing anything."""
    sheet, api, config, audit = _build_clients(config_path)
    diff = run_sync(sheet, api, config, audit, dry_run=True)
    click.echo(f"\n{diff.summary()}")
    api.close()


@cli.command("audit")
@click.option("--output", "output_path", default="audit.csv", show_default=True)
def cmd_audit(output_path: str) -> None:
    """Dump the audit log to CSV."""
    audit = AuditLog(os.environ.get("AUDIT_DB_PATH", "audit.db"))
    n = audit.dump_csv(output_path)
    click.echo(f"Wrote {n} rows to {output_path}")


if __name__ == "__main__":
    cli()
