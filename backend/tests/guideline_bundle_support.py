import json
from hashlib import sha256
from pathlib import Path

from app.domain.guidelines import GuidelineIdentity
from app.domain.guidelines.identity import bundled_guideline_id, bundled_guideline_version_id
from tests.guideline_support import content, version


class EmptyBundledSource:
    def read_versions(self):
        return ()


def bundled_version(number=1, *, key="fixture.mechanism", selected_content=None):
    root = bundled_guideline_id(key)
    selected_identity = GuidelineIdentity(guideline_id=root, family_root_id=root, origin="bundled", bundle_key=key)
    return version(selected_content or content(), selected_identity).model_copy(update={
        "version_id": bundled_guideline_version_id(key, number), "version_number": number,
    })


def write_bundle(root: Path, versions):
    root.mkdir(parents=True, exist_ok=True)
    entries = []
    for selected in versions:
        resource = f"topics/{selected.identity.bundle_key}/v{selected.version_number}.md"
        body = root / resource
        body.parent.mkdir(parents=True, exist_ok=True)
        body.write_bytes(selected.content.body_markdown.encode("utf-8"))
        entries.append({
            "bundle_key": selected.identity.bundle_key, "version": selected.version_number,
            "body_resource": resource,
            "body_digest": "sha256:" + sha256(selected.content.body_markdown.encode("utf-8")).hexdigest(),
            "content_digest": selected.content.content_digest, "model_view_digest": selected.model_view.view_digest,
            "metadata": selected.content.model_dump(mode="json", exclude={"body_markdown"}),
            "review": selected.review.model_dump(mode="json"),
        })
    manifest = {"schema_version": 1, "documents": entries}
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest
