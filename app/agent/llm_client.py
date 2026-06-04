"""AWS Bedrock Converse API wrapper.

Uses boto3 `bedrock-runtime` directly — the Converse API is the official,
model-agnostic way to call Claude (and other models) on Bedrock.

Authentication follows the standard boto3 credential chain:
  1. AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY env vars / .env  (local dev)
  2. ~/.aws/credentials profile                                  (local dev)
  3. EC2 / ECS / Lambda instance / task role                    (production)

The boto3 client is synchronous; we offload calls to a thread-pool executor
so FastAPI's async event loop is never blocked.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Dict, List, Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import settings
from app.core.exceptions import LLMServiceError
from app.core.logging import get_logger

log = get_logger(__name__)

# Shared executor — keeps thread count bounded.
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="bedrock")


class LLMClient:
    def __init__(self) -> None:
        self._model_id = settings.bedrock_model_id
        self._max_tokens = settings.anthropic_max_tokens
        self._client: "object | None" = None

    def _get_client(self) -> object:
        if self._client is None:
            kwargs: dict = {"region_name": settings.bedrock_region}
            if settings.aws_access_key_id:
                kwargs["aws_access_key_id"] = settings.aws_access_key_id
                kwargs["aws_secret_access_key"] = settings.aws_secret_access_key
            self._client = boto3.client("bedrock-runtime", **kwargs)
        return self._client

    def _invoke(
        self,
        system: str,
        user: str,
        max_tokens: int,
        temperature: float,
        history: Optional[List[Dict]] = None,
    ) -> str:
        """Synchronous Converse call — runs inside the thread executor."""
        # Build messages: prior turns + current user message
        # user may be a plain string OR a list of content blocks (file upload)
        messages: List[Dict] = []
        for turn in (history or []):
            role = turn.get("role", "user")
            content = turn.get("content", "")
            messages.append({"role": role, "content": [{"text": str(content)}]})

        if isinstance(user, list):
            # Multi-content: text + documents + images
            user_content = []
            for block in user:
                btype = block.get("type")
                if btype == "text":
                    user_content.append({"text": block["text"]})
                elif btype == "image":
                    user_content.append({"image": {
                        "format": block["source"]["media_type"].split("/")[-1],
                        "source": {"bytes": __import__("base64").b64decode(block["source"]["data"])},
                    }})
                elif btype == "document":
                    user_content.append({"document": {
                        "name":   block.get("title", "document"),
                        "format": "txt",
                        "source": {"bytes": block["source"]["data"].encode("utf-8")},
                    }})
            messages.append({"role": "user", "content": user_content})
        else:
            messages.append({"role": "user", "content": [{"text": user}]})

        try:
            resp = self._get_client().converse(
                modelId=self._model_id,
                system=[{"text": system}],
                messages=messages,
                inferenceConfig={
                    "maxTokens": max_tokens,
                    "temperature": temperature,
                },
            )
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            msg = exc.response["Error"]["Message"]
            log.error("llm.client_error", code=code, message=msg)
            raise LLMServiceError(f"Bedrock error: {code}") from exc
        except BotoCoreError as exc:
            log.error("llm.botocore_error", error=str(exc))
            raise LLMServiceError() from exc

        try:
            return resp["output"]["message"]["content"][0]["text"]
        except (KeyError, IndexError) as exc:
            log.error("llm.unexpected_response", resp=str(resp))
            raise LLMServiceError("Unexpected response structure from Bedrock.") from exc

    async def complete(
        self,
        *,
        system: str,
        user: str,
        max_tokens: Optional[int] = None,
        temperature: float = 0.0,
        history: Optional[List[Dict]] = None,
    ) -> str:
        """Async completion — offloads the blocking boto3 call to a thread."""
        loop = asyncio.get_event_loop()
        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(
                    _executor,
                    partial(
                        self._invoke,
                        system,
                        user,
                        max_tokens or self._max_tokens,
                        temperature,
                        history,
                    ),
                ),
                timeout=settings.llm_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            log.warning("llm.timeout", timeout=settings.llm_timeout_seconds)
            raise LLMServiceError("The analysis service timed out.") from exc

        log.info("llm.complete", model=self._model_id[:60])
        return result


# Module-level singleton — boto3 client is thread-safe.
llm_client = LLMClient()
