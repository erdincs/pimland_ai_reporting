"""AWS Lambda entry point.

EventBridge'den gelen event formatı:
    {"job": "incorta_sync"}
    {"job": "pimland_sync"}
    {"job": "view_refresh"}
    {"job": "master_data"}
    {"job": "validation"}
    {"job": "incorta_sync", "dry_run": true}
"""

from __future__ import annotations

import json
import logging
import os

# Lambda ortamında structlog olmayabilir — stdlib logging kullan
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

from sync.orchestrator import run_job


def handler(event: dict, context: object) -> dict:
    job_name = event.get("job")
    dry_run  = event.get("dry_run", False)

    if not job_name:
        return {
            "statusCode": 400,
            "body": json.dumps({"error": "'job' parametresi gerekli"}, ensure_ascii=False),
        }

    result = run_job(job_name, dry_run=dry_run)

    status_code = 200
    if result.get("status") == "failed":
        status_code = 500

    return {
        "statusCode": status_code,
        "body": json.dumps(result, ensure_ascii=False, default=str),
    }
