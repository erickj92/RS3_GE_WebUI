"""Pydantic models for request/response validation."""

from pydantic import BaseModel, Field


class MarketCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class MarketRename(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class ItemAdd(BaseModel):
    item_id: int = Field(gt=0)


class ItemImport(BaseModel):
    """A list of item IDs to import, in display order.

    `mode` selects how the list is applied to the market:
      - "replace" (default): clear the market's items first, then add.
      - "append": keep the existing items and add on top of them.
    """

    item_ids: list[int] = Field(default_factory=list)
    mode: str = Field(default="replace")


class RefreshAll(BaseModel):
    """Markets to refresh together in a single deduplicated job."""

    market_ids: list[int] = Field(default_factory=list)


class LookupOut(BaseModel):
    item_id: int
    name: str
    icon: str | None = None
