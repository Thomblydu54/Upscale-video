"""Mises à jour automatiques via les Releases GitHub.

Fonctionnement :
  1. `check()` interroge l'API GitHub (dernière release) et compare avec __version__.
  2. `download()` télécharge le bon fichier (exe unique ou zip portable) en vérifiant
     sa taille et son empreinte SHA-256 (fournie par GitHub).
  3. `apply_and_restart()` remplace l'application puis la relance :
       - exe unique  : l'exe en cours est renommé en « .old » (Windows l'autorise),
                       le nouveau prend sa place, puis il est lancé ;
       - portable    : un petit script attend la fermeture, copie les nouveaux
                       fichiers par-dessus puis relance l'application.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile
from typing import Callable, Optional

from . import __version__, tools

REPO = "Thomblydu54/Upscale-video"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
ONEFILE_ASSET = "UpscaleVideoAI.exe"
PORTABLE_ASSET = "UpscaleVideoAI-portable.zip"
USER_AGENT = f"UpscaleVideoAI/{__version__}"


class UpdateError(RuntimeError):
    pass


def parse_version(text: str) -> tuple:
    nums = re.findall(r"\d+", str(text or ""))
    return tuple(int(n) for n in nums[:4]) or (0,)


def install_mode() -> str:
    """'onefile' (exe unique), 'onedir' (version portable) ou 'dev' (sources Python)."""
    if not getattr(sys, "frozen", False):
        return "dev"
    exe_dir = os.path.normcase(os.path.abspath(os.path.dirname(sys.executable)))
    meipass = os.path.normcase(os.path.abspath(getattr(sys, "_MEIPASS", exe_dir)))
    return "onedir" if meipass.startswith(exe_dir) else "onefile"


def _get_json(url: str, timeout: float = 15) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def check(current: str = __version__, api_url: str = API_LATEST) -> Optional[dict]:
    """Retourne les infos de la nouvelle version, ou None si l'application est à jour."""
    data = _get_json(api_url)
    tag = data.get("tag_name", "")
    if parse_version(tag) <= parse_version(current):
        return None
    mode = install_mode()
    wanted = PORTABLE_ASSET if mode == "onedir" else ONEFILE_ASSET
    asset = next((a for a in data.get("assets", []) if a.get("name") == wanted), None)
    digest = (asset or {}).get("digest") or ""
    return {
        "version": tag.lstrip("v"),
        "tag": tag,
        "current": current,
        "notes": (data.get("body") or "").strip(),
        "page": data.get("html_url", f"https://github.com/{REPO}/releases/latest"),
        "published": data.get("published_at", ""),
        "asset": wanted if asset else "",
        "url": (asset or {}).get("browser_download_url", ""),
        "size": int((asset or {}).get("size") or 0),
        "sha256": digest.split(":", 1)[1] if digest.startswith("sha256:") else "",
        "mode": mode,
        "can_install": bool(asset) and mode in ("onefile", "onedir") and tools.IS_WIN,
    }


