"""Localisation des outils externes (ffmpeg, ffprobe, realesrgan) et helpers subprocess."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import zlib
import struct

IS_WIN = os.name == "nt"
EXE = ".exe" if IS_WIN else ""
CREATE_NO_WINDOW = 0x08000000 if IS_WIN else 0

UPSCALER_BIN = "realesrgan-ncnn-vulkan"


class ToolMissing(RuntimeError):
    pass


def resource_root() -> str:
    """Dossier racine des ressources (dans l'exe PyInstaller ou dans le dépôt)."""
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def bin_dir() -> str:
    env = os.environ.get("UPSCALER_BIN_DIR")
    if env:
        return env
    return os.path.join(resource_root(), "bin")


def models_dir() -> str:
    env = os.environ.get("UPSCALER_MODELS_DIR")
    if env:
        return env
    return os.path.join(bin_dir(), "models")


def find_tool(name: str) -> str:
    candidate = os.path.join(bin_dir(), name + EXE)
    if os.path.isfile(candidate):
        return candidate
    found = shutil.which(name)
    if found:
        return found
    raise ToolMissing(f"Outil introuvable : {name}{EXE} (attendu dans {bin_dir()})")


def popen(args, **kwargs) -> subprocess.Popen:
    if IS_WIN:
        kwargs.setdefault("creationflags", CREATE_NO_WINDOW)
    return subprocess.Popen([str(a) for a in args], **kwargs)


def run(args, **kwargs) -> subprocess.CompletedProcess:
    if IS_WIN:
        kwargs.setdefault("creationflags", CREATE_NO_WINDOW)
    return subprocess.run([str(a) for a in args], **kwargs)


def open_path(path: str) -> None:
    """Ouvre un fichier / dossier avec l'application par défaut du système."""
    try:
        if IS_WIN:
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        pass


def make_png(width: int, height: int, rgb=(128, 128, 128)) -> bytes:
    """Crée une petite image PNG unie (sans dépendance)."""
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


_GPU_RE = re.compile(r"^\[(\d+)\s+(.+?)\]\s+queueC", re.M)


def detect_gpus() -> tuple[list[tuple[int, str]], str]:
    """Lance realesrgan sur une image minuscule pour lister les GPU Vulkan.

    Retourne (liste [(index, nom)], sortie brute)."""
    exe = find_tool(UPSCALER_BIN)
    tmp = tempfile.mkdtemp(prefix="upv_gpu_")
    try:
        src = os.path.join(tmp, "in.png")
        dst = os.path.join(tmp, "out.png")
        with open(src, "wb") as f:
            f.write(make_png(16, 16))
        proc = run([exe, "-i", src, "-o", dst, "-n", "realesr-animevideov3",
                    "-s", "2", "-m", models_dir()],
                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
        out = proc.stdout.decode("utf-8", "replace")
        gpus = []
        seen = set()
        for m in _GPU_RE.finditer(out):
            idx = int(m.group(1))
            if idx not in seen:
                seen.add(idx)
                gpus.append((idx, m.group(2).strip()))
        return gpus, out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def keep_awake(enable: bool) -> None:
    """Empêche Windows de se mettre en veille pendant un traitement."""
    if not IS_WIN:
        return
    try:
        import ctypes
        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if enable else 0)
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
    except Exception:
        pass
