"""Resolve server-owned settings exactly once before accepting a new run."""

from app.application.analysis_runs.models import AnalysisConfigurationSnapshot
from app.application.transcripts.models import AnalysisRequestV1
from app.application.settings_admin.settings_service import SettingsService
from app.application.settings_admin.errors import SettingsInvalidDefaultsError


class RuntimeSettingsResolver:
    def __init__(self, service: SettingsService) -> None:
        self._service = service

    async def resolve(self, request: AnalysisRequestV1) -> AnalysisConfigurationSnapshot:
        if request.memory_mode not in self._service.catalog.memory_modes:
            raise SettingsInvalidDefaultsError()
        snapshot = await self._service.get_snapshot()
        return request.to_configuration_snapshot(
            snapshot.defaults.analysis_timezone,
            unit_policy_version=snapshot.unit_policy.version,
            unit_registry_digest=snapshot.unit_policy.registry_digest,
        )
