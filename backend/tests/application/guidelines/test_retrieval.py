import asyncio
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.application.analysis_runs.workflow import CancellationToken
from app.application.guidelines.retrieval import GuidelineRunContext, build_guideline_query, fit_guideline_selection
from app.domain.guidelines import GuidelineCatalogSnapshot, GuidelineDraftV1, GuidelineEmbeddingBatch, GuidelinePolicy
from app.domain.guidelines import GuidelineMatch
from app.domain import ParameterKey
from app.domain.memory_rag.embedding_vector import EmbeddingVector
from app.infrastructure.config import load_parameter_capability_registry
from app.ports import PortAuthenticationError
from tests.application.analysis_runs._topology_scheduler_support import topology
from tests.application.guidelines.test_worker import Clock, Embedding, system, worker, publish, edit, finish_generation
from tests.guideline_support import PH, DO, content, identity, profile, version
from tests.m4_support import analysis_context
from scripts.test_database import temporary_migrated_schema

REGISTRY = load_parameter_capability_registry()
PROFILE = profile(max_input_tokens=8192)
GRAPH = topology(("100", "12345678"))


def context(question="解释上下游酸碱变化", key=PH):
    base = analysis_context(question)
    capability = REGISTRY.get(key)
    selected = base.params_used[0].model_copy(update={"key": key, "canonical_name": capability.canonical_name,
                                                   "canonical_unit": capability.canonical_unit})
    return type(base).model_validate({**base.model_dump(), "params_used": [selected.model_dump()]})


def task(run_id, token, *, question="解释上下游酸碱变化", key=PH):
    return SimpleNamespace(task=SimpleNamespace(run_id=run_id, session_id=uuid4(), target_comid=GRAPH.target_comid,
                                                cancellation_token=token), context=context(question, key), topology=GRAPH)


class Repository:
    def __init__(self, library=1, candidates=1):
        self.catalog = GuidelineCatalogSnapshot(revision=5, generation_id=uuid4(), profile=PROFILE)
        self.library, self.candidates = library, candidates
        self.reads, self.counts, self.ranks = 0, [], []
        self.matches = (GuidelineMatch(version=version(), similarity=1),)
        self.before_counts = None

    async def read_catalog(self):
        self.reads += 1
        return self.catalog

    async def retrieval_counts(self, snapshot, keys):
        self.counts.append((snapshot, keys))
        if self.before_counts: await self.before_counts()
        return self.library, self.candidates

    async def rank_guidelines(self, snapshot, keys, vector, **bounds):
        self.ranks.append((snapshot, keys, vector, bounds))
        return self.matches


async def pin(repository=None, embedding=None, policy=None, *, selected=PROFILE, run_id=None, clock=None, token=None):
    return await GuidelineRunContext.prepare(run_id or uuid4(), repository=repository or Repository(),
        embedding=embedding or Embedding(), profile=selected, registry=REGISTRY, policy=policy or GuidelinePolicy(),
        clock=clock or Clock(), cancellation=token or CancellationToken(asyncio.Event()))


@pytest.mark.parametrize("library,candidates,status", [(0, 0, "empty_library"), (1, 0, "no_match")])
def test_empty_or_no_metadata_candidates_never_dispatch_embedding(library, candidates, status):
    async def exercise():
        repository, embedding = Repository(library, candidates), Embedding()
        run = await pin(repository, embedding)
        session = run.session(task(run.pin.run_id, CancellationToken(asyncio.Event())), deadline=run._clock.now()+timedelta(seconds=60))
        try:
            result = await session.get_or_load()
            assert result.status == status and not embedding.calls and not repository.ranks
            assert repository.reads == 1
        finally: await session.aclose()
    asyncio.run(exercise())


def test_single_flight_is_bound_to_run_session_query_and_released_on_close():
    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()
        async def block(selected, inputs, token):
            entered.set()
            await release.wait()
        embedding, repository = Embedding(block), Repository()
        run = await pin(repository, embedding)
        token = CancellationToken(asyncio.Event())
        session = run.session(task(run.pin.run_id, token), deadline=run._clock.now()+timedelta(seconds=60))
        first = asyncio.create_task(session.get_or_load())
        await asyncio.wait_for(entered.wait(), 1)
        cancelled_waiter = asyncio.create_task(session.get_or_load())
        await asyncio.sleep(0)
        cancelled_waiter.cancel()
        with pytest.raises(asyncio.CancelledError): await cancelled_waiter
        second = asyncio.create_task(session.get_or_load())
        release.set()
        a, b = await asyncio.gather(first, second)
        assert a is b and a is await session.get_or_load()
        assert a.status == "retrieved" and len(embedding.calls) == len(repository.counts) == len(repository.ranks) == 1
        assert embedding.calls[0][2] <= asyncio.get_running_loop().time()+3
        assert repository.ranks[0][3] == {"cutoff": 0.35, "limit": 3}
        other = run.session(task(run.pin.run_id, token, question="Explain a different mechanism"), deadline=run._clock.now()+timedelta(seconds=60))
        try:
            changed = await other.get_or_load()
            assert changed.query_digest != a.query_digest and len(embedding.calls) == 2
            with pytest.raises(ValueError): run.session(task(uuid4(), token), deadline=run._clock.now())
        finally:
            await other.aclose()
            await session.aclose()
        assert session._task is None and session._query is None
        assert (await session.get_or_load()).safe_error_code == "guideline_session_closed"
        assert len(embedding.calls) == 2
    asyncio.run(exercise())


