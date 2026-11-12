"""Télécharge les outils externes dans ./bin :

  * realesrgan-ncnn-vulkan (moteur IA, GPU via Vulkan : NVIDIA / AMD / Intel)
  * modèles Real-ESRGAN officiels + modèles Upscayl (lite / standard)
  * rife-ncnn-vulkan + modèle rife-v4.6 (interpolation d'images, GPU via Vulkan)
  * ffmpeg + ffprobe (builds BtbN, GPL, partagés)

Usage :  python scripts/fetch_tools.py [--platform windows|linux]
"""
from __future__ import annotations

import argparse
import io
import os
import shutil
import sys
import tarfile
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "bin")
MODELS = os.path.join(BIN, "models")

REALESRGAN = {
    "windows": "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/"
               "realesrgan-ncnn-vulkan-20220424-windows.zip",
    "linux": "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/"
             "realesrgan-ncnn-vulkan-20220424-ubuntu.zip",
}
FFMPEG = {
    "windows": "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
               "ffmpeg-n8.1-latest-win64-gpl-shared-8.1.zip",
    "linux": "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
             "ffmpeg-n8.1-latest-linux64-gpl-8.1.tar.xz",
}
RIFE = {
    "windows": "https://github.com/nihui/rife-ncnn-vulkan/releases/download/20221029/"
               "rife-ncnn-vulkan-20221029-windows.zip",
    "linux": "https://github.com/nihui/rife-ncnn-vulkan/releases/download/20221029/"
             "rife-ncnn-vulkan-20221029-ubuntu.zip",
}
RIFE_MODELS = ["rife-v4.6"]
UPSCAYL_COMMIT = "a00d55fee90e0f9435d5eaa86e76700df8199af8"
UPSCAYL_MODELS = ["upscayl-lite-4x", "upscayl-standard-4x"]
UPSCAYL_URL = ("https://raw.githubusercontent.com/upscayl/upscayl/"
               f"{UPSCAYL_COMMIT}/resources/models/{{name}}")


def download(url: str) -> bytes:
    print(f"  -> {url}", flush=True)
    req = urllib.request.Request(url, headers={"User-Agent": "UpscaleVideoAI-build"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            if attempt == 3:
                raise
            print(f"    nouvelle tentative ({e})", flush=True)
    raise RuntimeError("unreachable")


def fetch_realesrgan(platform: str) -> None:
    print("Real-ESRGAN ncnn-vulkan…")
    data = zipfile.ZipFile(io.BytesIO(download(REALESRGAN[platform])))
    exe_name = "realesrgan-ncnn-vulkan" + (".exe" if platform == "windows" else "")
    exe_member = next(n for n in data.namelist() if n.endswith(exe_name))
    prefix = exe_member[: -len(exe_name)]
    for name in data.namelist():
        if not name.startswith(prefix) or name.endswith("/"):
            continue
        rel = name[len(prefix):]
        if rel.lower().endswith((".jpg", ".png", ".mp4", ".md", ".txt")) and "/" not in rel:
            continue  # images d'exemple
        dest = os.path.join(BIN, *rel.split("/"))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with data.open(name) as src, open(dest, "wb") as dst:
            shutil.copyfileobj(src, dst)
    if platform != "windows":
        os.chmod(os.path.join(BIN, exe_name), 0o755)


def fetch_rife(platform: str) -> None:
    print("RIFE ncnn-vulkan (interpolation)…")
    data = zipfile.ZipFile(io.BytesIO(download(RIFE[platform])))
    exe_name = "rife-ncnn-vulkan" + (".exe" if platform == "windows" else "")
    exe_member = next(n for n in data.namelist() if n.endswith("/" + exe_name) or n == exe_name)
    prefix = exe_member[: -len(exe_name)]
    copied = []
    for name in data.namelist():
        if not name.startswith(prefix) or name.endswith("/"):
            continue
        rel = name[len(prefix):]
        top = rel.split("/")[0]
        if "/" in rel and top not in RIFE_MODELS:
            continue  # autres modèles : inutiles
        if "/" not in rel and not (rel == exe_name or rel.lower().endswith(".dll")):
            continue
        dest = os.path.join(BIN, *rel.split("/"))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with data.open(name) as src, open(dest, "wb") as dst:
            shutil.copyfileobj(src, dst)
        copied.append(rel)
    if platform != "windows":
        os.chmod(os.path.join(BIN, exe_name), 0o755)
    for m in RIFE_MODELS:
        if not any(c.startswith(m + "/") for c in copied):
            raise RuntimeError(f"modèle {m} absent de l'archive RIFE")
    print("    " + ", ".join(sorted(copied)))


def fetch_upscayl_models() -> None:
    print("Modèles Upscayl…")
    os.makedirs(MODELS, exist_ok=True)
    for model in UPSCAYL_MODELS:
        for ext in (".param", ".bin"):
            with open(os.path.join(MODELS, model + ext), "wb") as f:
                f.write(download(UPSCAYL_URL.format(name=model + ext)))


def fetch_ffmpeg(platform: str) -> None:
    print("FFmpeg…")
    raw = download(FFMPEG[platform])
    if platform == "windows":
        z = zipfile.ZipFile(io.BytesIO(raw))
        for name in z.namelist():
            base = os.path.basename(name)
            if "/bin/" in name and (base in ("ffmpeg.exe", "ffprobe.exe") or base.endswith(".dll")):
                with z.open(name) as src, open(os.path.join(BIN, base), "wb") as dst:
                    shutil.copyfileobj(src, dst)
    else:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:xz") as t:
            for m in t.getmembers():
                base = os.path.basename(m.name)
                if "/bin/" in m.name and base in ("ffmpeg", "ffprobe"):
                    with t.extractfile(m) as src, open(os.path.join(BIN, base), "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    os.chmod(os.path.join(BIN, base), 0o755)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform", choices=["windows", "linux"],
                    default="windows" if os.name == "nt" else "linux")
    args = ap.parse_args()
    os.makedirs(BIN, exist_ok=True)
    fetch_realesrgan(args.platform)
    fetch_upscayl_models()
    fetch_rife(args.platform)
    fetch_ffmpeg(args.platform)
    print("\nContenu de bin/ :")
    for dirpath, _, files in os.walk(BIN):
        for f in sorted(files):
            p = os.path.join(dirpath, f)
            print(f"  {os.path.relpath(p, BIN):45s} {os.path.getsize(p) / 1e6:8.1f} Mo")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # annotation visible dans l'interface GitHub Actions
        import traceback
        traceback.print_exc()
        print(f"::error title=fetch_tools::{type(exc).__name__}: {exc}".replace("\n", " "))
        sys.exit(1)
