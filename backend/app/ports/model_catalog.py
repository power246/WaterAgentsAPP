from typing import Protocol

from app.domain.model_catalog import ModelCatalogSnapshot


class ModelCatalogPort(Protocol):
    def snapshot(self) -> ModelCatalogSnapshot: ...
