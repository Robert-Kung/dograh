"""ccp W4c call records — list, detail and audio against the real test database.

Rows are inserted in 2021 so nothing else in the test DB falls into the
queried ranges; ``now`` is pinned accordingly (same convention as the W4b
usage-report tests, whose fixture shape this mirrors).
"""

import asyncio
import types
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import text

from api.db.models import (
    CcpCallMetaModel,
    OrganizationModel,
    RecordingRetentionAuditModel,
    UserModel,
    WorkflowRunModel,
)
from api.services.ccp import call_records as cr
from api.services.ccp import caller_identity as ci
from api.services.ccp import usage_report as ur

NOW = datetime(2021, 11, 20, 4, 0, tzinfo=UTC)
TZ = "Asia/Taipei"
CODES = ["general_inquiry", "billing_inquiry"]
KEY = bytes(range(32))


@pytest.fixture(autouse=True)
def hmac_key(monkeypatch):
    monkeypatch.setenv("CALLER_NUMBER_HMAC_KEY", KEY.hex())
    monkeypatch.setenv("RECORD_TRANSCRIPT_RETENTION_DAYS", "30")


@pytest.fixture
async def workflow(db_session, async_session):
    org = OrganizationModel(provider_id="test-org-call-records")
    async_session.add(org)
    await async_session.flush()
    user = UserModel(
        provider_id="test-user-call-records", selected_organization_id=org.id
    )
    async_session.add(user)
    await async_session.flush()
    return await db_session.create_workflow(
        name="Call Records Workflow",
        workflow_definition={"nodes": [], "edges": []},
        user_id=user.id,
        organization_id=org.id,
    )


@pytest.fixture
def add_run(workflow, async_session):
    async def _add(
        created_at: datetime,
        *,
        outcome: str | None = "ai_completed",
        completed: bool = True,
        mode: str = "livekit",
        call_type: str = "inbound",
        seconds=60,
        disposition: str | None = None,
        caller: str | None = None,
        audio_started_at: datetime | None = None,
        recording_url: str | None = None,
        annotations: dict | None = None,
        gathered: dict | None = None,
        logs: dict | None = None,
        did: str | None = "+886212345678",
    ) -> int:
        gathered_context = dict(gathered or {})
        if disposition:
            gathered_context["mapped_call_disposition"] = disposition
        run = WorkflowRunModel(
            name="call-records-run",
            workflow_id=workflow.id,
            mode=mode,
            call_type=call_type,
            is_completed=completed,
            created_at=created_at,
            annotations={
                **({"call_outcome": outcome} if outcome else {}),
                **(annotations or {}),
            },
            usage_info={} if seconds is None else {"call_duration_seconds": seconds},
            gathered_context=gathered_context,
            logs=logs or {},
            recording_url=recording_url,
            storage_backend="minio",
            initial_context={"did": did} if did else {},
        )
        async_session.add(run)
        await async_session.flush()
        if caller or audio_started_at:
            e164 = ci.normalize_caller(caller) if caller else None
            async_session.add(
                CcpCallMetaModel(
                    workflow_run_id=run.id,
                    caller_masked=ci.mask(e164) if e164 else None,
                    caller_last4=ci.last4(e164) if e164 else None,
                    caller_hmac=ci.caller_hmac(KEY, e164) if e164 else None,
                    audio_started_at=audio_started_at,
                )
            )
            await async_session.flush()
        return run.id

    return _add


def _at(y, m, d, hh=2, mm=0, ss=0) -> datetime:
    # 02:00 UTC = 10:00 Taipei, safely inside the local day.
    return datetime(y, m, d, hh, mm, ss, tzinfo=UTC)


def _q(date_from=date(2021, 11, 1), date_to=date(2021, 11, 20), **kw) -> cr.CallQuery:
    kw.setdefault("codes", CODES)
    return cr.validate_query(
        date_from=date_from, date_to=date_to, timezone=TZ, now=NOW, **kw
    )


async def _list(**kw) -> dict:
    return await cr.query_calls(_q(**kw), timezone=TZ, now=NOW)


# --- list: scope and parity with the usage report (2.2) ---