@pytest.mark.parametrize("action", ["cancel_before", "deadline_before", "cancel_inflight", "timeout", "short_session_timeout", "close_inflight", "deadline_after_preflight"])
def test_cancel_deadline_and_close_never_retry_or_dispatch_after_the_boundary(action):
    async def exercise():
        entered, cancelled = asyncio.Event(), asyncio.Event()
        dispatch_times = []
        async def block(selected, inputs, token):
            dispatch_times.append(asyncio.get_running_loop().time())
            entered.set()
            try: await asyncio.Event().wait()
            finally: cancelled.set()
        embedding, repository, clock = Embedding(block), Repository(), Clock()
        stop = asyncio.Event()
        token = CancellationToken(stop)
        run = await pin(repository, embedding, GuidelinePolicy(online_timeout_seconds=0.15), clock=clock)
        deadline = clock.now()+timedelta(seconds=0 if action == "deadline_before" else 60)
        if action == "short_session_timeout": deadline=clock.now()+timedelta(seconds=0.05)
        session = run.session(task(run.pin.run_id, token), deadline=deadline)
        if action == "cancel_before": stop.set()
        if action == "deadline_after_preflight":
            async def expire(): clock.advance(61)
            repository.before_counts = expire
        try:
            pending = asyncio.create_task(session.get_or_load())
            if action in {"cancel_inflight", "close_inflight"}:
                await asyncio.wait_for(entered.wait(), 1)
                if action == "cancel_inflight": stop.set()
                else:
                    await session.aclose()
                    with pytest.raises(asyncio.CancelledError): await pending
                    assert cancelled.is_set() and len(embedding.calls) == 1
                    return
            result = await pending
            assert result.status in {"cancelled", "unavailable"} and not repository.ranks
            assert (await session.get_or_load()).status == result.status
            if action in {"timeout", "short_session_timeout", "cancel_inflight"}:
                assert cancelled.is_set() and len(embedding.calls) == 1
                if action == "short_session_timeout":
                    assert 0 < embedding.calls[0][2] - dispatch_times[0] <= 0.051
            else: assert not embedding.calls
        finally: await session.aclose()
    asyncio.run(exercise())


@pytest.mark.parametrize("bad", ["profile", "credentials", "dimension", "zero_norm", "profile_response"])
def test_unavailable_profile_credentials_or_invalid_vectors_fail_safely_without_retry(bad):
    async def exercise():
        repository = Repository()
        async def response(selected, inputs, token):
            if bad == "credentials": raise PortAuthenticationError("private diagnostic")
            if bad in {"dimension", "zero_norm"}:
                return GuidelineEmbeddingBatch.model_construct(profile=selected, vectors=(EmbeddingVector(values=(0.,) if bad == "dimension" else (0.,0.,0.)),))
            if bad == "profile_response":
                return GuidelineEmbeddingBatch(profile=profile(model="other", max_input_tokens=8192), vectors=(EmbeddingVector(values=(1.,0.,0.)),))
        embedding = Embedding(response)
        run = await pin(repository, embedding, selected=profile(model="other") if bad == "profile" else PROFILE)
        session = run.session(task(run.pin.run_id, CancellationToken(asyncio.Event())), deadline=run._clock.now()+timedelta(seconds=60))
        try:
            result = await session.get_or_load()
            assert result.status == "unavailable" and "private" not in result.model_dump_json()
            assert len(embedding.calls) == (0 if bad == "profile" else 1) and not repository.ranks
            await session.get_or_load()
            assert len(embedding.calls) <= 1
        finally: await session.aclose()
    asyncio.run(exercise())


