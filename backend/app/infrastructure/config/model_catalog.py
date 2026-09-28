from app.domain.model_catalog import ModelCapability, ModelCatalogSnapshot


class ModelCatalog:
    def __init__(self, capabilities: tuple[ModelCapability, ...]) -> None:
        self._snapshot = ModelCatalogSnapshot(capabilities=capabilities)

    def snapshot(self) -> ModelCatalogSnapshot:
        return self._snapshot