@pytest.fixture
async def mixed_month(add_run):
    rows = [
        ("ai_completed", "general_inquiry", 3),
        ("ai_completed", "billing_inquiry", 2),
        ("ai_completed", None, 1),
        ("transferred:press0", "billing_inquiry", 2),
        ("transferred:safetynet", None, 1),
        ("transfer_failed:no_agent", "general_inquiry", 2),
        ("transfer_failed:press0_not_installed", None, 1),
        ("safetynet_terminated", "Other Thing", 1),
        (None, None, 1),
    ]
    for outcome, disposition, n in rows:
        for i in range(n):
            await add_run(
                _at(2021, 11, 3 + i),
                outcome=outcome,
                completed=outcome is not None,
                disposition=disposition,
            )
    # Stale run (no outcome, never completed, > 6 h old) is in scope.
    await add_run(_at(2021, 11, 10), outcome=None, completed=False)
    # Out of scope: editor test call, outbound, in progress.
    await add_run(_at(2021, 11, 4), mode="smallwebrtc")
    await add_run(_at(2021, 11, 4), call_type="outbound")
    await add_run(NOW - timedelta(minutes=10), outcome=None, completed=False)


async def test_totals_match_usage_report_cell_by_cell(mixed_month):
    report = await ur.build_usage_report(
        ur.validate_query(date(2021, 11, 1), date(2021, 11, 20), TZ, CODES, now=NOW),
        now=NOW,
    )
    assert (await _list())["total"] == report["calls"] == 15
    for outcome, n in report["outcomes"].items():
        assert (await _list(outcome=outcome))["total"] == n, outcome
    for code, split in report["categories"].items():
        assert (await _list(category=code))["total"] == split["calls"], code
    assert (await _list(uncategorized=True))["total"] == report["unclassified"]["calls"]
    # Outcome × category cross: the sum over categories equals the outcome.
    for outcome, n in report["outcomes"].items():
        parts = [(await _list(outcome=outcome, category=c))["total"] for c in CODES]
        parts.append((await _list(outcome=outcome, uncategorized=True))["total"])
        assert sum(parts) == n


async def test_out_of_scope_runs_never_listed(add_run):
    keep = await add_run(_at(2021, 11, 5))
    test_call = await add_run(_at(2021, 11, 5), mode="smallwebrtc")
    ids = {r["run_id"] for r in (await _list())["rows"]}
    assert keep in ids and test_call not in ids


async def test_category_outside_whitelist_folds_to_null(add_run):
    await add_run(_at(2021, 11, 5), disposition="secret free text 0912345678")
    rows = (await _list())["rows"]
    assert [r["category"] for r in rows] == [None]


async def test_sort_by_ai_seconds_is_stable_across_pages(add_run):
    ids = []
    for i in range(120):
        ids.append(
            await add_run(_at(2021, 11, 2 + i % 15), seconds=[30, 60, 90][i % 3])
        )
    seen = []
    first = await _list(sort="ai_seconds", order="desc")
    assert first["pages"] == 3 and first["total"] == 120
    for page in (1, 2, 3):
        rows = (await _list(sort="ai_seconds", order="desc", page=page))["rows"]
        seen.extend((r["ai_seconds"], r["run_id"]) for r in rows)
    assert len(seen) == 120 and {rid for _, rid in seen} == set(ids)
    assert seen == sorted(seen, reverse=True)
    assert seen[0][0] == 90


async def test_default_sort_is_newest_first(add_run):
    old = await add_run(_at(2021, 11, 2))
    new = await add_run(_at(2021, 11, 9))
    assert [r["run_id"] for r in (await _list())["rows"]] == [new, old]


async def test_page_past_the_end_returns_last_page(add_run):
    for i in range(51):
        await add_run(_at(2021, 11, 2 + i % 10))
    result = await _list(page=9)
    assert result["page"] == 2 and len(result["rows"]) == 1


async def test_empty_period_is_one_empty_page(add_run):
    result = await _list()
    assert (result["total"], result["page"], result["pages"], result["rows"]) == (
        0,
        1,
        1,
        [],
    )


async def test_dirty_seconds_are_zero(add_run):
    await add_run(_at(2021, 11, 5), seconds="12")
    await add_run(_at(2021, 11, 5), seconds=None)
    assert [r["ai_seconds"] for r in (await _list())["rows"]] == [0, 0]