def test_query_keeps_verified_fields_and_omits_whole_oversize_or_unsafe_optional_question():
    for question in ("酸碱机制"*4000, "https://private.invalid/?api_key=private-value"):
        query = build_guideline_query(context(question), GRAPH, GRAPH.target_comid, PROFILE, REGISTRY)
        assert query.question is None and query.input_reduced and query.parameter_keys == (PH,)
        assert "private" not in query.canonical_text and query.mode.value == "prediction"
        assert "direct_upstream_count=1" in query.task_facts
        assert len((query.instructions+"\n"+query.canonical_text).encode()) <= 8192
    base = build_guideline_query(context("".join(["中"]*1000)), GRAPH, GRAPH.target_comid, PROFILE, REGISTRY)
    exact = len((base.instructions+"\n"+base.canonical_text).encode())
    for size, reduced in ((exact, False), (exact-1, True)):
        selected = profile(max_input_tokens=size)
        value = build_guideline_query(context("中"*1000), GRAPH, GRAPH.target_comid, selected, REGISTRY)
        assert value.input_reduced is reduced
        assert (value.question is None) is reduced


def test_budget_fit_removes_whole_lowest_ranked_topics_and_never_retrieves_again():
    from app.domain.guidelines import GuidelineRetrievalResult
    versions = [version(content(title=f"Full topic {index}", body_markdown="Complete conditions; mechanisms; exceptions.")) for index in range(3)]
    matches = tuple(GuidelineMatch(version=value, similarity=1-index*.1) for index,value in enumerate(versions))
    result = GuidelineRetrievalResult(status="retrieved", matches=matches)
    seen=[]
    def fits(selected):
        seen.append(selected)
        return len(selected)<=1
    reduced=fit_guideline_selection(result, fits)
    assert reduced.matches == matches[:1] and [len(item) for item in seen]==[3,2,1]
    assert reduced.matches[0].version.model_view.body == versions[0].model_view.body
    assert fit_guideline_selection(result, lambda values: False).status == "budget_limited"


@pytest.mark.parametrize("available", [True, False])
def test_workflow_pins_once_after_saved_topology_and_unavailable_pin_preserves_execution(available):
    from app.application.analysis_runs.query_topology_workflow import QueryTopologyWorkflow
    from app.application.analysis_runs.workflow import WorkflowContext
    async def exercise():
        events=[]
        class Query:
            async def understand(self, value):
                events.append("query")
                return context()
        class Topology:
            async def build(self, value):
                events.append("topology")
                return GRAPH
        class Operations:
            async def reserve_transcript_item(self, run_id): return SimpleNamespace(item_id=uuid4())
            async def finalize_transcript_item(self, selected_run, item_id, content_type, schema_version, body):
                assert selected_run == run_id and schema_version == 1
                if content_type == "guideline_run_context":
                    assert body.run_id == run_id and body.status == ("ready" if available else "unavailable")
                    assert body.profile_id == PROFILE.profile_id
                    events.append("guideline_run_context_saved")
                else:
                    assert content_type == "topology_ready"
                    events.append("topology_saved")
        class Execution:
            async def execute(self, value, *args, **kwargs):
                events.append("execution")
                assert value.guidelines.pin.status == ("ready" if available else "unavailable")
                assert value.guidelines.pin.run_id == run_id
        run_id=uuid4()
        embedding=Embedding()
        repository=Repository()
        async def pinner(selected_id, cancellation):
            events.append("pin")
            return await pin(repository,embedding,run_id=selected_id,token=cancellation,
                selected=PROFILE if available else profile(model="incompatible"))
        workflow=QueryTopologyWorkflow(Query(),Topology())
        workflow.bind_guidelines(pinner)
        workflow.bind_execution_stage(Execution())
        original=WorkflowContext(run=SimpleNamespace(run_id=run_id),query="original",cancellation=CancellationToken(asyncio.Event()),
            request=SimpleNamespace(),operations=Operations())
        await workflow.run(original)
        assert events==["query","topology","topology_saved","pin","guideline_run_context_saved","execution"]
        assert repository.reads==1 and not embedding.calls and original.guidelines is None
    asyncio.run(exercise())


@pytest.fixture
def database_settings():
    with temporary_migrated_schema() as selected: yield selected


async def add(service, selected):
    created = await service.create(GuidelineDraftV1.model_validate(selected.model_dump()), uuid4())
    return await publish(service, created.document_id)


async def sql_pin(repository, clock, embedding=None, selected=PROFILE):
    return await pin(repository, embedding, selected=selected, clock=clock)


async def select(run, *, question="解释上下游酸碱变化", key=PH):
    session=run.session(task(run.pin.run_id,CancellationToken(asyncio.Event()),question=question,key=key), deadline=run._clock.now()+timedelta(seconds=60))
    try: return await session.get_or_load()
    finally: await session.aclose()


