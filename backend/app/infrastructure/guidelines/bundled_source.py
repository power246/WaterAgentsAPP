import json
from hashlib import sha256
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.guidelines import GuidelineContentV1, GuidelineDraftV1, GuidelineIdentity, GuidelineReview, GuidelineVersionV1, project_model_view
from app.domain.guidelines.canonical import Digest, VersionNumber, normalize_text
from app.domain.guidelines.identity import BundleKey, bundled_guideline_id, bundled_guideline_version_id
from app.domain.hydrology.parameter_capability_registry import ParameterCapabilityRegistry
from app.infrastructure.config import load_parameter_capability_registry
from app.ports.errors import GuidelineBundleError


class _BundleEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    bundle_key: BundleKey
    version: VersionNumber
    body_resource: str
    body_digest: Digest
    content_digest: Digest
    model_view_digest: Digest
    metadata: GuidelineDraftV1
    review: GuidelineReview


class _BundleManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    documents: tuple[_BundleEntry, ...] = Field(...)


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate manifest field")
        result[key] = value
    return result


class PackageBundledGuidelineSource:
    def __init__(self, *, root: Traversable | None = None,
                 registry: ParameterCapabilityRegistry | None = None) -> None:
        self._root = root if root is not None else files("app.infrastructure.guidelines.bundled")
        self._registry = registry or load_parameter_capability_registry()

    def read_versions(self) -> tuple[GuidelineVersionV1, ...]:
        try:
            manifest = _BundleManifest.model_validate(json.loads(
                self._root.joinpath("manifest.json").read_text(encoding="utf-8"),
                object_pairs_hook=_unique_keys,
            ))
            identities = [(entry.bundle_key, entry.version) for entry in manifest.documents]
            if len(identities) != len(set(identities)):
                raise ValueError("duplicate bundle key/version")
            return tuple(self._read_version(entry) for entry in sorted(
                manifest.documents, key=lambda entry: (entry.bundle_key, entry.version)))
        except GuidelineBundleError:
            raise
        except (OSError, ValueError, TypeError) as error:
            raise GuidelineBundleError("guideline_bundle_invalid") from error

    def _read_version(self, entry: _BundleEntry) -> GuidelineVersionV1:
        path = PurePosixPath(entry.body_resource)
        if (not entry.body_resource or "\\" in entry.body_resource or ":" in entry.body_resource
                or path.is_absolute() or any(part in {".", ".."} for part in entry.body_resource.split("/"))
                or any(not part for part in entry.body_resource.split("/")) or path.suffix != ".md"):
            raise ValueError("invalid bundled body resource path")
        body = normalize_text(self._root.joinpath(*path.parts).read_text(encoding="utf-8"))
        body_digest = "sha256:" + sha256(body.encode("utf-8")).hexdigest()
        if body_digest != entry.body_digest or entry.metadata.body_markdown is not None:
            raise ValueError("bundled body digest or authority mismatch")
        content = GuidelineContentV1.model_validate({**entry.metadata.model_dump(), "body_markdown": body})
        content.require_registered_parameters(self._registry)
        view = project_model_view(content)
        if content.content_digest != entry.content_digest or view.view_digest != entry.model_view_digest:
            raise ValueError("bundled canonical digest mismatch")
        root = bundled_guideline_id(entry.bundle_key)
        return GuidelineVersionV1(
            version_id=bundled_guideline_version_id(entry.bundle_key, entry.version),
            version_number=entry.version,
            identity=GuidelineIdentity(guideline_id=root, family_root_id=root, origin="bundled", bundle_key=entry.bundle_key),
            content=content, model_view=view, review=entry.review,
        )