# --- caller search (2.2) ---


async def test_caller_search_by_every_spelling_and_last4(add_run):
    hit = await add_run(_at(2021, 11, 5), caller="+886912345678")
    other = await add_run(_at(2021, 11, 5), caller="+886922335678")
    await add_run(_at(2021, 11, 5), caller="+886933000000")
    for spelling in (
        "0912-345-678",
        "0912345678",
        "+886 912 345 678",
        "+8860912345678",
    ):
        rows = (await _list(caller=spelling))["rows"]
        assert [r["run_id"] for r in rows] == [hit], spelling
        assert rows[0]["caller_masked"] == "***5678"
    assert {r["run_id"] for r in (await _list(caller="5678"))["rows"]} == {hit, other}


async def test_foreign_caller(add_run):
    hit = await add_run(_at(2021, 11, 5), caller="+14155551234")
    assert [r["run_id"] for r in (await _list(caller="+1 415 555 1234"))["rows"]] == [
        hit
    ]


@pytest.mark.parametrize("raw", ["０９１２３４５６７８", "abc", "123", "sip:1@x"])
def test_malformed_caller_is_a_fixed_code(raw):
    with pytest.raises(cr.CallRecordsInvalid, match="^caller_invalid$"):
        _q(caller=raw)


def test_full_number_without_key_is_disabled(monkeypatch):
    monkeypatch.delenv("CALLER_NUMBER_HMAC_KEY")
    with pytest.raises(cr.CallRecordsInvalid, match="^caller_search_disabled$"):
        _q(caller="0912345678")
    _q(caller="5678")  # last 4 still works


async def test_search_metadata(add_run, monkeypatch, async_session):
    await add_run(_at(2021, 11, 5), caller="+886912345678")
    result = await _list()
    assert result["caller_search"] == "full" and result["key_mismatch"] is False
    assert result["caller_coverage_from"] is not None
    await async_session.execute(
        text(
            "INSERT INTO ccp_settings (id, key_fingerprint) VALUES (1, 'deadbeef')"
            " ON CONFLICT (id) DO UPDATE SET key_fingerprint = 'deadbeef'"
        )
    )
    assert (await _list())["key_mismatch"] is True
    monkeypatch.delenv("CALLER_NUMBER_HMAC_KEY")
    result = await _list()
    assert result["caller_search"] == "last4_only" and result["key_mismatch"] is False


async def test_recording_enabled_follows_the_notice_setting(monkeypatch):
    monkeypatch.delenv("RECORD_CONSENT_NOTICE_TEXT", raising=False)
    assert (await _list())["recording_enabled"] is False
    monkeypatch.setenv("RECORD_CONSENT_NOTICE_TEXT", "本通話將錄音")
    assert (await _list())["recording_enabled"] is True


# --- validation ---


@pytest.mark.parametrize(
    "kw,code",
    [
        (
            {"date_from": date(2021, 1, 1), "date_to": date(2021, 4, 3)},
            "period_invalid",
        ),
        (
            {"date_from": date(2021, 11, 21), "date_to": date(2021, 11, 21)},
            "period_invalid",
        ),
        ({"outcome": "transferred:press0"}, "query_invalid"),
        ({"category": "not_in_codes"}, "query_invalid"),
        ({"category": "general_inquiry", "uncategorized": True}, "query_invalid"),
        ({"sort": "caller"}, "query_invalid"),
        ({"order": "sideways"}, "query_invalid"),
        ({"page": 0}, "query_invalid"),
        ({"codes": ["Bad Code"]}, "query_invalid"),
    ],
)
def test_validation_codes(kw, code):
    with pytest.raises(cr.CallRecordsInvalid, match=f"^{code}$"):
        _q(**kw)


def test_caller_widens_the_period_to_366_days():
    wide = {"date_from": date(2020, 11, 20), "date_to": date(2021, 11, 20)}
    _q(caller="0912345678", **wide)  # 366 days
    _q(caller="5678", date_from=date(2021, 1, 1), date_to=date(2021, 10, 27))  # 300
    with pytest.raises(cr.CallRecordsInvalid):
        _q(
            caller="0912345678",
            date_from=date(2020, 11, 19),
            date_to=date(2021, 11, 20),
        )
    with pytest.raises(cr.CallRecordsInvalid):
        _q(**wide)