@pytest.mark.parametrize("action", ["publish", "disable", "restore", "switch"])
def test_real_postgres_two_sessions_keep_pin_across_catalog_changes_and_new_run_sees_change(database_settings, action):
    from tests.guideline_bundle_support import bundled_version
    async def exercise():
        async with system(database_settings) as (repository, service, clock):
            base=bundled_version()
            await repository.import_bundled_version(base)
            preparation=worker(repository,Embedding(),PROFILE)
            await preparation.run_once()
            root=await service.get(base.identity.guideline_id)
            local=await service.local_edit(root.identity.guideline_id,base.version_id,base.content.content_digest,root.revision,uuid4())
            first=await publish(service,local.document_id)
            await preparation.run_once()
            old=await sql_pin(repository,clock)
            assert (await select(old)).matches[0].version.version_id==first.version_id
            selected=PROFILE
            if action=="publish":
                await edit(service,local.document_id,"New approved content")
                latest=await publish(service,local.document_id)
                await preparation.run_once()
                expected=latest.version_id
            elif action=="disable":
                root=await service.get(base.identity.guideline_id)
                await service.set_enabled(root.identity.guideline_id,False,root.revision,uuid4())
                expected=None
            elif action=="restore":
                root=await service.get(base.identity.guideline_id)
                await service.restore(root.identity.guideline_id,base.version_id,root.revision,uuid4())
                expected=base.version_id
            else:
                selected=profile(model="next-profile",output_dimensions=4,max_input_tokens=8192)
                preparation.profile=selected
                await finish_generation(preparation,repository)
                expected=first.version_id
            after=await select(old)
            assert after.matches[0].version.version_id==first.version_id
            assert old.pin.catalog.profile==PROFILE
            new=await sql_pin(repository,clock,selected=selected)
            current=await select(new)
            assert (current.matches[0].version.version_id if current.matches else None)==expected
            assert new.pin.catalog != old.pin.catalog
            assert await repository.read_embedding(old.pin.catalog.generation_id,first.version_id) is not None
    asyncio.run(exercise())


def test_real_postgres_exact_ranking_related_parameters_cutoff_top_k_stable_ties_and_profile_rejection(database_settings):
    from app.ports.errors import GuidelineConflictError
    from tests.persistence.test_guidelines import active_generation, create, ready
    async def exercise():
        async with system(database_settings) as (repository, service, clock):
            generation=await active_generation(repository,PROFILE)
            ids=[UUID(int=index,version=4) for index in (4,1,3,2)]
            for root in ids:
                selected=identity().model_copy(update={"guideline_id":root,"family_root_id":root})
                body=content(parameter_keys=(DO,),related_parameter_keys=(PH,))
                await create(repository,selected,body)
                await ready(repository,version(body,selected),1,generation)
            snapshot=await repository.read_catalog()
            assert await repository.retrieval_counts(snapshot,(PH,))==(4,4)
            ranked=await repository.rank_guidelines(snapshot,(PH,),EmbeddingVector(values=(1.,0.,0.)),cutoff=.35,limit=3)
            assert [item.version.identity.family_root_id for item in ranked]==sorted(ids)[:3]
            assert len({item.version.identity.family_root_id for item in ranked})==3
            assert await repository.rank_guidelines(snapshot,(PH,),EmbeddingVector(values=(0.,1.,0.)),cutoff=.35,limit=3)==()
            forged=snapshot.model_copy(update={"profile":profile(model="different",max_input_tokens=8192)})
            with pytest.raises(GuidelineConflictError): await repository.retrieval_counts(forged,(PH,))
            with pytest.raises(ValueError): await repository.rank_guidelines(snapshot,(PH,),EmbeddingVector(values=(1.,0.)),cutoff=.35,limit=3)
    asyncio.run(exercise())


def test_labeled_multilingual_fixture_uses_real_sql_and_counts_actual_embedding_dispatches(database_settings):
    cases=json.loads((Path(__file__).parents[2]/"fixtures/guidelines/retrieval_cases.json").read_text(encoding="utf-8"))
    async def exercise():
        async with system(database_settings) as (repository,service,clock):
            await add(service,content(title="Synthetic acid-base mechanism",related_parameter_keys=(ParameterKey(code_system="usgs",code="00410"),)))
            await worker(repository,Embedding(),PROFILE).run_once()
            for case in cases:
                async def encode(selected,inputs,token):
                    values=(0.,1.,0.) if "business hours" in inputs[0].question else (1.,0.,0.)
                    return GuidelineEmbeddingBatch(profile=selected,vectors=(EmbeddingVector(values=values),))
                embedding=Embedding(encode)
                run=await sql_pin(repository,clock,embedding)
                result=await select(run,question=case["question"],key=ParameterKey(code_system="usgs",code=case["parameter_code"]))
                assert bool(result.matches)==case["expected_match"],case["id"]
                assert len(embedding.calls)==case["expected_dispatches"],case["id"]
    asyncio.run(exercise())
