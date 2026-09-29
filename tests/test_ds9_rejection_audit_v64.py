from __future__ import annotations

import csv
import io
from datetime import datetime

from src.ds9_rejection_audit import export_rejections_csv, record_rejection


def test_audit_csv_is_bom_encoded_escaped_and_profile_local(isolated_home) -> None:
    record_rejection(
        dynamic_id="100000000000000001",
        canonical_url="https://www.bilibili.com/opus/100000000000000001",
        source_topic_id="47801",
        source_topic_name="抽奖号的日常",
        classifier_text='第一行, 含逗号\n第二行 "含引号"',
        seen_at=100,
    )
    filename, content = export_rejections_csv(
        source_topic_id="47801",
        now=datetime(2026, 9, 29, 12, 34),
    )

    assert filename == "binggo-ds9-rejection-audit-20260929-1234.csv"
    assert content.startswith(b"\xef\xbb\xbf")
    rows = list(csv.DictReader(io.StringIO(content.decode("utf-8-sig"))))
    assert rows == [{
        "dynamic_id": "100000000000000001",
        "topic_id": "47801",
        "topic_name": "抽奖号的日常",
        "canonical_url": "https://www.bilibili.com/opus/100000000000000001",
        "classifier_text": '第一行, 含逗号\n第二行 "含引号"',
        "classification_result": "非抽奖活动",
        "classification_reason": "非抽奖活动",
        "first_seen_at": "100",
        "last_seen_at": "100",
    }]


def test_empty_audit_csv_keeps_header_and_does_not_mutate_store(isolated_home) -> None:
    filename, content = export_rejections_csv(now=datetime(2026, 9, 29, 12, 34))

    assert filename == "binggo-ds9-rejection-audit-20260929-1234.csv"
    reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")))
    assert reader.fieldnames == [
        "dynamic_id",
        "topic_id",
        "topic_name",
        "canonical_url",
        "classifier_text",
        "classification_result",
        "classification_reason",
        "first_seen_at",
        "last_seen_at",
    ]
    assert list(reader) == []