@pytest.mark.parametrize(
    "raw,ok",
    [
        ("1", 1),
        ("2147483647", 2147483647),
        ("2147483648", None),
        ("9999999999", None),
        ("0", None),
        ("012", None),
        ("-1", None),
        ("abc", None),
        ("１２", None),
        ("", None),
    ],
)
def test_parse_run_id(raw, ok):
    assert cr.parse_run_id(raw) == ok


# --- detail (2.3, 2.4) ---


async def _detail(run_id, codes=CODES):
    return await cr.get_call(run_id, tuple(codes), now=NOW)


async def test_detail_out_of_scope_is_none(add_run):
    test_call = await add_run(_at(2021, 11, 5), mode="smallwebrtc")
    in_progress = await add_run(
        NOW - timedelta(minutes=5), outcome=None, completed=False
    )
    assert await _detail(test_call) is None
    assert await _detail(in_progress) is None
    assert await _detail(2_000_000_000) is None


async def test_recording_status_states(add_run, async_session):
    available = await add_run(_at(2021, 11, 5), recording_url="recordings/1.wav")
    expired = await add_run(_at(2021, 11, 5))
    legacy_expired = await add_run(_at(2021, 11, 5))
    legacy_transcript_only = await add_run(_at(2021, 11, 5))
    failed = await add_run(
        _at(2021, 11, 5),
        annotations={
            "consent_notice": {"failed_at": "x", "failed_reason": "playback_error"}
        },
    )
    disabled = await add_run(
        _at(2021, 11, 5), annotations={"consent_notice": {"disabled": True}}
    )
    safetynet = await add_run(_at(2021, 11, 5), outcome="transferred:safetynet")
    for run_id, scope, keys in [
        (expired, "audio", ["recordings/2.wav"]),
        (legacy_expired, None, ["recordings/3.wav", "transcripts/3.txt"]),
        (legacy_transcript_only, None, ["transcripts/4.txt"]),
    ]:
        async_session.add(
            RecordingRetentionAuditModel(
                workflow_run_id=run_id,
                object_keys=keys,
                retention_days=180,
                result="ok",
                scope=scope,
            )
        )
    await async_session.flush()

    expected = {
        available: "available",
        expired: "expired",
        legacy_expired: "expired",
        legacy_transcript_only: "none",
        failed: "notice_failed",
        disabled: "notice_disabled",
        safetynet: "none",
    }
    for run_id, status in expected.items():
        assert (await _detail(run_id))["recording_status"] == status, run_id
    listed = {r["run_id"]: r["recording_status"] for r in (await _list())["rows"]}
    assert listed == expected
    assert (await _detail(legacy_transcript_only))["transcript_status"] == "expired"


async def test_detail_fields(add_run):
    run_id = await add_run(
        _at(2021, 11, 5, 2, 3, 4),
        outcome="transferred:press0",
        seconds=83.5,
        disposition="billing_inquiry",
        caller="0912345678",
    )
    d = await _detail(run_id)
    assert d["started_at"] == "2021-11-05T02:03:04+00:00"
    assert d["ai_seconds"] == 83.5
    assert (d["outcome"], d["handed_off"], d["category"]) == (
        "transferred",
        True,
        "billing_inquiry",
    )
    assert (d["caller_masked"], d["did"]) == ("***5678", "+886212345678")
    assert d["retention"] == {"audio_days": 180, "transcript": 30}
    assert (await _detail(run_id, codes=[]))["category"] is None


def _event(kind, text_, ts, payload_ts=None, final=True):
    payload = {"text": text_}
    if payload_ts:
        payload["timestamp"] = payload_ts
    if kind == "rtf-user-transcription":
        payload["final"] = final
    return {"type": kind, "payload": payload, "timestamp": ts}


