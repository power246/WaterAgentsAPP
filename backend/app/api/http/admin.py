"""Local Admin preview, confirmation, and read-only status routes."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, Response

from app.api.http.analysis_runs import (
    REQUEST_ID_HEADERS, _reject_unknown_query_parameters, error_responses,
)
from app.api.schemas.settings_admin import (
    AdminOperationListResponse, AdminOperationSnapshot, AdminRAGStatusResponse,
    AdminStorageStatusResponse, ConfirmActionRequest, GenerationPreviewResponse,
    HistoryCleanupPreviewRequest, HistoryCleanupPreviewResponse,
    HistoryCleanupEstimate, ProfileHandoffPreviewResponse,
    RetryFailedJobsPreviewRequest, StorageItemStatus,
)
from app.application.analysis_runs.creation import InvalidIdempotencyKeyError
from app.application.settings_admin.errors import (
    AdminOperationCursorError, SettingsUnavailableError,
)
from app.domain.settings_admin import AdminActionKind


router = APIRouter(
    prefix="/api/admin", tags=["admin"],
    responses=error_responses(403, 405, 422, 500, 503),
)


def _prepare(request: Request, response: Response, allowed: set[str] | None = None) -> None:
    _reject_unknown_query_parameters(request, allowed or set())
    response.headers["Cache-Control"] = "no-store"


def _key(value: UUID) -> UUID:
    if value.version != 4:
        raise InvalidIdempotencyKeyError()
    return value


def _service(request: Request):
    service = getattr(request.app.state, "admin_service", None)
    if service is None:
        raise SettingsUnavailableError()
    return service


@router.get("/storage", response_model=AdminStorageStatusResponse,
            responses={200: {"headers": REQUEST_ID_HEADERS}})
async def get_storage(request: Request, response: Response) -> AdminStorageStatusResponse:
    _prepare(request, response)
    states = await _service(request).storage_status()
    return AdminStorageStatusResponse(**{
        name: StorageItemStatus(state=state) for name, state in states.items()
    })


@router.get("/rag", response_model=AdminRAGStatusResponse,
            responses={200: {"headers": REQUEST_ID_HEADERS}})
async def get_rag(request: Request, response: Response) -> AdminRAGStatusResponse:
    _prepare(request, response)
    index, handoff = await _service(request).rag_status()
    return AdminRAGStatusResponse(index=index, handoff=handoff)


@router.get("/operations", response_model=AdminOperationListResponse,
            responses={200: {"headers": REQUEST_ID_HEADERS}})
async def list_operations(
    request: Request, response: Response,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: str | None = None,
) -> AdminOperationListResponse:
    _prepare(request, response, {"limit", "cursor"})
    try:
        items, next_cursor = await _service(request).list_operations(
            limit=limit, cursor=cursor,
        )
    except ValueError as error:
        raise AdminOperationCursorError() from error
    return AdminOperationListResponse(items=tuple(
        AdminOperationSnapshot.model_validate(item.model_dump()) for item in items
    ), next_cursor=next_cursor)


@router.get("/operations/{operation_id}", response_model=AdminOperationSnapshot,
            responses={**error_responses(404), 200: {"headers": REQUEST_ID_HEADERS}})
async def get_operation(
    request: Request, response: Response, operation_id: UUID,
) -> AdminOperationSnapshot:
    _prepare(request, response)
    item = await _service(request).get_operation(operation_id)
    return AdminOperationSnapshot.model_validate(item.model_dump())


@router.post("/history-cleanups/preview", response_model=HistoryCleanupPreviewResponse,
             responses={**error_responses(409), 200: {"headers": REQUEST_ID_HEADERS}})
async def preview_history(
    request: Request, response: Response, body: HistoryCleanupPreviewRequest,
) -> HistoryCleanupPreviewResponse:
    _prepare(request, response)
    preview = await _service(request).preview_history(
        run_ids=body.selection.run_ids if body.selection.kind == "runs" else None,
        cascade_consumers=body.cascade_consumers,
    )
    plan, storage = preview.plan, preview.storage
    return HistoryCleanupPreviewResponse(
        requested_run_ids=tuple(UUID(value) for value in plan.requested_run_ids) if plan else (),
        delete_run_ids=tuple(UUID(value) for value in plan.delete_run_ids) if plan else (),
        delete_order=tuple(UUID(value) for value in plan.delete_order) if plan else (),
        dependencies=tuple(UUID(item.link_id) for item in plan.dependencies) if plan else (),
        cascade_required=plan.cascade_required if plan else False,
        cascade_authorized=preview.cascade_authorized,
        estimated=HistoryCleanupEstimate(
            runs=len(plan.delete_run_ids) if plan else 0,
            sessions=storage.sessions if storage else 0,
            transcript_items=storage.transcript_items if storage else 0,
            retained_idempotency_tombstones=storage.idempotency_keys if storage else 0,
        ),
        preserved=("api_keys", "settings", "unit_policy_versions"),
        confirmation=preview.confirmation,
    )


async def _execute(
    request: Request, response: Response, body: ConfirmActionRequest,
    idempotency_key: UUID, kind: AdminActionKind,
    generation_id: str | None = None,
) -> AdminOperationSnapshot:
    _prepare(request, response)
    operation = await _service(request).execute(
        kind=kind, token=body.confirmation_token,
        phrase=body.confirmation_phrase, idempotency_key=_key(idempotency_key),
        raw_body=await request.body(), generation_id=generation_id,
    )
    return AdminOperationSnapshot.model_validate(operation.model_dump())


_EXECUTE_RESPONSES = {**error_responses(404, 409),
                      202: {"headers": REQUEST_ID_HEADERS}}


@router.post("/history-cleanups", status_code=202,
             response_model=AdminOperationSnapshot, responses=_EXECUTE_RESPONSES)
async def execute_history(
    request: Request, response: Response, body: ConfirmActionRequest,
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
) -> AdminOperationSnapshot:
    return await _execute(request, response, body, idempotency_key,
                          AdminActionKind.HISTORY_CLEANUP)


@router.post("/rag/profile-handoffs/preview", response_model=ProfileHandoffPreviewResponse,
             responses={**error_responses(409), 200: {"headers": REQUEST_ID_HEADERS}})
async def preview_profile(request: Request, response: Response) -> ProfileHandoffPreviewResponse:
    _prepare(request, response)
    preview = await _service(request).preview_profile_handoff()
    return ProfileHandoffPreviewResponse(
        handoff=preview.handoff, estimated_jobs=preview.estimated_jobs,
        provider_usage_warning=True, confirmation=preview.confirmation,
    )


@router.post("/rag/profile-handoffs", status_code=202,
             response_model=AdminOperationSnapshot, responses=_EXECUTE_RESPONSES)
async def execute_profile(
    request: Request, response: Response, body: ConfirmActionRequest,
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
) -> AdminOperationSnapshot:
    return await _execute(request, response, body, idempotency_key,
                          AdminActionKind.PROFILE_HANDOFF)


@router.post("/rag/generations/{generation_id}/failed-jobs/retry-preview",
             response_model=GenerationPreviewResponse,
             responses={**error_responses(404, 409), 200: {"headers": REQUEST_ID_HEADERS}})
async def preview_retry(
    request: Request, response: Response, generation_id: UUID,
    body: RetryFailedJobsPreviewRequest,
) -> GenerationPreviewResponse:
    _prepare(request, response)
    preview = await _service(request).preview_failed_job_retry(
        str(generation_id), safe_error_codes=body.safe_error_codes, limit=body.limit,
    )
    return GenerationPreviewResponse(
        action=preview.action, generation_id=preview.generation_id,
        affected_jobs=preview.affected_jobs, confirmation=preview.confirmation,
    )


@router.post("/rag/generations/{generation_id}/failed-jobs/retries",
             status_code=202, response_model=AdminOperationSnapshot,
             responses=_EXECUTE_RESPONSES)
async def execute_retry(
    request: Request, response: Response, generation_id: UUID,
    body: ConfirmActionRequest,
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
) -> AdminOperationSnapshot:
    return await _execute(request, response, body, idempotency_key,
                          AdminActionKind.RETRY_FAILED_JOBS, str(generation_id))


@router.post("/rag/generations/{generation_id}/deletion-preview",
             response_model=GenerationPreviewResponse,
             responses={**error_responses(404, 409), 200: {"headers": REQUEST_ID_HEADERS}})
async def preview_deletion(
    request: Request, response: Response, generation_id: UUID,
) -> GenerationPreviewResponse:
    _prepare(request, response)
    preview = await _service(request).preview_generation_deletion(
        str(generation_id)
    )
    return GenerationPreviewResponse(
        action=preview.action, generation_id=preview.generation_id,
        affected_jobs=preview.affected_jobs, confirmation=preview.confirmation,
    )


@router.post("/rag/generations/{generation_id}/deletions",
             status_code=202, response_model=AdminOperationSnapshot,
             responses=_EXECUTE_RESPONSES)
async def execute_deletion(
    request: Request, response: Response, generation_id: UUID,
    body: ConfirmActionRequest,
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
) -> AdminOperationSnapshot:
    return await _execute(request, response, body, idempotency_key,
                          AdminActionKind.GENERATION_DELETION, str(generation_id))
