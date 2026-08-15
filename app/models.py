"""Pydantic models for request/response validation."""

from typing import Literal

from pydantic import BaseModel, Field

# Valid lookback periods offered by the UI (subset of the API's full set).
Lookback = Literal["24h", "7d", "30d"]
# Auto-refresh cadence in minutes (0 = manual only).
UpdateInterval = Literal[0, 5, 15, 30, 60]


class MarketCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class MarketRename(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class ItemAdd(BaseModel):
    item_id: int = Field(gt=0)


class ItemImport(BaseModel):
    """A list of item IDs to import, in display order."""

    item_ids: list[int] = Field(default_factory=list)


class LookupOut(BaseModel):
    item_id: int
    name: str
    icon: str | None = None


class MarketReorder(BaseModel):
    """New display order for the market list (full ordering)."""

    market_ids: list[int] = Field(default_factory=list)


class ItemsReorder(BaseModel):
    """New display order for the items inside one market (full ordering)."""

    item_ids: list[int] = Field(default_factory=list)


class MarketSettings(BaseModel):
    """Per-market watch-page settings; either field may be omitted."""

    lookback: Lookback | None = None
    update_interval_minutes: UpdateInterval | None = None
