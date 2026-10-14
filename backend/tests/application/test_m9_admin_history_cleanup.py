"""History cleanup reconciles a lost delete acknowledgement before releasing live state."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.application.settings_admin.admin_service import AdminService
from app.application.transcript_memory.maintenance import HistoryStorageFacts
from app.domain.settings_admin import AdminCommand


class _Clock:
    def now(self):
        return datetime.now(UTC)


@pytest.mark.parametrize("remaining_after_error,inspection_fails,invalidated", [
    (True, False, False),
    (False, False, True),
    (False, True, True),
])
def test_lost_delete_acknowledgement_reconciles_before_live_invalidation(
    remaining_after_error: bool, inspection_fails: bool, invalidated: bool,
) -> None:
    async def exercise():
        run_id = uuid4()
        facts = HistoryStorageFacts(
            run_ids=(str(run_id),), sessions=1, transcript_items=1,
            idempotency_keys=1, all_terminal=True,
        )

        class Maintenance:
            def __init__(self):
                self.inspections = 0

            async def inspect_history_storage(self, _run_ids):
                self.inspections += 1
                if self.inspections == 1:
                    return facts
                if inspection_fails:
                    raise OSError("verification unavailable")
                return facts if remaining_after_error else HistoryStorageFacts(
                    run_ids=(), sessions=0, transcript_items=0,
                    idempotency_keys=1, all_terminal=True,
                )

            async def delete_history_runs(self, *_args, **_kwargs):
                raise OSError("delete acknowledgement lost")

        live_invalidated = False

        @asynccontextmanager
        async def guard(_run_ids):
            nonlocal live_invalidated
            yield True
            live_invalidated = True

        service = AdminService(
            settings=object(), maintenance=Maintenance(), store=object(), clock=_Clock(),
            profile_for_id=lambda _id: None, apply_profile=lambda _profile: None,
            notify_maintenance=lambda: None, history_cleanup_guard=guard,
        )
        command = AdminCommand(
            kind="history_cleanup", resource_identity=f"history:{run_id}",
            facts_digest="a" * 64, requested_run_ids=(run_id,),
            expected_delete_run_ids=(run_id,),
        )

        async def same_facts(_command):
            return command.facts_digest

        service._current_digest = same_facts
        if remaining_after_error or inspection_fails:
            with pytest.raises(OSError, match="delete acknowledgement lost"):
                await service.handle(command)
        else:
            result = await service.handle(command)
            assert result.deleted_run_ids == (run_id,)
            assert result.retained_idempotency_tombstones == 1
        assert service._maintenance.inspections == 2
        assert live_invalidated is invalidated

    asyncio.run(exercise())
