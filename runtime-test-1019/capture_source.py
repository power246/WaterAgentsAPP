"""Capture the compatible working-tree source delta at archive time."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent
ROOT = PACKAGE.parent
OUTPUT = PACKAGE / "source-compatibility"


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", *args], cwd=ROOT)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> None:
    OUTPUT.mkdir(exist_ok=True)
    branch = git("branch", "--show-current").decode().strip()
    bundle = OUTPUT / "base.bundle"
    subprocess.run(["git", "bundle", "create", str(bundle), branch], cwd=ROOT, check=True)
    patch = git("diff", "--binary", "--no-ext-diff", "HEAD", "--")
    (OUTPUT / "tracked-changes.patch").write_bytes(patch)

    paths = git(
        "ls-files", "--others", "--exclude-standard", "-z", "--", "backend/app", "frontend/src"
    ).decode("utf-8").split("\0")
    extra_hashes: dict[str, str] = {}
    for relative in filter(None, paths):
        source = ROOT / relative
        destination = OUTPUT / "new-files" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        extra_hashes[relative] = sha256(destination.read_bytes())

    manifest = {
        "format": "wateragents-source-compatibility-v1",
        "base_commit": git("rev-parse", "HEAD").decode().strip(),
        "branch": branch,
        "base_bundle_sha256": sha256(bundle.read_bytes()),
        "tracked_patch_sha256": sha256(patch),
        "new_file_sha256": extra_hashes,
    }
    (OUTPUT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Captured source delta: {len(patch)} patch bytes, {len(extra_hashes)} new files")


if __name__ == "__main__":
    main()