def download(url: str, dest: str, expected_size: int = 0, sha256: str = "",
             progress: Optional[Callable[[int, int], None]] = None,
             cancel: Optional[threading.Event] = None) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    h = hashlib.sha256()
    done = 0
    tmp = dest + ".part"
    try:
        with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length") or expected_size or 0)
            while True:
                if cancel is not None and cancel.is_set():
                    raise UpdateError("Téléchargement annulé.")
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
        if expected_size and done != expected_size:
            raise UpdateError(f"Téléchargement incomplet ({done} / {expected_size} octets).")
        if sha256 and h.hexdigest().lower() != sha256.lower():
            raise UpdateError("Le fichier téléchargé est corrompu (empreinte SHA-256 différente).")
        os.replace(tmp, dest)
        return dest
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _clean_env() -> dict:
    """Environnement propre pour relancer une application PyInstaller depuis elle-même."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("_PYI") and k not in ("_MEIPASS2", "TCL_LIBRARY", "TK_LIBRARY")}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env


def _spawn_detached(args: list, cwd: Optional[str] = None) -> None:
    flags = 0
    if tools.IS_WIN:
        flags = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED | NEW_GROUP | NO_WINDOW
    subprocess.Popen(args, cwd=cwd, env=_clean_env(), creationflags=flags, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def swap_executable(new_file: str, target: str) -> str:
    """Remplace `target` (éventuellement en cours d'exécution) par `new_file`.

    Retourne le chemin de l'ancienne version renommée."""
    old = target + ".old"
    if os.path.exists(old):
        try:
            os.remove(old)
        except OSError:
            old = target + f".{os.getpid()}.old"
    _replace_retry(target, old)          # autorisé même si l'exe tourne
    try:
        _replace_retry(new_file, target)
    except BaseException:
        _replace_retry(old, target)      # retour arrière
        raise
    return old


def _replace_retry(src: str, dst: str, attempts: int = 20) -> None:
    """os.replace avec nouvelles tentatives : un antivirus (ou Windows lui-même, pendant le
    démarrage de l'exe) peut garder le fichier ouvert quelques secondes."""
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(0.5)


def cleanup_old_versions() -> None:
    """Supprime les « .old » laissés par une mise à jour précédente."""
    if install_mode() != "onefile":
        return
    folder = os.path.dirname(sys.executable)
    base = os.path.basename(sys.executable)
    for name in os.listdir(folder):
        if name.startswith(base + ".") and name.endswith((".old", ".new", ".new.part")):
            try:
                os.remove(os.path.join(folder, name))
            except OSError:
                pass


def prepare(info: dict, progress: Optional[Callable[[int, int], None]] = None,
            cancel: Optional[threading.Event] = None) -> dict:
    """Télécharge la mise à jour. Retourne un « plan » à passer à apply_and_restart()."""
    if not info.get("can_install"):
        raise UpdateError("Installation automatique impossible pour ce type d'installation.")
    mode = info["mode"]
    if mode == "onefile":
        target = sys.executable
        dest = target + ".new"
        try:
            download(info["url"], dest, info.get("size", 0), info.get("sha256", ""), progress, cancel)
        except PermissionError as e:
            raise UpdateError("Le dossier de l'application est protégé en écriture. Déplacez "
                              "UpscaleVideoAI.exe dans un dossier personnel (ex. Bureau) ou "
                              "téléchargez la mise à jour manuellement.") from e
        return {"mode": mode, "new": dest, "target": target}

    # version portable : zip -> dossier temporaire
    app_dir = os.path.dirname(sys.executable)
    work = tempfile.mkdtemp(prefix="upv_update_")
    zpath = os.path.join(work, PORTABLE_ASSET)
    download(info["url"], zpath, info.get("size", 0), info.get("sha256", ""), progress, cancel)
    with zipfile.ZipFile(zpath) as z:
        z.extractall(os.path.join(work, "new"))
    os.remove(zpath)
    root = os.path.join(work, "new")
    exe_name = os.path.basename(sys.executable)
    src = next((dp for dp, _, files in os.walk(root) if exe_name in files), None)
    if not src:
        raise UpdateError("Archive de mise à jour invalide.")
    return {"mode": mode, "src": src, "work": work, "target": app_dir,
            "exe": os.path.join(app_dir, exe_name)}


def apply_and_restart(plan: dict) -> None:
    """Installe la mise à jour préparée et relance l'application.

    L'appelant doit ensuite fermer l'application (os._exit)."""
    if plan["mode"] == "onefile":
        swap_executable(plan["new"], plan["target"])
        _spawn_detached([plan["target"]], cwd=os.path.dirname(plan["target"]))
        return
    script = os.path.join(plan["work"], "appliquer_maj.cmd")
    pid = os.getpid()
    with open(script, "w", encoding="utf-8") as f:
        f.write(
            "@echo off\r\nchcp 65001 >nul\r\n"
            ":wait\r\n"
            f'tasklist /FI "PID eq {pid}" 2>nul | find "{pid}" >nul && '
            "(timeout /t 1 /nobreak >nul & goto wait)\r\n"
            f'robocopy "{plan["src"]}" "{plan["target"]}" /E /R:10 /W:1 /NFL /NDL /NJH /NJS /NP >nul\r\n'
            f'start "" "{plan["exe"]}"\r\n'
            f'cd /d "%TEMP%"\r\n'
            f'rmdir /S /Q "{plan["work"]}"\r\n')
    _spawn_detached(["cmd.exe", "/c", script], cwd=plan["work"])
