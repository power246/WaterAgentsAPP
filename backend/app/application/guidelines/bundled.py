from dataclasses import dataclass

from app.domain.guidelines import GuidelineMutationReceipt
from app.ports.errors import GuidelineBundleError, GuidelineConflictError, PortError
from app.ports.guidelines import BundledGuidelineSourcePort, GuidelineRepositoryPort


@dataclass(frozen=True)
class BundledImportReport:
    receipts: tuple[GuidelineMutationReceipt, ...] = ()
    error_codes: tuple[str, ...] = ()


class BundledGuidelineImporter:
    def __init__(self, source: BundledGuidelineSourcePort, repository: GuidelineRepositoryPort) -> None:
        self._source = source
        self._repository = repository

    async def import_all(self) -> BundledImportReport:
        try:
            versions = self._source.read_versions()
        except GuidelineBundleError as error:
            return BundledImportReport(error_codes=(error.code,))
        except (PortError, ValueError):
            return BundledImportReport(error_codes=("guideline_bundle_unavailable",))
        receipts, errors = [], []
        for version in versions:
            try:
                receipts.append(await self._repository.import_bundled_version(version))
            except GuidelineConflictError as error:
                errors.append(error.code)
            except (PortError, ValueError):
                errors.append("guideline_import_unavailable")
        return BundledImportReport(receipts=tuple(receipts), error_codes=tuple(errors))
