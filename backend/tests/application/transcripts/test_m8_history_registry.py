import asyncio
import json
from datetime import UTC, datetime
from uuid import UUID

from app.application.analysis_runs.models import (
    AgentRole,
    AgentSessionStatus,
    RunStatus,
    SessionSummary,
)
from app.application.transcripts.models import (
    ParseStatus,
    StoredTranscriptItem,
)
from app.application.transcripts.read_models import (
    ReadView,
    StoredRunHeader,
    StoredRunHistoryPage,
    StoredRunRead,
)
from app.application.transcripts.reader import TranscriptReader
from app.application.transcripts.registry import create_production_registry
from app.domain import (
    EvidenceBundle,
    EvidenceFragmentReference,
    EvidenceSourceRecords,
    EvidenceToolCall,
    EvidenceToolCounts,
    EvidenceToolExecutionStatus,
    EvidenceToolOptions,
    EvidenceToolResult,
    HistoricalSearchStrategy,
    ItemReference,
)
from tests.domain.test_evidence import (
    NOW,
    TARGET,
    sample_fragments,
    valid_bundle,
    valid_manifest,
    valid_source_records,
)

RUN_ID = UUID("92000000-0000-4000-8000-000000000001")
SESSION_ID = UUID("92000000-0000-4000-8000-000000000002")
ITEM_IDS = tuple(
    UUID(f"92000000-0000-4000-8000-{index:012d}")
    for index in range(10, 15)
)


def _samples():
    manifest = valid_manifest()
    source = EvidenceSourceRecords(
        operation_id=manifest.operation_id,
        batch_id=manifest.manifest_id,
        manifest=manifest,
        records=(valid_source_records()[-1],),
    )
    call = EvidenceToolCall(
        tool_call_id="historical-call",
        session_id=str(SESSION_ID),
        tool_name="search_transcript_evidence",
        purpose="historical_context",
        window_ids=("w000",),
        parameter_keys=(TARGET,),
        options=EvidenceToolOptions(
            record_limit=1,
            historical_search_strategy=(
                HistoricalSearchStrategy.SEMANTIC_CASE_QUERY
            ),
        ),
        policy="optional",
        requested_at=NOW,
    )
    fragment = sample_fragments()[-1]
    result = EvidenceToolResult(
        tool_call_id=call.tool_call_id,
        operation_id=manifest.operation_id,
        tool_name=call.tool_name,
        execution_status=EvidenceToolExecutionStatus.COMPLETED,
        collection_status=None,
        counts=EvidenceToolCounts(),
        completed_at=NOW,
    )
    base_bundle = valid_bundle()
    bundle = EvidenceBundle.model_validate(
        {
            **base_bundle.model_dump(mode="python"),
            "context_ref": ItemReference(item_id=str(ITEM_IDS[0])),
            "topology_ref": ItemReference(item_id=str(ITEM_IDS[1])),
            "fragment_refs": (
                EvidenceFragmentReference(
                    item_id=str(ITEM_IDS[2]),
                    evidence_id="ev-direct",
                ),
                EvidenceFragmentReference(
                    item_id=str(ITEM_IDS[3]),
                    evidence_id="ev-aggregate",
                ),
            ),
            "source_record_item_refs": (str(ITEM_IDS[4]),),
            "tool_result_refs": (),
        }
    )
    return (
        ("evidence_tool_call", call),
        ("evidence_source_records", source),
        ("evidence_fragment", fragment),
        ("evidence_tool_result", result),
        ("evidence_bundle", bundle),
    )


def _stored_items(registry):
    items = []
    for sequence, ((content_type, body), item_id) in enumerate(
        zip(_samples(), ITEM_IDS, strict=True),
        1,
    ):
        encoded = registry.encode(content_type, 1, body)
        items.append(
            StoredTranscriptItem(
                item_id=item_id,
                run_id=RUN_ID,
                session_id=SESSION_ID,
                transcript_sequence=sequence,
                created_at=NOW,
                content_type=content_type,
                content_schema_version=1,
                inline_body=encoded.safe_body,
                content_bytes=encoded.content_bytes,
            )
        )
    return tuple(items)


