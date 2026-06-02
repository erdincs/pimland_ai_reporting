"""Abstract base for all data source connectors."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List

from app.schemas.connector import SourceConfig


class BaseConnector(ABC):
    """Every connector must implement `fetch` and `health_check`."""

    def __init__(self, config: SourceConfig) -> None:
        self.config = config
        self.source_id = config.source_id
        self.target_table = config.target_table

    @abstractmethod
    async def fetch(self) -> List[Dict[str, Any]]:
        """Pull all records from the source. Returns a list of raw dicts."""

    @abstractmethod
    async def health_check(self) -> bool:
        """Return True if the source is reachable."""
