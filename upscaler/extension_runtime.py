"""Services partagés des extensions officielles : stockage privé, TLS et démarrage volontaire.

Aucun service ne démarre à l'import. L'inscription au démarrage est limitée à la session
Windows de l'utilisateur ; elle ne nécessite ni élévation ni modification du pare-feu.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import ssl
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import tools, updater
from .jobs import data_dir


class ExtensionError(RuntimeError):
    pass


def private_dir(*parts: str) -> Path:
    path = Path(data_dir(), "extensions-data", *parts)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {} if default is None else default


def durable_replace(source, destination) -> None:
    """Renommage atomique confirmé sur disque avant l'émission d'un reçu de livraison."""
    if tools.IS_WIN:
        import ctypes
        from ctypes import wintypes
        move = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
        move.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
        move.restype = wintypes.BOOL
        for attempt in range(20):
            if move(str(source), str(destination), 0x1 | 0x8):  # REPLACE_EXISTING | WRITE_THROUGH
                break
            error = ctypes.get_last_error()
            if error not in (5, 32, 33) or attempt == 19:
                raise ctypes.WinError(error)
            time.sleep(0.05)  # lecteur de config / antivirus : partager le fichier sans échouer au hasard
    else:
        os.replace(source, destination)
        with contextlib.suppress(OSError):
            fd = os.open(Path(destination).parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(fd)
            finally:
                os.close(fd)


def atomic_bytes(path, content: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Plusieurs processus peuvent écrire des fichiers différents dans le même dossier.
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        durable_replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def atomic_json(path, data) -> None:
    atomic_bytes(path, json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8"))


def _dpapi(data: bytes, decrypt=False) -> bytes:
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buf = ctypes.create_string_buffer(data)
    src = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    dst = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    fn.restype = wintypes.BOOL
    k32.LocalFree.argtypes = [ctypes.c_void_p]
    if not fn(ctypes.byref(src), None, None, None, None, 1, ctypes.byref(dst)):
        raise ExtensionError("Impossible de protéger/lire les clés avec votre session Windows.")
    try:
        return ctypes.string_at(dst.data, dst.size)
    finally:
        k32.LocalFree(dst.data)


def save_private(path, data) -> None:
    raw = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
    atomic_bytes(path, b"DPAPI1\n" + _dpapi(raw) if tools.IS_WIN else b"JSON1\n" + raw)


def load_private(path) -> dict:
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        return {}
    if raw.startswith(b"DPAPI1\n") and tools.IS_WIN:
        raw = _dpapi(raw[7:], decrypt=True)
    elif raw.startswith(b"JSON1\n") and not tools.IS_WIN:
        raw = raw[6:]
    else:
        raise ExtensionError("Clés illisibles : réappairez ce PC depuis sa session Windows.")
    return json.loads(raw.decode("utf-8"))


def sha256_file(path, check=None, progress=None) -> str:
    h = hashlib.sha256()
    done = 0
    with open(path, "rb") as f:
        while chunk := f.read(2 << 20):
            if check:
                check()
            h.update(chunk)
            done += len(chunk)
            if progress:
                progress(done)
    return h.hexdigest()


def tls_identity(folder) -> str:
    """Certificat propre à cette installation. Sa SHA-256 est transmise hors bande au pair.

    Le client vérifie cette empreinte AVANT d'envoyer ses clés (pas de confiance aveugle
    dans un certificat auto-signé). Pas de domaine ni de certificat payant nécessaires.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    folder = Path(folder)
    cert_path, key_path = folder / "tls.crt", folder / "tls.key"
    if not cert_path.exists() or not key_path.exists():
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Upscale Video AI — relais privé")])
        now = datetime.now(timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=3650))
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .sign(key, hashes.SHA256()))
        atomic_bytes(key_path, key.private_bytes(serialization.Encoding.PEM,
                     serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        atomic_bytes(cert_path, cert.public_bytes(serialization.Encoding.PEM))
    der = ssl.PEM_cert_to_DER_cert(cert_path.read_text(encoding="ascii"))
    return hashlib.sha256(der).hexdigest()


def service_command(extension_id="remote-compute") -> list[str]:
    if extension_id != "remote-compute":
        raise ExtensionError("Service d'extension non reconnu.")
    if getattr(sys, "frozen", False):
        return [sys.executable, "--extension-service", extension_id]
    python = Path(sys.executable)
    if tools.IS_WIN and python.with_name("pythonw.exe").exists():
        python = python.with_name("pythonw.exe")
    return [str(python), str(Path(tools.resource_root(), "main.py")),
            "--extension-service", extension_id]


def set_startup(enabled: bool) -> None:
    """Opt-in, HKCU uniquement. L'utilisateur peut l'enlever depuis l'app ou Windows."""
    if not tools.IS_WIN:
        if enabled:
            raise ExtensionError("Le démarrage automatique intégré est réservé à Windows.")
        return
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                         r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
        if enabled:
            winreg.SetValueEx(key, "UpscaleVideoAI.CalculDistant", 0, winreg.REG_SZ,
                             subprocess.list2cmdline(service_command()))
        else:
            with contextlib.suppress(FileNotFoundError):
                winreg.DeleteValue(key, "UpscaleVideoAI.CalculDistant")


def spawn_service() -> None:
    flags = (0x00000008 | 0x00000200 | tools.CREATE_NO_WINDOW) if tools.IS_WIN else 0
    # Ne PAS utiliser tools.popen : le service doit survivre à la fermeture de l'interface.
    cwd = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else tools.resource_root()
    process = subprocess.Popen(service_command(), cwd=cwd, env=updater._clean_env(),
                     creationflags=flags, start_new_session=not tools.IS_WIN, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    threading.Thread(target=process.wait, daemon=True).start()


class ServiceLock:
    """Verrou OS libéré même après un crash : jamais deux services pour une installation."""
    def __init__(self, path, message="Le service est déjà en cours d'exécution."):
        self.path = Path(path)
        self.message = message
        self.file = None

    def __enter__(self):
        self.file = open(self.path, "a+b")
        self.file.seek(0)
        self.file.write(b"0")
        self.file.flush()
        self.file.seek(0)
        try:
            if tools.IS_WIN:
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            self.file = None
            raise ExtensionError(self.message) from None
        return self

    def __exit__(self, *args):
        if self.file:
            self.file.close()