def test_m8_typed_facts_round_trip_through_registry_and_history_readers() -> None:
    async def exercise() -> None:
        registry = create_production_registry()
        items = _stored_items(registry)
        session = SessionSummary(
            session_id=SESSION_ID,
            agent_role=AgentRole.WATERBODY,
            target_comid="12345678",
            status=AgentSessionStatus.RUNNING,
            created_at=NOW,
            updated_at=NOW,
        )
        stored = StoredRunRead(
            run=StoredRunHeader(
                run_id=RUN_ID,
                status=RunStatus.COMPLETED,
                created_at=NOW,
                updated_at=NOW,
                query="historical read contract",
                request_item_id=ITEM_IDS[0],
                created_item_id=ITEM_IDS[0],
            ),
            sessions=(session,),
            items=items,
        )

        class Store:
            async def read_run(self, run_id):
                return stored if run_id == RUN_ID else None

            async def read_run_detail_roots(self, run_id):
                return await self.read_run(run_id)

            async def read_run_items(self, run_id, item_ids):
                selected = await self.read_run(run_id)
                if selected is None:
                    return None
                return selected.model_copy(update={
                    "items": tuple(
                        item for item in selected.items if item.item_id in item_ids
                    ),
                })

            async def list_session_item_ids(
                self, run_id, session_id, content_type
            ):
                selected = await self.read_run(run_id)
                if selected is None:
                    return ()
                return tuple(
                    item.item_id for item in selected.items
                    if item.session_id == session_id
                    and (content_type is None or item.content_type == content_type)
                )

            async def list_runs(
                self,
                limit,
                after_created_at,
                after_run_id,
            ):
                del limit, after_created_at, after_run_id
                return StoredRunHistoryPage(runs=(stored,))

        class Live:
            async def get_live_read(self, run_id):
                del run_id
                return None

        reader = TranscriptReader(
            Live(),
            Store(),
            registry,
            error_id_factory=lambda: RUN_ID,
            inline_content_bytes=1_000_000,
            preview_bytes=4_096,
        )
        session_detail = await reader.get_session(
            RUN_ID,
            SESSION_ID,
            ReadView.HISTORY,
        )
        assert session_detail.session == session
        transcript = await reader.get_session_transcript(
            RUN_ID,
            SESSION_ID,
            ReadView.HISTORY,
        )
        assert tuple(item.content_type for item in transcript.items) == tuple(
            content_type for content_type, _ in _samples()
        )
        assert all(item.parse_status is ParseStatus.OK for item in transcript.items)

        source_item = transcript.items[1]
        projected = source_item.content.body
        historical = projected["records"][0]
        assert historical["rag_score"] == 0.9
        assert historical["matched_by"]["strategy"] == "semantic_case_query"
        assert historical["case_ref"]["result_item_id"] == "item-old"
        assert historical["result_projection"]["cells"][0]["confidence"] == 0.6

        full = await reader.get_content(
            RUN_ID,
            source_item.item_id,
            ReadView.HISTORY,
        )
        full_body = json.loads(full.text)
        assert full_body["records"][0]["rag_score"] == 0.9
        forbidden = {
            "canonical_query",
            "canonical_text",
            "embedding",
            "vector",
            "job_id",
            "provider_raw_response",
            "raw_response",
        }

        def keys(value):
            if isinstance(value, dict):
                return set(value).union(
                    *(keys(item) for item in value.values())
                )
            if isinstance(value, list):
                return set().union(*(keys(item) for item in value))
            return set()

        assert keys(projected).isdisjoint(forbidden)
        assert keys(full_body).isdisjoint(forbidden)
        history = await reader.list_runs(limit=1, cursor=None)
        assert history.items[0].run_id == RUN_ID

    asyncio.run(exercise())


def test_unknown_m8_schema_version_remains_fail_closed() -> None:
    registry = create_production_registry()
    encoded = registry.encode("evidence_tool_call", 1, _samples()[0][1])
    unsupported = StoredTranscriptItem(
        item_id=ITEM_IDS[0],
        run_id=RUN_ID,
        session_id=SESSION_ID,
        transcript_sequence=1,
        created_at=datetime(2026, 10, 8, tzinfo=UTC),
        content_type="evidence_tool_call",
        content_schema_version=2,
        inline_body=encoded.safe_body,
        content_bytes=encoded.content_bytes,
    )

    parsed = registry.parse(unsupported, encoded.safe_body)

    assert parsed.parse_status is ParseStatus.UNSUPPORTED
    assert parsed.body is None
    assert parsed.parse_error.code == "transcript_content_unsupported"
