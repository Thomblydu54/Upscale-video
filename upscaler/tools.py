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


_KILL_JOB = None


def _kill_with_app(proc: subprocess.Popen) -> None:
    """Windows : rattache l'outil (ffmpeg, IA…) à un « Job Object » qui le termine
    automatiquement si l'application se ferme brutalement (plantage, gestionnaire des tâches),
    pour qu'il ne continue pas à tourner en arrière-plan ni ne bloque la reprise."""
    global _KILL_JOB
    if not IS_WIN:
        return
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        if _KILL_JOB is None:
            class IO_COUNTERS(ctypes.Structure):
                _fields_ = [(n, ctypes.c_ulonglong) for n in (
                    "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                    "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

            class BASIC_LIMIT(ctypes.Structure):
                _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                            ("PerJobUserTimeLimit", ctypes.c_int64),
                            ("LimitFlags", wintypes.DWORD),
                            ("MinimumWorkingSetSize", ctypes.c_size_t),
                            ("MaximumWorkingSetSize", ctypes.c_size_t),
                            ("ActiveProcessLimit", wintypes.DWORD),
                            ("Affinity", ctypes.c_size_t),
                            ("PriorityClass", wintypes.DWORD),
                            ("SchedulingClass", wintypes.DWORD)]

            class EXT_LIMIT(ctypes.Structure):
                _fields_ = [("BasicLimitInformation", BASIC_LIMIT), ("IoInfo", IO_COUNTERS),
                            ("ProcessMemoryLimit", ctypes.c_size_t),
                            ("JobMemoryLimit", ctypes.c_size_t),
                            ("PeakProcessMemoryUsed", ctypes.c_size_t),
                            ("PeakJobMemoryUsed", ctypes.c_size_t)]

            k32.CreateJobObjectW.restype = wintypes.HANDLE
            k32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
            k32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                    wintypes.DWORD)
            job = k32.CreateJobObjectW(None, None)
            if not job:
                _KILL_JOB = 0
                return
            info = EXT_LIMIT()
            info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
                _KILL_JOB = 0
                return
            _KILL_JOB = job  # handle volontairement jamais fermé (fermé par Windows à la sortie)
        if _KILL_JOB:
            k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
            k32.AssignProcessToJobObject(_KILL_JOB, int(proc._handle))
    except Exception:  # noqa: BLE001
        _KILL_JOB = 0


def popen(args, **kwargs) -> subprocess.Popen:
    if IS_WIN:
        kwargs.setdefault("creationflags", CREATE_NO_WINDOW)
    proc = subprocess.Popen([str(a) for a in args], **kwargs)
    _kill_with_app(proc)
    return proc


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


OWNER_FILE = "upv.pid"


def make_work_dir(prefix: str, dir: str | None = None) -> str:
    """Crée un dossier de travail temporaire marqué avec le PID de l'application.

    Le marqueur permet au nettoyage du démarrage de supprimer sans risque les dossiers
    laissés par une session interrompue (plantage, arrêt du PC), jamais ceux d'une
    application encore ouverte."""
    if dir:
        os.makedirs(dir, exist_ok=True)
    path = tempfile.mkdtemp(prefix=prefix, dir=dir or None)
    try:
        with open(os.path.join(path, OWNER_FILE), "w", encoding="ascii") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass
    return path


def detect_gpus() -> tuple[list[tuple[int, str]], str]:
    """Lance realesrgan sur une image minuscule pour lister les GPU Vulkan.

    Retourne (liste [(index, nom)], sortie brute)."""
    exe = find_tool(UPSCALER_BIN)
    tmp = make_work_dir("upv_gpu_")
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
