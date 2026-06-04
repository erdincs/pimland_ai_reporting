"""CloudWatch alarm ve SNS kurulumu.

Tek seferlik çalıştır:
    python -m app.core.alarms

Veya uygulama başlangıcında:
    from app.core.alarms import ensure_alarms
    ensure_alarms()  # zaten varsa atlar
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

_REGION      = os.environ.get("AWS_REGION", "eu-north-1")
_ALERT_EMAIL = os.environ.get("ALERT_EMAIL", "")
_ACCOUNT_ID  = "448049806345"
_SNS_TOPIC   = f"arn:aws:sns:{_REGION}:{_ACCOUNT_ID}:pimland-alerts"

_ALARMS = [
    {
        "AlarmName":          "pimland-sync-failure",
        "AlarmDescription":   "3 üst üste sync job başarısız",
        "Namespace":          "Pimland/Sync",
        "MetricName":         "SyncJobFailed",
        "Statistic":          "Sum",
        "Period":             3600,
        "EvaluationPeriods":  3,
        "Threshold":          1.0,
        "ComparisonOperator": "GreaterThanOrEqualToThreshold",
        "TreatMissingData":   "notBreaching",
    },
    {
        "AlarmName":          "pimland-mcp-timeout",
        "AlarmDescription":   "5 dakikada 3+ MCP timeout",
        "Namespace":          "Pimland/Agent",
        "MetricName":         "McpTimeout",
        "Statistic":          "Sum",
        "Period":             300,
        "EvaluationPeriods":  1,
        "Threshold":          3.0,
        "ComparisonOperator": "GreaterThanOrEqualToThreshold",
        "TreatMissingData":   "notBreaching",
    },
    {
        "AlarmName":          "pimland-guard-anomaly",
        "AlarmDescription":   "1 saatte 10+ injection girişimi",
        "Namespace":          "Pimland/Guard",
        "MetricName":         "InjectionAttempt",
        "Statistic":          "Sum",
        "Period":             3600,
        "EvaluationPeriods":  1,
        "Threshold":          10.0,
        "ComparisonOperator": "GreaterThanOrEqualToThreshold",
        "TreatMissingData":   "notBreaching",
    },
]


def _boto3_clients():
    import boto3
    sns = boto3.client("sns", region_name=_REGION)
    cw  = boto3.client("cloudwatch", region_name=_REGION)
    return sns, cw


def ensure_sns_topic() -> str:
    """SNS topic oluştur (yoksa), ARN döndür."""
    sns, _ = _boto3_clients()
    resp = sns.create_topic(Name="pimland-alerts")
    arn  = resp["TopicArn"]

    if _ALERT_EMAIL:
        try:
            sns.subscribe(TopicArn=arn, Protocol="email", Endpoint=_ALERT_EMAIL)
            log.info("alarms.sns_subscribed email=%s", _ALERT_EMAIL)
        except Exception as e:
            log.warning("alarms.sns_subscribe_failed err=%s", e)

    return arn


def ensure_alarms() -> None:
    """Alarm'ları oluştur (zaten varsa atlar)."""
    if not os.environ.get("CLOUDWATCH_ENABLED", "false").lower() == "true":
        log.info("alarms.skipped CLOUDWATCH_ENABLED=false")
        return

    try:
        topic_arn = ensure_sns_topic()
        _, cw = _boto3_clients()

        for alarm in _ALARMS:
            cw.put_metric_alarm(
                **alarm,
                ActionsEnabled=True,
                AlarmActions=[topic_arn],
                OKActions=[topic_arn],
            )
            log.info("alarms.created name=%s", alarm["AlarmName"])

    except Exception as e:
        log.error("alarms.setup_failed err=%s", e)


def publish_metric(namespace: str, metric: str, value: float = 1.0) -> None:
    """CloudWatch custom metric yay (alarm için)."""
    if not os.environ.get("CLOUDWATCH_ENABLED", "false").lower() == "true":
        return
    try:
        _, cw = _boto3_clients()
        cw.put_metric_data(
            Namespace=namespace,
            MetricData=[{
                "MetricName": metric,
                "Value": value,
                "Unit": "Count",
            }],
        )
    except Exception as e:
        log.warning("alarms.metric_failed metric=%s err=%s", metric, e)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ensure_alarms()
    print("Alarmlar kuruldu.")