async def test_transcript_segments(add_run):
    origin = datetime(2021, 11, 5, 2, 0, 0, tzinfo=UTC)
    logs = {
        "realtime_feedback_events": [
            {"type": "rtf-node-transition", "payload": {}, "timestamp": "x"},
            # TTSSpeakFrame (consent notice): no payload timestamp
            _event("rtf-bot-text", "本通話將錄音", "2021-11-05T02:00:01.500+00:00"),
            _event(
                "rtf-user-transcription",
                "partial",
                "2021-11-05T02:00:03+00:00",
                final=False,
            ),
            _event(
                "rtf-user-transcription",
                "<img src=x onerror=alert(1)>",
                "2021-11-05T02:00:09+00:00",
                "2021-11-05T02:00:04.250+00:00",
            ),
            _event("rtf-bot-text", "   ", "2021-11-05T02:00:10+00:00"),
            "not-an-object",
        ]
    }
    run_id = await add_run(_at(2021, 11, 5), logs=logs, audio_started_at=origin)
    d = await _detail(run_id)
    assert d["transcript_status"] == "available"
    assert [
        (s["speaker"], s["text"], s["offset_ms"], s["seekable"]) for s in d["segments"]
    ] == [
        ("ai", "本通話將錄音", 1500, False),
        ("caller", "<img src=x onerror=alert(1)>", 4250, False),
    ]
    assert d["segments"][1]["at"] == "2021-11-05T02:00:04.250000+00:00"
    assert {s["source"] for s in d["segments"]} == {"ai_leg"}
    assert d["segments_truncated"] is False


async def test_segments_without_origin_are_not_seekable(add_run):
    logs = {
        "realtime_feedback_events": [
            _event("rtf-bot-text", "hi", "2021-11-05T02:00:01+00:00")
        ]
    }
    run_id = await add_run(_at(2021, 11, 5), logs=logs)
    seg = (await _detail(run_id))["segments"][0]
    assert (seg["offset_ms"], seg["seekable"]) == (None, False)


def test_queued_tts_line_logged_twice_is_one_segment():
    notice = "本通話將錄音"
    rows = [
        ("rtf-bot-text", "2021-11-05T02:00:01+00:00", None, notice),  # queued
        (
            "rtf-bot-text",
            "2021-11-05T02:00:03+00:00",
            "2021-11-05T02:00:03+00:00",
            notice,
        ),
        ("rtf-user-transcription", None, "2021-11-05T02:00:05+00:00", "嗨"),
        ("rtf-bot-text", None, "2021-11-05T02:00:06+00:00", "了解"),
        ("rtf-bot-text", None, "2021-11-05T02:00:08+00:00", "了解"),  # said twice
    ]
    segments, _ = cr.build_segments(rows, None)
    assert [(s["speaker"], s["text"]) for s in segments] == [
        ("ai", notice),
        ("caller", "嗨"),
        ("ai", "了解"),
        ("ai", "了解"),
    ]
    assert segments[0]["at"] == "2021-11-05T02:00:01+00:00"


def test_segments_are_never_seekable_while_the_recording_drifts():
    origin = datetime(2021, 11, 5, 2, 0, tzinfo=UTC)
    rows = [("rtf-bot-text", None, "2021-11-05T02:00:06+00:00", "了解")]
    (seg,), _ = cr.build_segments(rows, origin)
    assert (seg["offset_ms"], seg["seekable"]) == (6000, False)


def test_segment_limits():
    rows = [("rtf-bot-text", "2021-11-05T02:00:01+00:00", None, "x" * 5000)]
    segments, truncated = cr.build_segments(rows, None)
    assert len(segments[0]["text"]) == cr.MAX_SEGMENT_CHARS and truncated
    many = [("rtf-bot-text", None, None, "a")] * (cr.MAX_SEGMENTS + 1)
    segments, truncated = cr.build_segments(many, None)
    assert len(segments) == cr.MAX_SEGMENTS and truncated
    big = [("rtf-bot-text", None, None, "字" * 1000)] * 200  # 3000 B each
    segments, truncated = cr.build_segments(big, None)
    assert sum(len(s["text"].encode()) for s in segments) <= cr.MAX_TRANSCRIPT_BYTES
    assert truncated


