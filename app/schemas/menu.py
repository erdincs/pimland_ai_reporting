"""Pydantic schemas for menu management API."""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class MenuItemSchema(BaseModel):
    id: str
    label: str
    icon: str = "📄"
    active: bool = True
    navId: Optional[str] = None

    model_config = {"from_attributes": True}


class MenuGroupSchema(BaseModel):
    id: str
    label: str
    icon: str = "📁"
    active: bool = True
    order: int = 0
    navId: Optional[str] = None
    items: List[MenuItemSchema] = Field(default_factory=list)

    model_config = {"from_attributes": True}


class MenuAgentSchema(BaseModel):
    id: str
    label: str
    subtitle: str = ""
    icon: str = "🤖"
    badge: str = ""
    badgeColor: str = "#ffffff"
    active: bool = True

    model_config = {"from_attributes": True}


class MenuDataSchema(BaseModel):
    agents: List[MenuAgentSchema]
    groups: List[MenuGroupSchema]


class MenuSaveRequest(BaseModel):
    agents: List[MenuAgentSchema]
    groups: List[MenuGroupSchema]


class ToggleResponse(BaseModel):
    id: str
    active: bool
