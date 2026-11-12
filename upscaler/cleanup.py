"""Nettoyage automatique au démarrage : rien ne s'accumule sur le disque.

En fonctionnement normal, chaque dossier temporaire est supprimé dès qu'il ne sert plus.
Ce module rattrape ce qui a pu rester après une session interrompue brutalement
(plantage, gestionnaire des tâches, coupure de courant, arrêt forcé de Windows) :

  * dossiers de travail « upv_* » (images PNG des lots en cours : parfois plusieurs Go) ;
  * avancement de traitements qui n'existent plus (« UpscaleVideoAI-reprise ») ;
  * images d'aperçu de plus de 24 h ;
  * dossiers « _MEI… » de l'exe (fichiers de l'application décompressés à chaque lancement,
    ~300 Mo) laissés par une session tuée — uniquement s'ils ne sont plus utilisés ;
  * fichiers « .partiel » de vidéos finales jamais terminées (gérés par jobs.py).

Les dossiers d'une application encore ouverte ne sont jamais touchés.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from typing import Callable, Iterable

from . import tools

WORK_PREFIX = "upv_"
RESUME_DIR = "UpscaleVideoAI-reprise"
PREVIEW_DIR = "UpscaleVideoAI_apercus"
PREVIEW_MAX_AGE = 24 * 3600
NO_OWNER_MIN_AGE = 3600       # dossier sans marqueur (anciennes versions) : au moins 1 h
MEI_MIN_AGE = 15 * 60


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if tools.IS_WIN:
        try:
            import ctypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.OpenProcess.restype = ctypes.c_void_p
            h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                # accès refusé = le processus existe (autre utilisateur) ; sinon il n'existe plus
                return ctypes.get_last_error() == 5
            try:
                code = ctypes.c_ulong()
                if not k32.GetExitCodeProcess(ctypes.c_void_p(h), ctypes.byref(code)):
                    return True
                return code.value == 259  # STILL_ACTIVE
            finally:
                k32.CloseHandle(ctypes.c_void_p(h))
        except Exception:  # noqa: BLE001
            return True  # dans le doute, on ne supprime rien
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _age(path: str) -> float:
    try:
        return time.time() - os.path.getmtime(path)
    except OSError:
        return 0.0


def _size(path: str) -> int:
    total = 0
    for dp, _, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(dp, name))
            except OSError:
                pass
    return total


def _remove_tree(path: str) -> int:
    size = _size(path)
    shutil.rmtree(path, ignore_errors=True)
    return size if not os.path.exists(path) else size - _size(path)


def _orphan_work_dir(path: str) -> bool:
    """Vrai si le dossier de travail n'appartient à aucune application ouverte."""
    owner = os.path.join(path, tools.OWNER_FILE)
    try:
        with open(owner, "r", encoding="ascii") as f:
            pid = int(f.read().strip() or 0)
    except (OSError, ValueError):
        return _age(path) > NO_OWNER_MIN_AGE  # pas de marqueur : prudence, on attend 1 h
    return not _pid_alive(pid)


def clean_work_dirs(roots: Iterable[str]) -> int:
    freed = 0
    for root in roots:
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for name in names:
            path = os.path.join(root, name)
            if name.startswith(WORK_PREFIX) and os.path.isdir(path) and _orphan_work_dir(path):
                freed += _remove_tree(path)
    return freed


def clean_resume_dirs(roots: Iterable[str], known_ids: Callable[[], set]) -> int:
    """Avancement placé dans un dossier temporaire personnalisé dont le traitement n'existe plus."""
    freed = 0
    for root in roots:
        base = os.path.join(root, RESUME_DIR)
        try:
            names = os.listdir(base)
        except OSError:
            continue
        for name in names:
            path = os.path.join(base, name)
            # liste relue à chaque fois : un traitement lancé pendant le nettoyage est protégé
            if os.path.isdir(path) and _age(path) > 120 and name not in known_ids():
                freed += _remove_tree(path)
        try:
            os.rmdir(base)
        except OSError:
            pass
    return freed


def clean_previews() -> int:
    folder = os.path.join(tempfile.gettempdir(), PREVIEW_DIR)
    freed = 0
    try:
        names = os.listdir(folder)
    except OSError:
        return 0
    for name in names:
        path = os.path.join(folder, name)
        if os.path.isfile(path) and _age(path) > PREVIEW_MAX_AGE:
            try:
                size = os.path.getsize(path)
                os.remove(path)
                freed += size
            except OSError:
                pass
    return freed


def clean_pyinstaller_leftovers() -> int:
    """Dossiers « _MEIxxxx » d'anciennes sessions de l'exe tuées avant d'avoir pu nettoyer.

    Sous Windows, renommer un dossier dont un fichier est ouvert (DLL chargée par une
    application en cours) échoue : c'est ce test qui garantit qu'il n'est plus utilisé."""
    current = getattr(sys, "_MEIPASS", None)
    if not current or not tools.IS_WIN:
        return 0
    root = os.path.dirname(os.path.abspath(current))
    freed = 0
    try:
        names = os.listdir(root)
    except OSError:
        return 0
    for name in names:
        path = os.path.join(root, name)
        if (not name.startswith("_MEI") or name.endswith("_suppr")
                or os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(current))
                or not os.path.isdir(path) or _age(path) < MEI_MIN_AGE):
            continue
        # uniquement les dossiers de CETTE application
        if not (os.path.isfile(os.path.join(path, "ui", "index.html"))
                and os.path.isfile(os.path.join(path, "ui", "mock.js"))
                and os.path.isdir(os.path.join(path, "bin"))):
            continue
        trash = path + "_suppr"
        try:
            os.rename(path, trash)
        except OSError:
            continue  # encore utilisé par une autre fenêtre de l'application
        freed += _remove_tree(trash)
    for name in names:  # restes d'une suppression interrompue
        if name.startswith("_MEI") and name.endswith("_suppr"):
            freed += _remove_tree(os.path.join(root, name))
    return freed


def startup_cleanup(temp_dirs: Iterable[str] = (), known_job_ids: Callable[[], set] = set,
                    log: Callable[[str], None] = lambda m: None) -> int:
    """Lance tous les nettoyages. Retourne le nombre d'octets libérés."""
    roots = [tempfile.gettempdir()]
    for d in temp_dirs:
        if d and os.path.isdir(d) and d not in roots:
            roots.append(d)
    freed = 0
    for step in (lambda: clean_work_dirs(roots),
                 lambda: clean_resume_dirs(roots, known_job_ids),
                 clean_previews,
                 clean_pyinstaller_leftovers):
        try:
            freed += step()
        except Exception as e:  # noqa: BLE001
            log(f"Nettoyage : {e}")
    if freed >= 1_000_000:
        log(f"🧹 Nettoyage : {freed / 1e6:,.0f} Mo de fichiers temporaires oubliés supprimés."
            .replace(",", " "))
    return freed
