from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, Response
from pydantic import UUID4

from app.api.http.analysis_runs import REQUEST_ID_HEADERS, _reject_unknown_query_parameters, error_responses
from app.api.schemas.guidelines import (
    GuidelineConflictResponse, GuidelineListResponse, LocalEditGuidelineRequest, PublishGuidelineRequest,
    RestoreBundledGuidelineRequest, RetryGuidelineOperationRequest, SaveGuidelineDraftRequest, SetGuidelineEnabledRequest,
)
from app.domain.guidelines import GuidelineDocumentDetails, GuidelineDraftV1, GuidelineMutationReceipt, GuidelineOperation, GuidelineVersionV1
from app.domain.guidelines.canonical import NormalizedText
from app.domain.guidelines.document_details import GuidelineManagementState

router = APIRouter(prefix="/api/guidelines", tags=["guidelines"],
    responses={**error_responses(400, 403, 404, 405, 422, 500, 503),
               409: {"model": GuidelineConflictResponse, "headers": REQUEST_ID_HEADERS}})
Key = Annotated[UUID4, Header(alias="Idempotency-Key")]


def _service(request):
    _reject_unknown_query_parameters(request, set())
    return request.app.state.guideline_service


@router.get("", response_model=GuidelineListResponse, responses={200: {"headers": REQUEST_ID_HEADERS}})
async def list_guidelines(
    request: Request, source: Literal["all", "bundled", "mine"] = "all",
    state: GuidelineManagementState | None = None,
    q: Annotated[NormalizedText, Query(max_length=160)] = "",
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    cursor: Annotated[str | None, Query(min_length=1, max_length=512)] = None,
) -> GuidelineListResponse:
    _reject_unknown_query_parameters(request, {"source", "state", "q", "limit", "cursor"})
    items, next_cursor = await request.app.state.guideline_service.list(source=source, state=state, q=q, limit=limit, cursor=cursor)
    return GuidelineListResponse(items=items, next_cursor=next_cursor)


@router.post("", response_model=GuidelineMutationReceipt, status_code=201, responses={201: {"headers": REQUEST_ID_HEADERS}})
async def create_guideline(request: Request, body: GuidelineDraftV1, idempotency_key: Key) -> GuidelineMutationReceipt:
    return await _service(request).create(body, idempotency_key)


# Static operations paths precede the document UUID paths.
@router.get("/operations/{operation_id}", response_model=GuidelineOperation, responses={200: {"headers": REQUEST_ID_HEADERS}})
async def get_guideline_operation(request: Request, operation_id: UUID) -> GuidelineOperation:
    return await _service(request).get_operation(operation_id)


@router.post("/operations/{operation_id}/retry", response_model=GuidelineMutationReceipt, status_code=202,
             responses={202: {"headers": REQUEST_ID_HEADERS}})
async def retry_guideline_operation(request: Request, operation_id: UUID, body: RetryGuidelineOperationRequest,
                                    idempotency_key: Key) -> GuidelineMutationReceipt:
    return await _service(request).retry(operation_id, body.expected_revision, idempotency_key)


@router.get("/{document_id}", response_model=GuidelineDocumentDetails, responses={200: {"headers": REQUEST_ID_HEADERS}})
async def get_guideline(request: Request, document_id: UUID) -> GuidelineDocumentDetails:
    return await _service(request).get(document_id)


@router.put("/{document_id}/draft", response_model=GuidelineMutationReceipt, responses={200: {"headers": REQUEST_ID_HEADERS}})
async def save_guideline_draft(request: Request, document_id: UUID, body: SaveGuidelineDraftRequest,
                                idempotency_key: Key) -> GuidelineMutationReceipt:
    return await _service(request).save(document_id, body.draft, body.expected_revision, idempotency_key)


@router.post("/{document_id}/local-edit", response_model=GuidelineMutationReceipt, status_code=201,
             responses={201: {"headers": REQUEST_ID_HEADERS}, 200: {"model": GuidelineMutationReceipt, "headers": REQUEST_ID_HEADERS}})
async def local_edit_guideline(request: Request, response: Response, document_id: UUID, body: LocalEditGuidelineRequest,
                               idempotency_key: Key) -> GuidelineMutationReceipt:
    receipt = await _service(request).local_edit(document_id, body.base_version_id, body.base_content_digest,
                                                body.expected_revision, idempotency_key)
    response.status_code = receipt.status_code
    return receipt


@router.post("/{document_id}/publish", response_model=GuidelineMutationReceipt, status_code=202,
             responses={202: {"headers": REQUEST_ID_HEADERS}})
async def publish_guideline(request: Request, document_id: UUID, body: PublishGuidelineRequest,
                            idempotency_key: Key) -> GuidelineMutationReceipt:
    return await _service(request).publish(document_id, **body.model_dump(), key=idempotency_key)


@router.post("/{document_id}/set-enabled", response_model=GuidelineMutationReceipt, responses={200: {"headers": REQUEST_ID_HEADERS}})
async def set_guideline_enabled(request: Request, document_id: UUID, body: SetGuidelineEnabledRequest,
                                idempotency_key: Key) -> GuidelineMutationReceipt:
    return await _service(request).set_enabled(document_id, body.enabled, body.expected_revision, idempotency_key)


@router.post("/{document_id}/restore-bundled", response_model=GuidelineMutationReceipt, responses={200: {"headers": REQUEST_ID_HEADERS}})
async def restore_bundled_guideline(request: Request, document_id: UUID, body: RestoreBundledGuidelineRequest,
                                   idempotency_key: Key) -> GuidelineMutationReceipt:
    return await _service(request).restore(document_id, body.version_id, body.expected_revision, idempotency_key)


@router.get("/{document_id}/versions/{version_id}", response_model=GuidelineVersionV1, responses={200: {"headers": REQUEST_ID_HEADERS}})
async def get_guideline_version(request: Request, document_id: UUID, version_id: UUID) -> GuidelineVersionV1:
    return await _service(request).get_version(document_id, version_id)