async def test_extracted_variables_only_and_ordered(add_run):
    gathered = {
        "trace_url": "https://trace",
        "call_tags": ["user_speech"],
        "nodes_visited": ["start"],
        "customer_name": "王小明",
        "extracted_variables": {
            "customer_name": "王小明",
            "order": {"id": 12, "items": ["a"]},
            "vip": True,
            "count": 3,
            "deep": {"a": {"b": {"c": {"d": {"e": {"f": {"g": {"h": {"i": 1}}}}}}}}},
            "long": "x" * 600,
            "__proto__": "p",
        },
    }
    d = await _detail(await add_run(_at(2021, 11, 5), gathered=gathered))
    assert [e["key"] for e in d["extracted"]] == [
        "customer_name",
        "order",
        "vip",
        "count",
        "deep",
        "long",
        "__proto__",
    ]
    values = {e["key"]: e["value"] for e in d["extracted"]}
    assert values["order"] == '{"id": 12, "items": ["a"]}'
    assert values["vip"] is True and values["count"] == 3
    assert values["deep"] == cr.UNDISPLAYABLE
    assert len(values["long"]) == cr.MAX_EXTRACTED_CHARS
    assert d["extracted_truncated"] is True


async def test_no_extracted_variables(add_run):
    d = await _detail(await add_run(_at(2021, 11, 5), gathered={"call_tags": ["x"]}))
    assert (d["extracted"], d["extracted_truncated"]) == ([], False)
    assert d["transcript_status"] == "none" and d["segments"] == []


def test_tts_line_repeated_after_the_caller_spoke_is_one_segment():
    rows = [
        ("rtf-bot-text", "2021-11-05T02:00:01+00:00", None, "本通話將錄音"),
        ("rtf-user-transcription", None, "2021-11-05T02:00:02+00:00", "喂"),
        ("rtf-bot-text", None, "2021-11-05T02:00:03+00:00", "本通話將錄音"),
    ]
    segments, _ = cr.build_segments(rows, None)
    assert [s["text"] for s in segments] == ["本通話將錄音", "喂"]


def test_json_value_cut_marks_truncated():
    extracted, truncated = cr.build_extracted({"items": ["x" * 300, "y" * 300]})
    assert len(extracted[0]["value"]) == cr.MAX_EXTRACTED_CHARS and truncated


def test_too_many_extracted_keys():
    extracted, truncated = cr.build_extracted({f"k{i}": i for i in range(60)})
    assert len(extracted) == cr.MAX_EXTRACTED_KEYS and truncated


# --- audio (2.5) ---


@pytest.mark.parametrize(
    "header,size,expected",
    [
        (None, 100, None),
        ("bytes=0-", 100, (0, 100)),
        ("bytes=10-19", 100, (10, 10)),
        ("bytes=90-200", 100, (90, 10)),
        ("bytes=-10", 100, (90, 10)),
        ("bytes=-500", 100, (0, 100)),
    ],
)
def test_parse_range(header, size, expected):
    assert cr.parse_range(header, size) == expected


@pytest.mark.parametrize(
    "header",
    [
        "bytes=0-10,20-30",
        "bytes=100-",
        "bytes=20-10",
        "bytes=-0",
        "bytes=-",
        "items=0-1",
        "bytes=a-b",
    ],
)
def test_parse_range_rejects(header):
    with pytest.raises(cr.RangeNotSatisfiable):
        cr.parse_range(header, 100)


class _FakeObject:
    def __init__(self, data):
        self.data, self.closed = data, False

    def read(self, n):
        chunk, self.data = self.data[:n], self.data[n:]
        return chunk

    def close(self):
        self.closed = True

    def release_conn(self):
        pass


@pytest.fixture
def fake_minio(monkeypatch):
    from api.services import storage
    from api.services.filesystem.minio import MinioFileSystem

    blob = bytes(range(256)) * 400  # 102400 bytes
    fs = MinioFileSystem.__new__(MinioFileSystem)
    fs.bucket_name = "voice-audio"
    state = types.SimpleNamespace(calls=[], objects=[], blob=blob, missing=False)

    async def metadata(key):
        return None if state.missing else {"size": len(blob)}

    def get_object(bucket, key, offset, length):
        state.calls.append((key, offset, length))
        end = len(blob) if length == 0 else offset + length
        obj = _FakeObject(blob[offset:end])
        state.objects.append(obj)
        return obj

    fs.aget_file_metadata = metadata
    fs.client = types.SimpleNamespace(get_object=get_object)
    monkeypatch.setattr(storage, "get_storage_for_backend", lambda backend: fs)
    monkeypatch.setattr(cr, "_storages", {})
    return state


