"""Catalogue et chargement d'extensions provenant uniquement des releases officielles.

Une .upvext est un ZIP de code Python. Pas d'import de fichiers tiers ni de dépôt
personnalisé : l'API n'accepte qu'un identifiant du catalogue officiel, et vérifie le
SHA-256 publié avec la release avant d'installer. Le code n'est chargé qu'après opt-in.
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import re
import shutil
import sys
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

from . import __version__, updater
from .extension_runtime import (ExtensionError, ServiceLock, atomic_json, private_dir, read_json,
                                load_private, save_private, set_startup)
from .jobs import data_dir

API_VERSION = 1
MAX_PACKAGE = 4 << 20
MAX_UNPACKED = 12 << 20
OFFICIAL = {"remote-compute": {"id": "remote-compute", "name": "Calcul à distance",
    "description": "Confiez vos vidéos au GPU d'un autre PC, suivez le calcul et récupérez le résultat automatiquement.",
    "version": "1.0.1", "min_app": "1.9.0", "entrypoint": "upv_remote",
    "permissions": ["Réseau et transfert de vos médias", "Calcul GPU en arrière-plan",
                    "Démarrage à l'ouverture de session Windows (facultatif)"]}}


def _root() -> Path:
    path = Path(data_dir(), "extensions")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _official_download(url: str) -> None:
    p = urllib.parse.urlsplit(url)
    if (p.scheme != "https" or p.netloc != "github.com" or p.query or p.fragment
            or not p.path.startswith(f"/{updater.REPO}/releases/download/")):
        raise ExtensionError("Cette extension ne vient pas d'une release officielle.")


def _fetch(url: str, maximum: int) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": updater.USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as response:
        content = response.read(maximum + 1)
    if len(content) > maximum:
        raise ExtensionError("Fichier d'extension trop volumineux.")
    return content


class ExtensionManager:
    def __init__(self, log=lambda s: None):
        self._log = log
        self._lock = threading.RLock()
        self._plugins = {}
        self._catalog = {}
        self._error = ""
        self._busy = ""
        self._checked = False
        # Récupération si Windows s'est arrêté entre les deux renommages d'une mise à jour.
        try:
            with ServiceLock(_root() / "install.lock"):
                for key in OFFICIAL:
                    folder, previous = _root() / key, _root() / (key + ".previous")
                    if not folder.exists() and previous.exists():
                        previous.rename(folder)
        except ExtensionError:
            pass  # une autre instance installe actuellement ; ne jamais toucher à son stage

    def public(self) -> dict:
        with self._lock:
            items = []
            for key, info in OFFICIAL.items():
                record = read_json(_root() / key / "installed.json")
                available = self._catalog.get(key)
                items.append({**info, "installed": record.get("version", ""),
                              "available": bool(available),
                              "version": (available or info)["version"],
                              "update": bool(record and available and updater.parse_version(available["version"])
                                             > updater.parse_version(record.get("version", "0")))})
            return {"items": items, "error": self._error, "busy": self._busy,
                    "checked": self._checked}

    def refresh(self) -> dict:
        try:
            release = updater._get_json(updater.API_LATEST)
            asset = next((x for x in release.get("assets", [])
                          if x.get("name") == "extension-catalog.json"), None)
            if not asset:
                raise ExtensionError("Le catalogue n'est pas encore publié dans cette release.")
            url = asset["browser_download_url"]
            _official_download(url)
            raw = _fetch(url, 128 << 10)
            digest = asset.get("digest", "")
            if not digest.startswith("sha256:") or hashlib.sha256(raw).hexdigest() != digest[7:]:
                raise ExtensionError("L'intégrité du catalogue officiel n'a pas pu être vérifiée.")
            data = json.loads(raw)
            if data.get("api") != API_VERSION:
                raise ExtensionError("Mettez l'application à jour pour lire ce catalogue.")
            catalog = {}
            for info in data.get("extensions", []):
                key = info.get("id")
                if key not in OFFICIAL:
                    continue  # un nouveau type d'extension demande une API d'hôte compatible
                _official_download(info["url"])
                if not re.fullmatch(r"[0-9a-f]{64}", info.get("sha256", "")):
                    raise ExtensionError("Empreinte manquante dans le catalogue.")
                if not 0 < int(info.get("size", 0)) <= MAX_PACKAGE:
                    raise ExtensionError("Taille d'extension invalide.")
                if updater.parse_version(info["min_app"]) <= updater.parse_version(__version__):
                    catalog[key] = info
            with self._lock:
                self._catalog, self._error, self._checked = catalog, "", True
        except Exception as e:
            with self._lock:
                self._error, self._checked = str(e), True
            self._log(f"Extensions : {e}")
        return self.public()

    def install(self, key: str) -> None:
        with self._lock:
            if self._busy:
                raise ExtensionError("Une installation d'extension est déjà en cours.")
            self._busy = key
        try:
            if key not in OFFICIAL:
                raise ExtensionError("Extension inconnue.")
            if key not in self._catalog:
                self.refresh()
            info = self._catalog.get(key)
            if not info:
                raise ExtensionError(self._error or "Extension indisponible pour cette version.")
            _official_download(info["url"])
            raw = _fetch(info["url"], MAX_PACKAGE)
            self._install_verified(raw, info)
            self._log(f"Extension installée : {OFFICIAL[key]['name']} {info['version']}")
        finally:
            with self._lock:
                self._busy = ""

    def _install_verified(self, raw: bytes, info: dict) -> None:
        """Interne : info doit provenir du catalogue HTTPS officiel (ou des tests de build)."""
        key = info["id"]
        if key not in OFFICIAL or len(raw) > MAX_PACKAGE or len(raw) != int(info["size"]):
            raise ExtensionError("Archive d'extension invalide.")
        if hashlib.sha256(raw).hexdigest() != info["sha256"]:
            raise ExtensionError("L'extension téléchargée est corrompue (SHA-256 différent).")
        with self._lock, ServiceLock(_root() / "install.lock", "Une autre instance installe une extension."):
            try:
                old = self.load(key)
            except (ExtensionError, OSError) as e:
                self._log("Réparation d'une extension incomplète : " + str(e))
                self._stop_untrusted(key)
                old = None
            if old:
                old.before_remove()  # interdit une mise à jour pendant un travail en attente
            folder = _root() / key
            stage, backup = _root() / (key + ".install"), _root() / (key + ".previous")
            shutil.rmtree(stage, ignore_errors=True)
            stage.mkdir(mode=0o700)
            try:
                with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                    entries = archive.infolist()
                    if len(entries) > 100 or sum(x.file_size for x in entries) > MAX_UNPACKED:
                        raise ExtensionError("Archive démesurée.")
                    names = set()
                    hashes = {}
                    for entry in entries:
                        name = entry.filename
                        p = PurePosixPath(name)
                        if (name in names or "\\" in name or ":" in name or p.is_absolute()
                                or ".." in p.parts or not p.parts or p.as_posix() != name or name.endswith("/")
                                or (entry.external_attr >> 16) & 0o170000 == 0o120000
                                or not (name == "manifest.json" or
                                        (p.parts[0] == OFFICIAL[key]["entrypoint"]
                                         and name.endswith((".py", ".md"))))):
                            raise ExtensionError("Chemin interdit dans l'archive d'extension.")
                        names.add(name)
                        content = archive.read(entry)
                        target = stage / name
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(content)
                        hashes[name] = hashlib.sha256(content).hexdigest()
                manifest = read_json(stage / "manifest.json")
                if (manifest.get("id") != key or manifest.get("api") != API_VERSION
                        or manifest.get("version") != info["version"]
                        or manifest.get("entrypoint") != OFFICIAL[key]["entrypoint"]
                        or updater.parse_version(manifest.get("min_app", "999"))
                        > updater.parse_version(__version__)
                        or f"{manifest['entrypoint']}/__init__.py" not in hashes):
                    raise ExtensionError("Manifeste d'extension incompatible.")
                atomic_json(stage / "installed.json", {"id": key, "version": info["version"],
                             "sha256": info["sha256"], "files": hashes})
                shutil.rmtree(backup, ignore_errors=True)
                if folder.exists():
                    folder.rename(backup)
                try:
                    stage.rename(folder)
                except BaseException:
                    if backup.exists():
                        backup.rename(folder)
                    raise
                self._unload(key)
                shutil.rmtree(backup, ignore_errors=True)
            finally:
                shutil.rmtree(stage, ignore_errors=True)

    def _stop_untrusted(self, key):
        # Réparer sans importer/exécuter le moindre octet d'une installation corrompue.
        folder = private_dir(key)
        path = folder / "private.dat"
        config = load_private(path)
        if config.get("startup"):
            set_startup(False)
        config.update(enabled=False, startup=False)
        save_private(path, config)
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            try:
                with ServiceLock(folder / "service.lock"):
                    return
            except ExtensionError:
                time.sleep(0.2)
        raise ExtensionError("Le service termine sa mise en pause. Réessayez la réparation ensuite.")

    def _unload(self, key):
        self._plugins.pop(key, None)
        name = "upv_ext_" + key.replace("-", "_")
        for mod in list(sys.modules):
            if mod == name or mod.startswith(name + "."):
                del sys.modules[mod]

    def load(self, key="remote-compute"):
        with self._lock:
            if key in self._plugins:
                return self._plugins[key]
            if key not in OFFICIAL:
                return None
            folder = _root() / key
            record = read_json(folder / "installed.json")
            if not record:
                return None
            files = record.get("files", {})
            entry = OFFICIAL[key]["entrypoint"] + "/__init__.py"
            if entry not in files or "manifest.json" not in files:
                raise ExtensionError("Installation d'extension incomplète : réinstallez-la.")
            for name, digest in files.items():
                p = PurePosixPath(name)
                if p.is_absolute() or ".." in p.parts or "\\" in name:
                    raise ExtensionError("Installation d'extension invalide.")
                if hashlib.sha256((folder / name).read_bytes()).hexdigest() != digest:
                    raise ExtensionError("Une extension a été modifiée sur le disque : réinstallez-la.")
            name = "upv_ext_" + key.replace("-", "_")
            spec = importlib.util.spec_from_file_location(name, folder / entry,
                submodule_search_locations=[str(folder / OFFICIAL[key]["entrypoint"])])
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            try:
                spec.loader.exec_module(module)
                plugin = module.Plugin(private_dir(key), self._log)
                self._plugins[key] = plugin
                return plugin
            except BaseException:
                self._unload(key)
                raise

    def remove(self, key: str) -> None:
        with self._lock, ServiceLock(_root() / "install.lock", "Une autre instance installe une extension."):
            if self._busy or key not in OFFICIAL:
                raise ExtensionError("Extension inconnue ou installation en cours.")
            plugin = self.load(key)
            if plugin:
                plugin.before_remove()
            self._unload(key)
            shutil.rmtree(_root() / key, ignore_errors=True)
            # Les clés et l'appairage restent privés pour une éventuelle réinstallation.
            self._log(f"Extension désinstallée : {OFFICIAL[key]['name']}")


def run_service(key: str) -> None:
    plugin = ExtensionManager().load(key)
    if plugin is None:
        raise ExtensionError("L'extension n'est pas installée.")
    plugin.run_service()


def selftest_package(path: str) -> str:
    """Auto-test CI explicite ; ne modifie jamais les extensions de l'utilisateur."""
    import os
    import tempfile
    with tempfile.TemporaryDirectory(prefix="upv_extension_test_") as tmp:
        before = os.environ.get("UPSCALER_DATA_DIR")
        os.environ["UPSCALER_DATA_DIR"] = tmp
        manager = ExtensionManager()
        try:
            raw = Path(path).read_bytes()
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                manifest = json.loads(z.read("manifest.json"))
            manager._install_verified(raw, {**manifest, "size": len(raw),
                                            "sha256": hashlib.sha256(raw).hexdigest()})
            plugin = manager.load()
            module = __import__(type(plugin).__module__.rsplit(".", 1)[0] + ".selftest", fromlist=["run"])
            protocol = module.run()
            # Vrai lancement du même exe en mode service (pas seulement un objet dans
            # le processus de test). Aucun accès à la clé de démarrage HKCU du compte CI.
            import secrets
            import socket
            from .extension_runtime import tls_identity
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            plugin._save({"role": "controller", "enabled": True, "port": port,
                          "pin": tls_identity(plugin.folder), "token": secrets.token_urlsafe(32),
                          "startup": False, "revision": "packaged-selftest"})
            try:
                plugin.ensure_running()
                state = read_json(plugin.status_path)
                assert state["pid"] != os.getpid(), "Le service n'est pas détaché."
                assert plugin.controller().get("/v1/controller/state")["workers"] == []
                plugin.start()
                assert read_json(plugin.status_path)["pid"] == state["pid"], "Deux services démarrés."
                if os.name == "nt":
                    import ctypes
                    from ctypes import wintypes
                    user = ctypes.WinDLL("user32", use_last_error=True)
                    callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
                    user.EnumWindows.argtypes = [callback, wintypes.LPARAM]
                    user.EnumWindows.restype = wintypes.BOOL
                    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
                    user.IsWindowVisible.argtypes = [wintypes.HWND]
                    user.IsWindowVisible.restype = wintypes.BOOL
                    visible = []
                    @callback
                    def visit(hwnd, _):
                        pid = wintypes.DWORD()
                        user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                        if pid.value == state["pid"] and user.IsWindowVisible(hwnd):
                            visible.append(hwnd)
                        return True
                    assert user.EnumWindows(visit, 0), "Impossible de vérifier les fenêtres du service."
                    assert not visible, "Le service a ouvert une fenêtre visible."
            finally:
                plugin.stop()
            assert not plugin.public()["running"]
            return protocol + " · service réel détaché, sans fenêtre, démarrage idempotent et arrêt : OK"
        finally:
            manager._unload("remote-compute")
            if before is None:
                os.environ.pop("UPSCALER_DATA_DIR", None)
            else:
                os.environ["UPSCALER_DATA_DIR"] = before
