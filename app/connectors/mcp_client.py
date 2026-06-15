"""AgentUp MCP connector.

AgentUp wraps REST APIs as MCP-style tool servers. Their calling convention:

  GET  {base_url}/tools                    → list available tools
  GET  {base_url}/tools/{tool_name}        → get tool schema
  POST {base_url}/tools/{tool_name}/execute → execute a tool

Response envelope:
  {
    "content": {
      "success": true,
      "statusCode": 200,
      "data": {
        "result": <payload>,
        "isSuccessful": true
      }
    }
  }

Use `response_data_path: "content.data.result"` in YAML to extract the payload.
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
from app.schemas.connector import SourceConfig

log = get_logger(__name__)


class McpConnector(BaseConnector):
    """Calls an AgentUp MCP tool via POST .../execute and returns a list of dicts."""

    def __init__(self, config: SourceConfig) -> None:
        super().__init__(config)
        self._token_cache: Dict[str, Any] = {}

    # ── OAuth2 ROPC (Resource Owner Password Credentials) ───────────────────

    def _env(self, key: Optional[str], label: str) -> str:
        if not key:
            raise ValueError(f"[{self.source_id}] Auth config missing '{label}'.")
        val = os.environ.get(key, "")
        if not val:
            raise ValueError(f"[{self.source_id}] Env var '{key}' is not set.")
        return val

    async def _get_token(self, client: httpx.AsyncClient) -> Optional[str]:
        """Fetch a Bearer token if auth is configured, else return None."""
        auth = self.config.auth
        if not auth or auth.type == "none":
            return None

        cached = self._token_cache
        if cached.get("expires_at", 0) > time.time() + 30:
            return cached["access_token"]

        if auth.type in ("bearer", "api_key"):
            return self._env(auth.token_env, "token_env")

        if auth.type == "oauth2_password_grant":
            token_url   = self._env(getattr(auth, "token_url_env", None),      "token_url_env")
            client_id   = self._env(getattr(auth, "client_id_env", None),      "client_id_env")
            client_secret = self._env(getattr(auth, "client_secret_env", None),"client_secret_env")
            username    = self._env(getattr(auth, "username_env", None),       "username_env")
            password    = self._env(getattr(auth, "password_env", None),       "password_env")
            # scope: literal string or from env var
            scope_env = getattr(auth, "scope_env", None)
            scope = os.environ.get(scope_env, "") if scope_env else (getattr(auth, "scope", "") or "")

            resp = await client.post(token_url, data={
                "grant_type":    "password",
                "client_id":     client_id,
                "client_secret": client_secret,
                "username":      username,
                "password":      password,
                "scope":         scope,
            })
            resp.raise_for_status()
            data = resp.json()
            self._token_cache = {
                "access_token": data["access_token"],
                "expires_at":   time.time() + data.get("expires_in", 3600),
            }
            log.info("mcp.token_refreshed", source=self.source_id)
            return data["access_token"]

        if auth.type == "oauth2_client_credentials":
            token_url     = self._env(auth.token_url_env,     "token_url_env")
            client_id     = self._env(auth.client_id_env,     "client_id_env")
            client_secret = self._env(auth.client_secret_env, "client_secret_env")
            resp = await client.post(token_url, data={
                "grant_type":    "client_credentials",
                "client_id":     client_id,
                "client_secret": client_secret,
            })
            resp.raise_for_status()
            data = resp.json()
            self._token_cache = {
                "access_token": data["access_token"],
                "expires_at":   time.time() + data.get("expires_in", 3600),
            }
            return data["access_token"]

        return None

    def _resolve_args(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Replace __env:VAR_NAME placeholders in args with env values."""
        resolved = {}
        for k, v in args.items():
            if isinstance(v, str) and v.startswith("__env:"):
                env_var = v[6:]
                resolved[k] = os.environ.get(env_var, "")
            else:
                resolved[k] = v
        return resolved

    def _set_nested(self, d: Dict[str, Any], path: str, value: Any) -> None:
        """Set a value in a nested dict using dot-notation."""
        keys = path.split(".")
        for key in keys[:-1]:
            d = d.setdefault(key, {})
        d[keys[-1]] = value

    # ── Tool execution ───────────────────────────────────────────────────────

    async def _execute_once(
        self,
        client: httpx.AsyncClient,
        execute_url: str,
        headers: Dict[str, str],
        body: Dict[str, Any],
    ) -> Any:
        resp = await client.post(execute_url, headers=headers, json=body)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            success = data.get("content", {}).get("success", True)
            if not success:
                err = data.get("content", {}).get("error", "unknown")
                raise RuntimeError(f"[{self.source_id}] Tool error: {err}")
        return data

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    async def fetch(
        self,
        extra_prompts: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch records from the MCP tool.

        extra_prompts: additional Incorta filter prompts injected at runtime
        (used by the incremental sync pipeline to pass date range filters).
        """
        conn = self.config.connection
        tool = self.config.tool
        if not conn or not tool:
            raise ValueError(f"[{self.source_id}] Requires connection.url and tool.name.")

        base_url: str = getattr(conn, "url", "")
        timeout: int  = getattr(conn, "timeout_seconds", 60)
        execute_url   = f"{base_url.rstrip('/')}/tools/{tool.name}/execute"

        async with httpx.AsyncClient(timeout=timeout) as client:
            token = await self._get_token(client)
            headers: Dict[str, str] = {}
            if token:
                headers["Authorization"] = f"Bearer {token}"

            base_args = self._resolve_args(tool.args or {})

            # Inject extra_prompts (e.g. date filter for incremental sync)
            if extra_prompts:
                existing = base_args.get("prompts", [])
                base_args["prompts"] = list(existing) + extra_prompts

            # ── Pagination support ──────────────────────────────────────────
            if tool.pagination_start_field and tool.pagination_size_field:
                all_rows: List[Dict[str, Any]] = []
                page_size = tool.pagination_page_size
                total: Optional[int] = None

                # Detect mode: "pageNumber" = 1-based page, otherwise offset-based
                is_page_based = "pageNumber" in tool.pagination_start_field
                cursor = 1 if is_page_based else 0

                while True:
                    body = dict(base_args)
                    self._set_nested(body, tool.pagination_start_field, cursor)
                    self._set_nested(body, tool.pagination_size_field, page_size)
                    data = await self._execute_once(client, execute_url, headers, body)

                    if total is None and tool.total_rows_path:
                        total = self._dig(data, tool.total_rows_path)
                        log.info("mcp.paginate_start", source=self.source_id,
                                 total=total, page_size=page_size)

                    rows = self._parse_response(data, tool)
                    if not rows:
                        break
                    all_rows.extend(rows)

                    if is_page_based:
                        cursor += 1
                        # total_rows_path gives totalPageCount for page-based
                        total_pages = self._dig(data,
                            "content.data.result.totalPageCount") if is_page_based else None
                        if total_pages and cursor > int(total_pages):
                            break
                    else:
                        cursor += page_size
                        if total is not None and cursor >= total:
                            break

                log.info("mcp.fetched", source=self.source_id, tool=tool.name,
                         count=len(all_rows))
                return all_rows

            # ── Single-page fetch ────────────────────────────────────────────
            data = await self._execute_once(client, execute_url, headers, base_args)

        rows = self._parse_response(data, tool)
        log.info("mcp.fetched", source=self.source_id, tool=tool.name, count=len(rows))
        return rows

    def _parse_response(self, data: Any, tool: Any) -> List[Dict[str, Any]]:
        """Route to the correct parser based on response_format."""
        fmt = getattr(tool, "response_format", None)
        if fmt == "incorta":
            return self._parse_incorta(data)
        if fmt in ("pimland_products", "pimland_products_squ"):
            return self._parse_pimland_products(data)
        return self._extract(data, tool.response_data_path)

    @staticmethod
    def _parse_pimland_products(data: Any) -> List[Dict[str, Any]]:
        """Parse Pimland product catalog response (both filter and squ endpoints).

        productImages takes priority for image names; barcodes used as fallback.
        Image URL: https://img-adl.sm.mncdn.com/cdnimages/products/{img.name}
        """
        _IMG_BASE = "https://img-adl.sm.mncdn.com/cdnimages/products"
        try:
            products = data["content"]["data"]["result"]["products"]
        except (KeyError, TypeError):
            return []
        rows = []
        for p in (products or []):
            # Collect color codes from barcodes
            barcodes = p.get("barcodes") or []
            color_codes_set = {b.get("colorCode", "") for b in barcodes if b.get("colorCode")}
            color_codes = sorted(color_codes_set)

            # Get best image: use productImages if available
            images = p.get("productImages") or []
            web_imgs = [i for i in images
                        if not i.get("isDeleted") and "Web" in (i.get("type") or "")]
            first_img_name = web_imgs[0]["name"] if web_imgs else None
            first_color = (web_imgs[0]["colorCode"] if web_imgs
                           else (color_codes[0] if color_codes else None))
            default_image = f"{_IMG_BASE}/{first_img_name}" if first_img_name else (
                f"{_IMG_BASE}/{p['stockCode']}_{first_color}_1.jpg" if first_color else None
            )

            rows.append({
                "stockCode":            p.get("stockCode"),
                "description":          p.get("description"),
                "seasonCode":           p.get("seasonCode"),
                "seasonName":           p.get("seasonName"),
                "brandCode":            p.get("brandCode"),
                "brandName":            p.get("brandName"),
                "productGroupCode":     p.get("productGroupCode"),
                "productGroupName":     p.get("productGroupName"),
                "productMainGroupCode": p.get("productMainGroupCode"),
                "productMainGroupName": p.get("productMainGroupName"),
                "productThemeCode":     p.get("productThemeCode"),
                "productThemeName":     p.get("productThemeName"),
                "fabricMaterialName":   p.get("fabricMaterialName"),
                "isBlocked":            p.get("isBlocked", False),
                "useInternet":          p.get("useInternet", True),
                "color_codes":          ",".join(color_codes),
                "first_color_code":     first_color,
                "default_image_url":    default_image,
            })
        return rows

    @staticmethod
    def _dig(obj: Any, path: Optional[str]) -> Any:
        if not path:
            return obj
        for key in path.split("."):
            if isinstance(obj, dict):
                obj = obj.get(key)
            else:
                return None
        return obj

    def _extract(self, data: Any, path: Optional[str]) -> List[Dict[str, Any]]:
        data = self._dig(data, path)
        if isinstance(data, list):
            return [r for r in data if isinstance(r, dict)]
        if isinstance(data, dict):
            return [data]
        return []

    @staticmethod
    def _parse_incorta(data: Any) -> List[Dict[str, Any]]:
        """Parse Incorta columnar response: headers + data arrays → dict list.

        Supports two response envelopes:
          Legacy: data["content"]["data"] → {headers, data}
          Direct: data["data"]            → {headers, data}  (newer tools)

        "--" values in data rows are converted to None.
        """
        payload = None
        try:
            payload = data["content"]["data"]
        except (KeyError, TypeError):
            pass
        if payload is None:
            try:
                payload = data["data"]
            except (KeyError, TypeError):
                return []

        try:
            headers  = payload["headers"]
            rows_raw = payload["data"]
        except (KeyError, TypeError):
            return []

        dims     = headers.get("dimensions", [])
        measures = headers.get("measures", [])
        all_cols = dims + measures

        from app.ingestion.normalizer import normalise_column_name
        col_names = [normalise_column_name(c["label"]) for c in all_cols]

        def clean(v: Any) -> Any:
            return None if v == "--" else v

        return [
            {k: clean(v) for k, v in zip(col_names, row)}
            for row in (rows_raw or [])
        ]

    async def health_check(self) -> bool:
        conn = self.config.connection
        if not conn:
            return False
        url = f"{getattr(conn, 'url', '').rstrip('/')}/tools"
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                token = await self._get_token(client)
                headers = {"Authorization": f"Bearer {token}"} if token else {}
                resp = await client.get(url, headers=headers)
                return resp.status_code == 200
        except Exception:  # noqa: BLE001
            return False
