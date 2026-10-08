from uuid import UUID, uuid5


_CASE_NAMESPACE = UUID("8f5ac7a0-78d8-4b08-b702-957bc8ce1f60")


def derive_historical_case_id(
    generation_id: str | UUID,
    source_run_id: str | UUID,
    source_session_id: str | UUID,
    source_result_item_id: str | UUID,
) -> UUID:
    return uuid5(
        _CASE_NAMESPACE,
        "|".join(
            (
                str(generation_id),
                str(source_run_id),
                str(source_session_id),
                str(source_result_item_id),
                "projection-v1",
            )
        ),
    )
