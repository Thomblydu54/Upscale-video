"""Construire les paquets officiels téléchargeables et leur catalogue de release."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from upscaler import __version__  # noqa: E402


def package(source: Path):
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as z:
        paths = [source / "manifest.json"] + sorted((source / manifest["entrypoint"]).rglob("*"))
        for path in paths:
            if not path.is_file() or path.suffix not in (".json", ".py", ".md") or "__pycache__" in path.parts:
                continue
            info = zipfile.ZipInfo(path.relative_to(source).as_posix(), (2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            z.writestr(info, path.read_bytes())
    return manifest, stream.getvalue()


def build(dest: Path, tag: str):
    dest.mkdir(parents=True, exist_ok=True)
    catalog = {"api": 1, "extensions": []}
    for source in sorted((ROOT / "extensions").iterdir()):
        if not (source / "manifest.json").exists():
            continue
        manifest, raw = package(source)
        name = manifest["id"] + ".upvext"
        (dest / name).write_bytes(raw)
        catalog["extensions"].append({**manifest, "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
            "url": f"https://github.com/Thomblydu54/Upscale-video/releases/download/{tag}/{name}"})
    (dest / "extension-catalog.json").write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
    return catalog


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dest", default="release")
    parser.add_argument("--tag", default="v" + __version__)
    args = parser.parse_args()
    print(json.dumps(build(Path(args.dest), args.tag), ensure_ascii=False, indent=2))