async def _drain(stream) -> bytes:
    return b"".join([chunk async for chunk in stream.body()])


async def test_audio_range_read(add_run, fake_minio):
    run_id = await add_run(_at(2021, 11, 5), recording_url="recordings/9.wav")
    stream = await cr.open_audio(run_id, "bytes=1000-", now=NOW)
    assert (stream.status, stream.offset, stream.length, stream.size) == (
        206,
        1000,
        101400,
        102400,
    )
    assert await _drain(stream) == fake_minio.blob[1000:]
    assert fake_minio.calls == [("recordings/9.wav", 1000, 101400)]
    assert fake_minio.objects[0].closed

    whole = await cr.open_audio(run_id, None, now=NOW)
    assert (whole.status, whole.length) == (200, 102400)
    assert await _drain(whole) == fake_minio.blob


async def test_audio_absent(add_run, fake_minio):
    none = await add_run(_at(2021, 11, 5))
    url_shaped = await add_run(
        _at(2021, 11, 5), recording_url="http://evil/recordings/1.wav"
    )
    traversal = await add_run(
        _at(2021, 11, 5), recording_url="recordings/../tickets/1.wav"
    )
    test_call = await add_run(
        _at(2021, 11, 5), mode="smallwebrtc", recording_url="recordings/1.wav"
    )
    for run_id in (none, url_shaped, traversal, test_call):
        assert await cr.open_audio(run_id, None, now=NOW) is None
    assert fake_minio.calls == []
    fake_minio.missing = True
    run_id = await add_run(_at(2021, 11, 5), recording_url="recordings/1.wav")
    assert await cr.open_audio(run_id, None, now=NOW) is None


async def test_audio_unsatisfiable_range_releases_slot(add_run, fake_minio):
    run_id = await add_run(_at(2021, 11, 5), recording_url="recordings/9.wav")
    for _ in range(cr.AUDIO_CONCURRENCY + 1):
        with pytest.raises(cr.RangeNotSatisfiable) as caught:
            await cr.open_audio(run_id, "bytes=0-1,5-6", now=NOW)
        assert caught.value.size == 102400


async def test_audio_concurrency_cap_and_release_on_abort(add_run, fake_minio):
    run_id = await add_run(_at(2021, 11, 5), recording_url="recordings/9.wav")
    streams = [
        await cr.open_audio(run_id, None, now=NOW) for _ in range(cr.AUDIO_CONCURRENCY)
    ]
    with pytest.raises(cr.AudioBusy):
        await cr.open_audio(run_id, None, now=NOW)
    # A client that disconnects mid-stream: the generator is closed early.
    first = streams[0].body()
    await first.__anext__()
    await first.aclose()
    assert fake_minio.objects[0].closed
    again = await cr.open_audio(run_id, None, now=NOW)
    # One dropped before the first byte: only the route's background release.
    streams[1].release()
    streams[1].release()  # idempotent
    assert await cr.open_audio(run_id, None, now=NOW) is not None
    for s in [again, *streams[2:]]:
        s.release()
    for _ in range(cr.AUDIO_CONCURRENCY - 1):
        (await cr.open_audio(run_id, None, now=NOW)).release()


async def test_audio_streams_without_a_db_session(add_run, fake_minio, monkeypatch):
    from api.db import db_client

    run_id = await add_run(_at(2021, 11, 5), recording_url="recordings/9.wav")
    real = db_client.async_session
    open_sessions = []

    class Tracking:
        async def __aenter__(self):
            open_sessions.append(1)
            self.ctx = real()
            return await self.ctx.__aenter__()

        async def __aexit__(self, *exc):
            open_sessions.pop()
            return await self.ctx.__aexit__(*exc)

    monkeypatch.setattr(db_client, "async_session", Tracking)
    stream = await cr.open_audio(run_id, None, now=NOW)
    assert open_sessions == []  # closed before the first byte
    await asyncio.wait_for(_drain(stream), 5)
