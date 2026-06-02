"""Generic REST API connector.

Supports:
  - Auth: none | api_key | bearer | oauth2_client_credentials
  - Pagination: none | page_number | cursor | link_header | offset_limit
  - Rate limiting (requests_per_second)
  - Retry with exponential backoff (tenacity)
  - Dot-notation response path extraction (e.g. "data.items")
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Dict, List, Optional

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from app.connectors.base import BaseConnector
from app.core.logging import get_logger
from app.schemas.connector import EndpointConfig, PaginationConfig, SourceConfig

log = get_logger(__name__)


def _get_env(env_var: Optional[str], field_name: str) -> str:
    if not env_var:
        raise ValueError(f"Auth config missing env var reference for '{field_name}'.")
    val = os.environ.get(env_var, "")
    if not val:
        raise ValueError(f"Environment variable '{env_var}' is not set.")
    return val


def _dig(obj: Any, path: Optional[str]) -> Any:
    """Traverse a nested dict/list using dot-notation path."""
    if not path:
        return obj
    for key in path.split("."):
        if isinstance(obj, dict):
            obj = obj.get(key)
        elif isinstance(obj, list) and key.isdigit():
            obj = obj[int(key)]
        else:
            return None
    return obj


class RestApiConnector(BaseConnector):
    def __init__(self, config: SourceConfig) -> None:
        super().__init__(config)
        self._token_cache: Dict[str, Any] = {}

    def _build_headers(self) -> Dict[str, str]:
        auth = self.config.auth
        if not auth or auth.type == "none":
            return {}
        if auth.type == "bearer":
            token = _get_env(auth.token_env, "token_env")
            return {auth.key_header: f"{auth.key_prefix} {token}"}
        if auth.type == "api_key":
            token = _get_env(auth.token_env, "token_env")
            return {auth.key_header: token}
        return {}

    async def _get_oauth2_token(self, client: httpx.AsyncClient) -> str:
        auth = self.config.auth
        cached = self._token_cache
        if cached.get("expires_at", 0) > time.time() + 10:
            return cached["access_token"]

        token_url = _get_env(auth.token_url_env, "token_url_env")
        client_id = _get_env(auth.client_id_env, "client_id_env")
        client_secret = _get_env(auth.client_secret_env, "client_secret_env")

        resp = await client.post(
            token_url,
            data={"grant_type": "client_credentials",
                  "client_id": client_id,
                  "client_secret": client_secret},
        )
        resp.raise_for_status()
        data = resp.json()
        self._token_cache = {
            "access_token": data["access_token"],
            "expires_at": time.time() + data.get("expires_in", 3600),
        }
        return data["access_token"]

    async def _fetch_page(
        self,
        client: httpx.AsyncClient,
        endpoint: EndpointConfig,
        extra_params: Optional[Dict[str, Any]] = None,
    ) -> Any:
        params = dict(endpoint.params or {})
        if extra_params:
            params.update(extra_params)

        headers = self._build_headers()
        if self.config.auth and self.config.auth.type == "oauth2_client_credentials":
            token = await self._get_oauth2_token(client)
            headers[self.config.auth.key_header] = f"Bearer {token}"

        resp = await client.request(
            method=endpoint.method,
            url=endpoint.path,
            params=params,
            headers=headers,
        )
        resp.raise_for_status()
        return resp.json(), resp.headers

    async def _paginate(
        self,
        client: httpx.AsyncClient,
        endpoint: EndpointConfig,
    ) -> List[Dict[str, Any]]:
        pg = endpoint.pagination
        records: List[Dict[str, Any]] = []

        if not pg or pg.strategy == "none":
            raw, _ = await self._fetch_page(client, endpoint)
            items = _dig(raw, endpoint.response_data_path)
            return items if isinstance(items, list) else ([raw] if raw else [])

        if pg.strategy == "page_number":
            page = 1
            while True:
                raw, _ = await self._fetch_page(
                    client, endpoint, {pg.page_param: page, pg.page_size_param: pg.page_size}
                )
                items = _dig(raw, endpoint.response_data_path) or []
                if not items:
                    break
                records.extend(items)
                total_pages = _dig(raw, pg.total_pages_field)
                if total_pages and page >= int(total_pages):
                    break
                page += 1
                await self._maybe_throttle()

        elif pg.strategy == "cursor":
            cursor = None
            while True:
                extra = {}
                if cursor:
                    extra[pg.cursor_param or "cursor"] = cursor
                raw, _ = await self._fetch_page(client, endpoint, extra or None)
                items = _dig(raw, endpoint.response_data_path) or []
                if not items:
                    break
                records.extend(items)
                cursor = _dig(raw, pg.cursor_field)
                if not cursor:
                    break
                await self._maybe_throttle()

        elif pg.strategy == "offset_limit":
            offset = 0
            while True:
                raw, _ = await self._fetch_page(
                    client, endpoint, {"offset": offset, "limit": pg.page_size}
                )
                items = _dig(raw, endpoint.response_data_path) or []
                if not items:
                    break
                records.extend(items)
                if len(items) < pg.page_size:
                    break
                offset += pg.page_size
                await self._maybe_throttle()

        elif pg.strategy == "link_header":
            url = endpoint.path
            while url:
                resp = await client.request(
                    method=endpoint.method, url=url,
                    params=endpoint.params, headers=self._build_headers(),
                )
                resp.raise_for_status()
                raw = resp.json()
                items = _dig(raw, endpoint.response_data_path) or []
                records.extend(items)
                link = resp.headers.get("Link", "")
                url = self._parse_next_link(link)
                await self._maybe_throttle()

        return records

    @staticmethod
    def _parse_next_link(link_header: str) -> Optional[str]:
        for part in link_header.split(","):
            parts = [p.strip() for p in part.split(";")]
            if len(parts) == 2 and 'rel="next"' in parts[1]:
                return parts[0].strip("<>")
        return None

    async def _maybe_throttle(self) -> None:
        conn = self.config.connection
        rps = getattr(conn, "requests_per_second", None) if conn else None
        if rps:
            await asyncio.sleep(1.0 / rps)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    async def fetch(self) -> List[Dict[str, Any]]:
        conn = self.config.connection
        if not conn:
            raise ValueError(f"[{self.source_id}] No connection config.")

        timeout = getattr(conn, "timeout_seconds", 30)
        base_url = getattr(conn, "base_url", "")

        async with httpx.AsyncClient(base_url=base_url, timeout=timeout) as client:
            all_records: List[Dict[str, Any]] = []
            for endpoint in (self.config.endpoints or []):
                records = await self._paginate(client, endpoint)
                all_records.extend(records)
                log.info("rest_api.fetched", source=self.source_id,
                         endpoint=endpoint.path, count=len(records))
        return all_records

    async def health_check(self) -> bool:
        conn = self.config.connection
        if not conn:
            return False
        try:
            async with httpx.AsyncClient(
                base_url=getattr(conn, "base_url", ""),
                timeout=5,
            ) as client:
                resp = await client.get("/")
                return resp.status_code < 500
        except Exception:  # noqa: BLE001
            return False
