"""Apply the archived compatible source delta to its exact Git base commit."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent
SOURCE = PACKAGE / "source-compatibility"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=PACKAGE.parent)
    parser.add_argument("--apply", action="store_true", help="Apply after all checks pass")
    args = parser.parse_args()
    root = args.project_root.resolve()
    if not (root / "backend" / "app").is_dir() or not (root / ".git").exists():
        raise SystemExit("Not a WaterAgentsAPP Git checkout")

    manifest = json.loads((SOURCE / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("format") != "wateragents-source-compatibility-v1":
        raise SystemExit("Unknown source compatibility format")
    if sha256(SOURCE / "base.bundle") != manifest["base_bundle_sha256"]:
        raise SystemExit("Base Git bundle checksum mismatch")
    if git(root, "rev-parse", "HEAD") != manifest["base_commit"]:
        raise SystemExit("Git HEAD differs from the archived base commit")
    if git(root, "status", "--porcelain", "--untracked-files=no"):
        raise SystemExit("Tracked working tree has local changes; use a clean checkout")

    patch = SOURCE / "tracked-changes.patch"
    if sha256(patch) != manifest["tracked_patch_sha256"]:
        raise SystemExit("Tracked patch checksum mismatch")
    for relative, expected in manifest["new_file_sha256"].items():
        source = SOURCE / "new-files" / relative
        destination = root / relative
        if not source.is_file() or sha256(source) != expected:
            raise SystemExit(f"New source file checksum mismatch: {relative}")
        if destination.exists():
            raise SystemExit(f"Destination already exists: {relative}")

    subprocess.run(["git", "apply", "--check", str(patch)], cwd=root, check=True)
    if not args.apply:
        print("Source archive verified; pass --apply to restore it")
        return

    subprocess.run(["git", "apply", str(patch)], cwd=root, check=True)
    for relative in manifest["new_file_sha256"]:
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SOURCE / "new-files" / relative, destination)
    print(f"Restored compatible source at {root}")


if __name__ == "__main__":
    main()
