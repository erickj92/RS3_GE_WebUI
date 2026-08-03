"""Pydantic models for request/response validation."""

from pydantic import BaseModel, Field


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
