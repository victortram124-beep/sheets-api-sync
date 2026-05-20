"""
Conflict resolution strategies for bidirectional sync.

When the same row has been edited on both sides (Sheet *and* API) between two
sync runs, we have to decide whose copy wins. This module exposes a small set
of strategies so callers can pick one in config without touching sync logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional, Protocol


class ConflictError(Exception):
    """Raised by the Manual strategy so the caller can decide externally."""

    def __init__(self, row_id: str, field: str, sheet_value: Any, api_value: Any):
        self.row_id = row_id
        self.field = field
        self.sheet_value = sheet_value
        self.api_value = api_value
        super().__init__(
            f"Conflict on row {row_id!r} field {field!r}: "
            f"sheet={sheet_value!r} api={api_value!r}"
        )


@dataclass
class ConflictContext:
    """All the information a strategy needs to make a decision."""

    row_id: str
    field: str
    sheet_value: Any
    api_value: Any
    sheet_mtime: Optional[datetime] = None
    api_mtime: Optional[datetime] = None


class ConflictStrategy(Protocol):
    name: str

    def resolve(self, ctx: ConflictContext) -> Any:  # pragma: no cover - protocol
        ...


class SheetWins:
    """Always prefer the value from the spreadsheet. Useful when humans edit
    the sheet and the API is treated as a read-mostly mirror."""

    name = "sheet_wins"

    def resolve(self, ctx: ConflictContext) -> Any:
        return ctx.sheet_value


class APIWins:
    """Always prefer the API value. Useful when the API is the source of truth
    (CRM, billing system) and the sheet is a working view."""

    name = "api_wins"

    def resolve(self, ctx: ConflictContext) -> Any:
        return ctx.api_value


class LastWriteWins:
    """Prefer whichever side was modified most recently. Falls back to the
    API value when timestamps are missing — API systems usually have reliable
    mtimes, Sheets often doesn't expose them per-cell."""

    name = "last_write_wins"

    def resolve(self, ctx: ConflictContext) -> Any:
        if ctx.sheet_mtime and ctx.api_mtime:
            return ctx.sheet_value if ctx.sheet_mtime >= ctx.api_mtime else ctx.api_value
        # Without timestamps we can't make an informed call; defer to API.
        return ctx.api_value


class Manual:
    """Raise so an external operator (or higher-level workflow) can decide."""

    name = "manual"

    def resolve(self, ctx: ConflictContext) -> Any:
        raise ConflictError(ctx.row_id, ctx.field, ctx.sheet_value, ctx.api_value)


_STRATEGIES: dict[str, type] = {
    SheetWins.name: SheetWins,
    APIWins.name: APIWins,
    LastWriteWins.name: LastWriteWins,
    Manual.name: Manual,
}


def get_strategy(name: str) -> ConflictStrategy:
    """Look up a strategy by its config name. Raises ValueError on unknown."""
    try:
        return _STRATEGIES[name]()
    except KeyError:
        valid = ", ".join(sorted(_STRATEGIES))
        raise ValueError(f"Unknown conflict strategy {name!r}. Valid: {valid}")
