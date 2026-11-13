"""Interface moderne (HTML/CSS/JS) affichée dans une fenêtre native via pywebview (WebView2)."""
from __future__ import annotations

import base64
import copy
from fractions import Fraction
import json
import os
import subprocess
import sys
import threading
import time
import traceback

from concurrent.futures import ThreadPoolExecutor

from . import __version__, cleanup, engine, tools, updater
from .jobs import JobStore
from .engine import (ENCODERS, IMAGE_EXT, IMAGE_FORMATS, INTERPOLATIONS, MODELS, OUTPUT_SIZES,
                     SPEEDS, Cancelled, Settings, Upscaler, UpscaleError, fmt_time, fmt_time_ms)

VIDEO_EXT = (".mp4", ".mkv", ".mov", ".avi", ".wmv", ".flv", ".webm", ".m4v", ".mpg",
             ".mpeg", ".ts", ".m2ts", ".mts", ".vob", ".3gp", ".ogv", ".divx", ".asf", ".gif")
MEDIA_EXT = VIDEO_EXT + IMAGE_EXT
WEBVIEW2_GUID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"

# Métadonnées d'affichage des modèles (vitesse / qualité sur 5, icône)
MODEL_META = {
    "upscayl-lite-4x": {"speed": 5, "quality": 3, "icon": "film", "tag": "Recommandé"},
    "upscayl-standard-4x": {"speed": 2, "quality": 5, "icon": "sparkles", "tag": "Détails max"},
    "realesrgan-x4plus": {"speed": 2, "quality": 4, "icon": "wand", "tag": "Débruitage"},
    "realesr-animevideov3": {"speed": 5, "quality": 4, "icon": "brush", "tag": "Anime rapide"},
    "realesrgan-x4plus-anime": {"speed": 2, "quality": 5, "icon": "star", "tag": "Anime HQ"},
}
MODEL_SHORT = {
    "upscayl-lite-4x": ("Réaliste Rapide", "Films, séries, vidéos perso"),
    "upscayl-standard-4x": ("Réaliste Pro", "Le plus de détails, plus lent"),
    "realesrgan-x4plus": ("Real-ESRGAN", "Vidéos très bruitées / compressées"),
    "realesr-animevideov3": ("Anime Rapide", "Dessins animés, 2D, x2/x3/x4"),
    "realesrgan-x4plus-anime": ("Anime Pro", "Anime net et fidèle, plus lent"),
}


def config_path() -> str:
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    folder = os.path.join(base, "UpscaleVideoAI")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "settings.json")


def webview2_available() -> bool:
    """Vérifie la présence du runtime Microsoft Edge WebView2 (Windows)."""
    if not tools.IS_WIN:
        return True
    try:
        import winreg
    except ImportError:
        return False
    keys = [
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_GUID}"),
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_GUID}"),
        (winreg.HKEY_CURRENT_USER, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_GUID}"),
    ]
    for hive, path in keys:
        try:
            with winreg.OpenKey(hive, path) as k:
                version, _ = winreg.QueryValueEx(k, "pv")
                if version and version != "0.0.0.0":
                    return True
        except OSError:
            continue
    return False


def _thumbnail(path: str, duration: float) -> str:
    """Miniature JPEG (data URI) extraite à ~15 % de la vidéo."""
    t = duration * 0.15 if duration and duration > 2 else 0
    proc = tools.run([tools.find_tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin",
                      *engine._seek(t), "-i", path, "-frames:v", "1",
                      "-vf", "scale=480:-2:flags=bicubic", "-q:v", "4",
                      "-f", "image2pipe", "-c:v", "mjpeg", "-"],
                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=60)
    if proc.returncode != 0 or not proc.stdout:
        return ""
    return "data:image/jpeg;base64," + base64.b64encode(proc.stdout).decode("ascii")


def _jpeg_data_uri(src: str, width: int, height: int, flags: str) -> str:
    proc = tools.run([tools.find_tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin",
                      "-i", src, "-frames:v", "1", "-vf", f"scale={width}:{height}:flags={flags}",
                      "-q:v", "2", "-f", "image2pipe", "-c:v", "mjpeg", "-"],
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    if proc.returncode != 0:
        raise UpscaleError(proc.stderr.decode("utf-8", "replace")[-500:])
    return "data:image/jpeg;base64," + base64.b64encode(proc.stdout).decode("ascii")


class Api:
    """Méthodes appelées depuis JavaScript (window.pywebview.api.*).

    Tous les attributs internes commencent par « _ » pour ne pas être exposés à JS."""

    def __init__(self):
        self._window = None
        self._lock = threading.RLock()
        self._files: dict[int, dict] = {}
        self._order: list[int] = []
        self._next_id = 1
        self._events: list[dict] = []
        self._logs: list[str] = []
        self._running = False
        self._task = ""
        self._progress: dict | None = None
        self._upscaler: Upscaler | None = None
        self._gpus: list | None = None
        self._gpu_error = ""
        self._last_output_dir = ""
        self._settings = self._load_settings()
        self._update: dict | None = None
        self._updating = False
        self._frame_pool = ThreadPoolExecutor(max_workers=4)
        self._jobs = JobStore()
        self._pause_req = False
        self._cancel_req = False
        self._closing_ok = False
        self._restored = 0
        self._restore_jobs()
        threading.Thread(target=self._detect_gpus, daemon=True).start()

    # ------------------------------------------------------------------ interne
    def _load_settings(self) -> Settings:
        try:
            with open(config_path(), "r", encoding="utf-8") as f:
                return Settings.from_dict(json.load(f))
        except Exception:
            return Settings()

    def _save_settings(self) -> None:
        try:
            with open(config_path(), "w", encoding="utf-8") as f:
                json.dump(self._settings.to_dict(), f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    def _emit(self, **event) -> None:
        with self._lock:
            self._events.append(event)

    def _log(self, msg: str) -> None:
        with self._lock:
            self._logs.append(time.strftime("[%H:%M:%S] ") + msg)

    def _toast(self, kind: str, title: str, text: str = "") -> None:
        self._emit(type="toast", kind=kind, title=title, text=text)

    def _detect_gpus(self) -> None:
        try:
            gpus, raw = tools.detect_gpus()
            self._gpus = [{"id": str(i), "name": n} for i, n in gpus]
            if gpus:
                self._log("Carte(s) graphique(s) : " + ", ".join(n for _, n in gpus))
            else:
                self._gpu_error = raw[-600:]
                self._log("Aucune carte graphique Vulkan détectée.\n" + raw[-600:])
                self._toast("warn", "Aucune carte graphique compatible détectée",
                            "Mettez à jour les pilotes de votre carte graphique (NVIDIA / AMD / Intel).")
        except Exception as e:  # noqa: BLE001
            self._gpus = []
            self._gpu_error = str(e)
            self._log(f"Détection GPU impossible : {e}")
        self._emit(type="gpus", gpus=self._gpus)

    def _file_public(self, f: dict, with_thumb: bool = False) -> dict:
        d = {k: f[k] for k in ("id", "name", "path", "status", "state", "progress", "res", "dur",
                               "out", "size", "output", "error", "kind", "fps")}
        job = f.get("job")
        d["frozen"] = job.data.get("summary", "") if job else ""
        d["resumable"] = bool(job and (job.data.get("state") or {}).get("src_done"))
        # nettoyage automatique détecté (affiché seulement s'il sera réellement appliqué)
        cl = f.get("clean")
        mode = job.data["settings"].get("clean", "off") if job else self._settings.clean
        d["clean"] = (f"Compression détectée · nettoyage {cl.level}"
                      if cl is not None and cl.filter and mode == "auto" else "")
        if with_thumb:
            d["thumb"] = f.get("thumb", "")
        return d

    def _compute_out(self, f: dict) -> None:
        info = f.get("info")
        if not info:
            return
        # un fichier déjà lancé garde les réglages figés au moment du clic
        settings = f["job"].settings if f.get("job") else self._settings
        try:
            plan = engine.make_plan(info, settings)
            f["out"] = f"{plan.out_w}×{plan.out_h}"
            if plan.interp > 1:
                f["out"] += f" · {round(float(Fraction(plan.out_fps)), 2):g} i/s".replace(".", ",")
        except UpscaleError:
            f["out"] = "déjà ≥" if settings.output_size != "x1" else "inchangé"

    def _add_paths(self, paths) -> list:
        added = []
        with self._lock:
            known = {f["path"] for f in self._files.values()}
            expanded = []
            for p in paths or []:
                if not p:
                    continue
                p = os.path.normpath(str(p))
                if os.path.isdir(p):
                    for name in sorted(os.listdir(p)):
                        if name.lower().endswith(MEDIA_EXT):
                            expanded.append(os.path.join(p, name))
                elif os.path.isfile(p):
                    expanded.append(p)
            for p in expanded:
                if p in known:
                    continue
                known.add(p)
                fid = self._next_id
                self._next_id += 1
                try:
                    size = os.path.getsize(p)
                except OSError:
                    size = 0
                f = {"id": fid, "name": os.path.basename(p), "path": p, "status": "Analyse…",
                     "state": "probing", "progress": 0.0, "res": "", "dur": "", "out": "",
                     "size": size, "output": "", "error": "", "thumb": "", "info": None,
                     "kind": "image" if p.lower().endswith(IMAGE_EXT) else "video", "fps": 0,
                     "job": None}
                self._files[fid] = f
                self._order.append(fid)
                added.append(self._file_public(f))
                threading.Thread(target=self._probe, args=(fid,), daemon=True).start()
        if added:
            self._emit(type="files_added", files=added)
        return added

    def _probe(self, fid: int) -> None:
        f = self._files.get(fid)
        if not f:
            return
        try:
            info = engine.probe(f["path"])
            with self._lock:
                f["info"] = info
                f["res"] = f"{info.width}×{info.height}"
                f["kind"] = "image" if info.is_image else "video"
                f["fps"] = 0 if info.is_image else round(info.fps_float, 3)
                f["dur"] = "Image" if info.is_image else fmt_time(info.duration)
                if not f.get("job"):
                    f["status"] = "Prêt"
                    f["state"] = "ready"
                self._compute_out(f)
            try:
                f["thumb"] = _thumbnail(f["path"], info.duration)
            except Exception:
                f["thumb"] = ""
            self._emit(type="thumb", id=fid, thumb=f["thumb"])
            try:  # analyse des défauts de compression (quelques images, en arrière-plan)
                f["clean"] = engine.detect_compression(info)
            except Exception:  # noqa: BLE001
                f["clean"] = None
        except Exception as e:  # noqa: BLE001
            with self._lock:
                job = f.get("job")
                if job and f["state"] in ("running", "queued") and self._running:
                    return  # le moteur signalera lui-même l'erreur
                f["status"] = "Illisible"
                f["state"] = "error"
                f["error"] = str(e)
                if job:
                    if not os.path.exists(f["path"]):
                        f["error"] = ("Le fichier source est introuvable (déplacé ou supprimé) : "
                                      "le traitement en pause ne peut pas être repris.\n" + f["path"])
                    f["job"] = None
                    self._jobs.delete(job)
            self._log(f"Impossible de lire {f['name']} : {e}")

    # ------------------------------------------------------------------ API JS
    def init(self) -> dict:
        models = []
        for m in MODELS:
            short, sub = MODEL_SHORT.get(m.key, (m.label, m.description))
            meta = MODEL_META.get(m.key, {})
            models.append({"key": m.key, "name": short, "sub": sub, "description": m.description,
                           "scales": list(m.scales), **meta})
        with self._lock:
            files = [self._file_public(self._files[i], True) for i in self._order]
        return {
            "version": __version__,
            "models": models,
            "sizes": [{"key": k, "label": lbl} for k, lbl in OUTPUT_SIZES],
            "encoders": [{"key": e.key, "label": e.label} for e in ENCODERS],
            "speeds": [{"key": k, "label": lbl} for k, lbl in SPEEDS],
            "interpolations": [{"key": k, "label": lbl} for k, lbl in INTERPOLATIONS],
            "image_formats": [{"key": k, "label": lbl} for k, lbl in IMAGE_FORMATS],
            "finishes": [{"key": k, "label": lbl, "values": v} for k, lbl, v in engine.FINISH_PRESETS],
            "settings": self._settings.to_dict(),
            "gpus": self._gpus,
            "files": files,
            "running": self._running,
            "pending": self._pending_count(),
            "restored": self._restored,
            "install_mode": updater.install_mode(),
        }

    def poll(self) -> dict:
        with self._lock:
            events, self._events = self._events, []
            logs, self._logs = self._logs, []
            files = [self._file_public(self._files[i]) for i in self._order]
            return {"events": events, "logs": logs, "files": files, "running": self._running,
                    "task": self._task, "progress": self._progress, "pending": self._pending_count(),
                    "pausing": self._running and self._pause_req}

    def pick_files(self) -> list:
        import webview
        types = ("Vidéos et images (" + ";".join("*" + e for e in MEDIA_EXT) + ")",
                 "Vidéos (" + ";".join("*" + e for e in VIDEO_EXT) + ")",
                 "Images (" + ";".join("*" + e for e in IMAGE_EXT) + ")",
                 "Tous les fichiers (*.*)")
        try:
            paths = self._window.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=True,
                                                    file_types=types)
        except Exception as e:  # noqa: BLE001
            self._log(f"Boîte de dialogue : {e}")
            paths = None
        return self._add_paths(paths or [])

    def add_paths(self, paths) -> list:
        return self._add_paths(paths)

    def pick_folder(self, key: str) -> str:
        import webview
        try:
            res = self._window.create_file_dialog(webview.FileDialog.FOLDER)
        except Exception:
            res = None
        if not res:
            return ""
        folder = os.path.normpath(res[0] if isinstance(res, (list, tuple)) else res)
        if key in ("output_dir", "temp_dir"):
            self.set_settings({key: folder})
        return folder

    def remove_file(self, fid) -> bool:
        with self._lock:
            fid = int(fid)
            f = self._files.get(fid)
            if not f or f["state"] == "running":
                return False
            del self._files[fid]
            self._order.remove(fid)
            if f.get("job"):
                self._jobs.delete(f["job"])
        return True

    def clear_files(self) -> bool:
        with self._lock:
            if self._running:
                return False
            for f in self._files.values():
                if f.get("job"):
                    self._jobs.delete(f["job"])
            self._files.clear()
            self._order.clear()
        return True

    def set_settings(self, values: dict) -> dict:
        with self._lock:
            data = self._settings.to_dict()
            data.update(values or {})
            self._settings = Settings.from_dict(data)
            for f in self._files.values():
                self._compute_out(f)
        self._save_settings()
        return self._settings.to_dict()

    def start(self) -> bool:
        """Lance (ou complète) la file d'attente.

        Les fichiers prêts deviennent des traitements avec un INSTANTANÉ des réglages actuels :
        modifier les réglages ensuite ne change rien pour eux (même après un redémarrage).
        Les traitements en pause sont repris là où ils s'étaient arrêtés."""
        with self._lock:
            if self._running and self._task != "queue":
                return False
            if self._running and self._pause_req:
                self._toast("info", "Mise en pause en cours", "Patientez une seconde puis réessayez.")
                return False
            new = [fid for fid in self._order
                   if self._files[fid]["state"] in ("ready", "error", "cancelled")
                   and self._files[fid].get("info") and not self._files[fid].get("job")]
            if not new and not self._pending_count():
                if self._running:
                    self._toast("info", "Déjà en file", "Ajoutez d'autres fichiers pour les mettre en file.")
                else:
                    self._toast("info", "Rien à traiter", "Ajoutez une vidéo ou une image (ou retirez celles déjà terminées).")
                return False
            snapshot = Settings.from_dict(self._settings.to_dict())  # réglages figés à cet instant
            reserved = {f["job"].output for f in self._files.values() if f.get("job")}
            added = 0
            for fid in new:
                f = self._files[fid]
                try:
                    plan = engine.make_plan(f["info"], snapshot)
                    out = _unique_path(engine.default_output_path(f["info"], snapshot, plan), reserved)
                except UpscaleError as e:
                    f.update(state="error", status="Erreur", error=str(e))
                    continue
                reserved.add(out)
                job = self._jobs.create(f["path"], out, snapshot, self._summary(snapshot, f["info"]))
                f.update(job=job, state="queued", status="En file", progress=0.0, error="", output="")
                self._compute_out(f)
                added += 1
            if self._running:
                if added:
                    self._toast("info", f"{added} fichier(s) ajouté(s) à la file",
                                "Ils seront traités avec les réglages actuels, figés dès maintenant.")
                return bool(added)
            if not self._pending_count():
                return False
            self._launch_runner()
        return True

    def resume(self) -> bool:
        """Reprend les traitements en pause / interrompus (sans ajouter de nouveaux fichiers)."""
        with self._lock:
            if self._running or not self._pending_count():
                return False
            self._launch_runner()
        return True

    def pause(self) -> bool:
        """Met en pause : l'avancement est enregistré sur le disque ; on peut alors quitter
        l'application ou éteindre le PC, la reprise se fera à l'image près."""
        with self._lock:
            if not (self._running and self._task == "queue") or self._pause_req:
                return False
            self._pause_req = True
            up = self._upscaler
        if up:
            up.pause()
        self._log("⏸ Mise en pause demandée… (enregistrement de l'avancement)")
        return True

    def cancel(self) -> bool:
        with self._lock:
            if self._running and self._task == "queue":
                self._cancel_req = True
                up = self._upscaler
            elif not self._running and self._pending_count():
                n = 0
                for f in self._files.values():
                    if f.get("job") and f["state"] in ("queued", "paused"):
                        self._jobs.delete(f["job"])
                        f.update(job=None, state="cancelled", status="Annulé", progress=0.0)
                        self._compute_out(f)
                        n += 1
                self._progress = None
                self._log(f"Traitement(s) en pause abandonné(s) : {n}")
                self._toast("info", "Traitement abandonné", "L'avancement enregistré a été supprimé.")
                return True
            else:
                return False
        if up:
            up.cancel()
        self._log("Annulation demandée…")
        return True

    def preview(self, fid=None, index=None) -> bool:
        with self._lock:
            if self._running:
                return False
            ids = [i for i in self._order if self._files[i].get("info")]
            if fid is not None and int(fid) in self._files:
                target = int(fid)
            elif ids:
                target = ids[0]
            else:
                self._toast("info", "Aucune vidéo", "Ajoutez une vidéo pour générer un aperçu.")
                return False
            self._running = True
            self._task = "preview"
            self._progress = None
            settings = Settings.from_dict(self._settings.to_dict())
        threading.Thread(target=self._run_preview, args=(target, settings, index),
                         daemon=True).start()
        return True

    def open_output(self, fid=None) -> bool:
        target = ""
        if fid is not None:
            f = self._files.get(int(fid))
            if f and f.get("output") and os.path.exists(f["output"]):
                if tools.IS_WIN:
                    subprocess.Popen(["explorer", "/select,", f["output"]])
                    return True
                target = os.path.dirname(f["output"])
        target = target or self._last_output_dir or self._settings.output_dir
        if not target and self._order:
            target = os.path.dirname(self._files[self._order[0]]["path"])
        if target and os.path.isdir(target):
            tools.open_path(target)
            return True
        return False

    def play(self, fid) -> bool:
        f = self._files.get(int(fid))
        if f and f.get("output") and os.path.exists(f["output"]):
            tools.open_path(f["output"])
            return True
        return False

    def open_url(self, url: str) -> None:
        import webbrowser
        if str(url).startswith("https://github.com/") or str(url).startswith("https://developer.microsoft.com/"):
            webbrowser.open(url)

    # ---- choix de l'image d'aperçu --------------------------------------------
    @staticmethod
    def _frame_time(info, index: int) -> float:
        """Temps de recherche qui tombe exactement sur l'image n° index."""
        return max(0.0, (index - 0.5) / info.fps_float) if index > 0 else 0.0

    def frame_info(self, fid) -> dict:
        f = self._files.get(int(fid))
        if not f or not f.get("info"):
            return {}
        info = f["info"]
        return {"id": f["id"], "name": f["name"], "fps": info.fps_float, "frames": max(1, info.frames),
                "duration": info.duration, "width": info.width, "height": info.height,
                "res": f["res"], "out": f["out"], "kind": "image" if info.is_image else "video"}

    def get_frame(self, fid, index, width=1280) -> dict:
        f = self._files.get(int(fid))
        if not f or not f.get("info"):
            return {}
        info = f["info"]
        index = max(0, min(int(index), max(0, info.frames - 1)))
        t = self._frame_time(info, index)
        try:
            data = engine.grab_frame_jpeg(f["path"], t, int(width))
        except Exception as e:  # noqa: BLE001
            return {"error": str(e), "index": index}
        return {"index": index, "time": fmt_time_ms(index / info.fps_float),
                "data": "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")}

    def get_filmstrip(self, fid, count=12) -> list:
        f = self._files.get(int(fid))
        if not f or not f.get("info"):
            return []
        info = f["info"]
        count = max(2, min(int(count), 24))
        frames = max(1, info.frames)
        indexes = [int((i + 0.5) * frames / count) for i in range(count)]

        def grab(idx):
            try:
                data = engine.grab_frame_jpeg(f["path"], self._frame_time(info, idx), 200, 6)
                return {"index": idx, "data": "data:image/jpeg;base64,"
                        + base64.b64encode(data).decode("ascii")}
            except Exception:  # noqa: BLE001
                return {"index": idx, "data": ""}

        return list(self._frame_pool.map(grab, indexes))

    # ---- mises à jour ----------------------------------------------------------
    def check_update(self, manual=False) -> dict:
        try:
            info = updater.check()
        except Exception as e:  # noqa: BLE001
            self._log(f"Vérification des mises à jour impossible : {e}")
            if manual:
                self._toast("warn", "Vérification impossible",
                            "Pas de connexion à GitHub. Réessayez plus tard.")
            return {"status": "error", "error": str(e)}
        if info is None:
            if manual:
                self._toast("success", "Vous avez la dernière version",
                            f"Upscale Video AI {__version__} est à jour.")
            return {"status": "uptodate", "current": __version__}
        self._update = info
        self._log(f"Nouvelle version disponible : {info['version']} (actuelle : {__version__})")
        self._emit(type="update", **info)
        return {"status": "available", **info}

    def install_update(self) -> bool:
        info = self._update
        if not info or self._updating:
            return False
        if self._running:
            self._toast("info", "Traitement en cours",
                        "La mise à jour pourra être installée à la fin du traitement.")
            return False
        if not info.get("can_install"):
            self.open_url(info.get("page", ""))
            return False
        self._updating = True
        threading.Thread(target=self._do_update, args=(info,), daemon=True).start()
        return True

    def _do_update(self, info: dict) -> None:
        def progress(done, total):
            self._emit(type="update_progress", done=done, total=total,
                       fraction=(done / total) if total else 0)
        try:
            self._log(f"Téléchargement de la version {info['version']}…")
            plan = updater.prepare(info, progress)
            self._emit(type="update_ready", version=info["version"])
            self._log("Installation de la mise à jour et redémarrage…")
            self._save_settings()
            time.sleep(1.2)
            updater.apply_and_restart(plan)
            time.sleep(0.3)
            os._exit(0)
        except Exception as e:  # noqa: BLE001
            self._updating = False
            self._log(f"✖ Mise à jour : {e}")
            self._emit(type="update_failed", error=str(e), page=info.get("page", ""))

    def _startup_tasks(self) -> None:
        try:
            updater.cleanup_old_versions()
        except Exception:
            pass
        self._cleanup()
        self._auto_resume()
        if self._settings.auto_update:
            time.sleep(4)
            self.check_update(manual=False)

    # ------------------------------------------------------------------ travail
    def _on_progress(self, fid: int, index: int, count: int):
        def cb(p: engine.Progress):
            frac = p.done / p.total if p.total else 0.0
            with self._lock:
                f = self._files.get(fid)
                if f:
                    f["progress"] = frac
                    f["status"] = f"{frac * 100:.0f} %"
                self._progress = {"done": p.done, "total": p.total, "speed": round(p.speed, 2),
                                  "eta": fmt_time(p.eta), "elapsed": fmt_time(p.elapsed),
                                  "fraction": frac, "stage": p.stage, "index": index,
                                  "count": count, "name": f["name"] if f else ""}
        return cb

    def _pending_count(self) -> int:
        return sum(1 for f in self._files.values()
                   if f.get("job") and f["state"] in ("queued", "paused", "running"))

    def _summary(self, st: Settings, info) -> str:
        """Résumé lisible des réglages figés, affiché sur la carte du fichier."""
        parts = [MODEL_SHORT.get(st.model, (st.model,))[0]]
        sizes = {"x1": "taille d'origine", "720p": "720p", "1080p": "1080p", "1440p": "1440p (2K)",
                 "2160p": "4K", "4320p": "8K"}
        parts.append(sizes.get(st.output_size, st.output_size))
        if info is not None and info.is_image:
            fmt = dict(IMAGE_FORMATS).get(st.image_format, st.image_format)
            parts.append("format " + (fmt.lower() if st.image_format == "auto" else st.image_format.upper()))
        else:
            if st.interpolation in ("x2", "x3", "x4"):
                parts.append(f"fluidité {st.interpolation}")
            elif st.interpolation == engine.INTERP_CUSTOM:
                parts.append(f"fluidité {st.interp_fps:g} i/s".replace(".", ","))
            enc = engine.ENCODERS_BY_KEY.get(st.encoder)
            codec = "H.265" if enc and enc.codec == "hevc" else "H.264"
            parts.append(f"{codec} {st.container.upper()} · CRF {st.quality}")
        if engine.finish_active(st):
            parts.append("finitions " + engine.finish_label(st))
        return " · ".join(parts)

    def _restore_jobs(self) -> None:
        """Recharge les traitements en pause ou interrompus (fermeture, plantage, arrêt du PC)."""
        try:
            pending = self._jobs.load_pending()
        except Exception as e:  # noqa: BLE001
            self._log(f"Reprise impossible : {e}")
            return
        for job in pending:
            fid = self._next_id
            self._next_id += 1
            try:
                size = os.path.getsize(job.path)
            except OSError:
                size = 0
            prog = float(job.data.get("progress") or 0.0)
            f = {"id": fid, "name": os.path.basename(job.path), "path": job.path,
                 "status": f"En pause · {prog * 100:.0f} %" if prog else "En file (en pause)",
                 "state": "paused", "progress": prog, "res": "", "dur": "", "out": "", "size": size,
                 "output": "", "error": "", "thumb": "", "info": None,
                 "kind": "image" if job.path.lower().endswith(IMAGE_EXT) else "video", "fps": 0,
                 "job": job}
            if job.status == "running":
                self._jobs.update(job, status="paused")
            self._files[fid] = f
            self._order.append(fid)
            self._restored += 1
            threading.Thread(target=self._probe, args=(fid,), daemon=True).start()
        if pending:
            self._log(f"{len(pending)} traitement(s) en pause retrouvé(s) : reprise automatique.")

    def _cleanup(self) -> None:
        """Supprime les fichiers temporaires laissés par une session interrompue."""
        try:
            with self._lock:
                temp_dirs = {self._settings.temp_dir} | {
                    f["job"].data["settings"].get("temp_dir", "") for f in self._files.values()
                    if f.get("job")}
            cleanup.startup_cleanup(temp_dirs, self._jobs.known_ids, self._log)
        except Exception as e:  # noqa: BLE001
            self._log(f"Nettoyage : {e}")

    def _auto_resume(self) -> None:
        if not self._restored:
            return
        deadline = time.monotonic() + 15  # laisse l'analyse des fichiers se terminer
        while time.monotonic() < deadline and any(
                f["state"] == "probing" or (f.get("job") and not f.get("info"))
                for f in list(self._files.values()) if f.get("job")):
            time.sleep(0.2)
        time.sleep(1.0)
        with self._lock:
            if self._running or not self._pending_count():
                return
            n = self._pending_count()
            self._launch_runner()
        self._toast("info", "Reprise automatique",
                    f"{n} traitement(s) reprennent exactement là où ils s'étaient arrêtés.")

    def _launch_runner(self) -> None:
        """À appeler sous self._lock."""
        self._running = True
        self._task = "queue"
        self._progress = None
        self._pause_req = False
        self._cancel_req = False
        for f in self._files.values():
            if f.get("job") and f["state"] == "paused":
                f.update(state="queued", status="En file")
        tools.keep_awake(True)
        threading.Thread(target=self._run_jobs, daemon=True).start()

    def _run_jobs(self) -> None:
        ok = failed = handled = 0
        t0 = time.monotonic()
        cancelled = paused = False
        try:
            while True:
                with self._lock:
                    if self._cancel_req:
                        cancelled = True
                        break
                    if self._pause_req:
                        paused = True
                        break
                    todo = [fid for fid in self._order
                            if self._files[fid].get("job") and self._files[fid]["state"] == "queued"]
                    if not todo:
                        break
                    fid = todo[0]
                    f = self._files[fid]
                    job = f["job"]
                    handled += 1
                    index, count = handled, handled - 1 + len(todo)
                    resuming = bool((job.data.get("state") or {}).get("src_done"))
                    f.update(state="running", status="Reprise…" if resuming else "Démarrage…", error="")
                    self._jobs.update(job, status="running")
                    up = Upscaler(on_log=self._log, on_progress=self._on_progress(fid, index, count))
                    self._upscaler = up
                self._log(f"──── [{index}/{count}] {f['name']}")
                self._log("Réglages figés : " + job.data.get("summary", ""))
                if resuming:
                    self._log(f"↻ Reprise à l'image {job.data['state']['src_done']} (avancement enregistré).")

                def on_state(st, job=job, f=f):
                    self._jobs.update(job, state=copy.deepcopy(st), progress=f.get("progress", 0.0))

                try:
                    out = up.process(job.path, job.settings, job.output, job_dir=job.work,
                                     state=copy.deepcopy(job.data.get("state") or {}), on_state=on_state)
                    ok += 1
                    with self._lock:
                        f.update(state="done", status="Terminé", progress=1.0, output=out, job=None)
                        self._jobs.delete(job)
                    self._last_output_dir = os.path.dirname(out)
                    self._log(f"✔ Terminé : {out}")
                except engine.Paused:
                    paused = True
                    with self._lock:
                        pct = f.get("progress", 0.0) * 100
                        f.update(state="paused", status=f"En pause · {pct:.0f} %")
                        self._jobs.update(job, status="paused", progress=f.get("progress", 0.0))
                    self._log("⏸ En pause. Vous pouvez fermer l'application ou éteindre le PC : "
                              "le traitement reprendra automatiquement à la même image.")
                    break
                except Cancelled:
                    cancelled = True
                    with self._lock:
                        f.update(state="cancelled", status="Annulé", progress=0.0, job=None)
                        self._jobs.delete(job)
                        self._compute_out(f)
                    self._log("Traitement annulé.")
                except Exception as e:  # noqa: BLE001
                    failed += 1
                    with self._lock:
                        f.update(state="error", status="Erreur", error=str(e), job=None)
                        self._jobs.delete(job)
                        self._compute_out(f)
                    if isinstance(e, UpscaleError):
                        self._log(f"✖ {f['name']} : {e}")
                    else:
                        self._log(f"✖ Erreur inattendue : {e}\n{traceback.format_exc()}")
            with self._lock:
                for f in self._files.values():
                    if f.get("job") and f["state"] == "queued":
                        if cancelled:  # « Arrêter » vide aussi la file
                            self._jobs.delete(f["job"])
                            f.update(job=None, state="ready", status="Prêt", progress=0.0)
                            self._compute_out(f)
                        elif paused:
                            f.update(state="paused", status="En file (en pause)")
                            self._jobs.update(f["job"], status="paused")
        finally:
            with self._lock:
                self._running = False
                self._task = ""
                self._upscaler = None
                self._pause_req = self._cancel_req = False
                if paused and self._progress:
                    self._progress = dict(self._progress, paused=True)
            tools.keep_awake(False)
            elapsed = fmt_time(time.monotonic() - t0)
            self._emit(type="queue_done", ok=ok, failed=failed, cancelled=cancelled, paused=paused,
                       elapsed=elapsed)
            if paused:
                self._toast("info", "Traitement en pause",
                            "Avancement enregistré : vous pouvez quitter ou éteindre le PC, "
                            "la reprise se fera automatiquement à la même image.")
            elif cancelled:
                self._toast("info", "Traitement annulé")
            elif failed:
                self._toast("error", f"{failed} fichier(s) en erreur",
                            "Ouvrez le journal pour voir le détail.")
            elif ok:
                self._toast("success", f"{ok} fichier(s) terminé(s) en {elapsed}",
                            "Vos fichiers améliorés sont prêts.")

    def _on_closing(self):
        """Fermeture de la fenêtre pendant un traitement : mise en pause propre d'abord."""
        if self._closing_ok or not (self._running and self._task == "queue"):
            if self._running and self._upscaler:
                self._upscaler.cancel()  # aperçu en cours
            return None
        self._emit(type="closing")
        threading.Thread(target=self._pause_then_close, daemon=True).start()
        return False  # annule la fermeture pour l'instant

    def _pause_then_close(self) -> None:
        self.pause()
        deadline = time.monotonic() + 60
        while self._running and time.monotonic() < deadline:
            time.sleep(0.1)
        self._closing_ok = True
        try:
            self._window.destroy()
        except Exception:  # noqa: BLE001
            os._exit(0)

    def _run_preview(self, fid: int, settings: Settings, index=None) -> None:
        f = self._files.get(fid)
        try:
            self._upscaler = Upscaler(on_log=self._log)
            info = f["info"]
            if index is None:
                index = int(info.frames * 0.5)
            index = max(0, min(int(index), max(0, info.frames - 1)))
            t = self._frame_time(info, index)
            label = "" if info.is_image else fmt_time_ms(index / info.fps_float)
            self._emit(type="preview_started", id=fid, name=f["name"], index=index, time=label)
            cmp_, ai = self._upscaler.preview(f["path"], settings, time_s=t)
            plan = engine.make_plan(info, settings)
            w, h = plan.out_w, plan.out_h
            if w > 3840:  # limite d'affichage
                h = int(round(h * 3840 / w / 2)) * 2
                w = 3840
            src = cmp_.replace("_comparaison.png", "_source.png")
            before = _jpeg_data_uri(src, w, h, "bicubic")
            after = _jpeg_data_uri(ai, w, h, "lanczos")
            self._emit(type="preview_done", id=fid, before=before, after=after, width=w,
                       height=h, name=f["name"], source=f["res"], output=f"{plan.out_w}×{plan.out_h}",
                       model=settings.model, index=index, time=label,
                       finish=engine.finish_label(settings) if engine.finish_active(settings) else "",
                       clean=engine.clean_for(info, settings).level)
        except Cancelled:
            self._emit(type="preview_failed", error="Aperçu annulé.")
        except Exception as e:  # noqa: BLE001
            self._log(f"✖ Aperçu : {e}")
            self._emit(type="preview_failed", error=str(e))
        finally:
            with self._lock:
                self._running = False
                self._task = ""
                self._upscaler = None


def _unique_path(path: str, taken: set) -> str:
    """Évite que deux traitements en file visent le même fichier de sortie."""
    if path not in taken:
        return path
    base, ext = os.path.splitext(path)
    n = 2
    while f"{base} ({n}){ext}" in taken or os.path.exists(f"{base} ({n}){ext}"):
        n += 1
    return f"{base} ({n}){ext}"


def _on_drop(api: Api):
    def handler(event):
        try:
            files = event.get("dataTransfer", {}).get("files", [])
            paths = [f.get("pywebviewFullPath") for f in files if f.get("pywebviewFullPath")]
            api._add_paths(paths)
        except Exception as e:  # noqa: BLE001
            api._log(f"Glisser-déposer : {e}")
    return handler


def _force_dark_titlebar(window) -> None:
    if not tools.IS_WIN:
        return
    try:
        import ctypes
        hwnd = window.native.Handle.ToInt32()
        val = ctypes.c_int(1)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (Win11 / Win10 20H1)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(val), 4)
        color = ctypes.c_int(0x00140B0B)  # légende : #0b0b14 (format BGR)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(color), 4)
    except Exception:
        pass


def ui_index() -> str:
    return os.path.join(tools.resource_root(), "ui", "index.html")


def run(selftest_report: str | None = None) -> int:
    import webview
    from webview.dom import DOMEventHandler

    api = Api()
    url = ui_index()
    if os.environ.get("UPSCALER_DEMO"):  # captures d'écran : données fictives
        url += "?demo=" + os.environ["UPSCALER_DEMO"]
    window = webview.create_window(
        f"Upscale Video AI {__version__}", url=url, js_api=api,
        width=1400, height=880, min_size=(1100, 720), background_color="#0b0b14")
    api._window = window
    result = {"ok": False, "detail": ""}

    def on_start():
        _force_dark_titlebar(window)
        if not selftest_report:
            threading.Thread(target=api._startup_tasks, daemon=True).start()
        try:
            noop = lambda e: None  # noqa: E731
            doc = window.dom.document
            doc.events.dragenter += DOMEventHandler(noop, True, True)
            doc.events.dragstart += DOMEventHandler(noop, True, True)
            doc.events.dragover += DOMEventHandler(noop, True, True, debounce=500)
            doc.events.drop += DOMEventHandler(_on_drop(api), True, True)
        except Exception as e:  # noqa: BLE001
            api._log(f"Glisser-déposer indisponible : {e}")
        if selftest_report:
            for _ in range(60):
                try:
                    if window.evaluate_js("window.__appReady === true"):
                        result["ok"] = True
                        result["detail"] = str(window.evaluate_js(
                            "document.querySelectorAll('.model-card').length + ' modèles affichés'"))
                        break
                except Exception as e:  # noqa: BLE001
                    result["detail"] = str(e)
                time.sleep(0.5)
            window.destroy()

    window.events.shown += lambda: _force_dark_titlebar(window)
    window.events.closing += api._on_closing
    kwargs = {}
    if tools.IS_WIN:
        kwargs["gui"] = "edgechromium"
    webview.start(on_start, debug=bool(os.environ.get("UPSCALER_DEBUG")), **kwargs)
    if selftest_report:
        with open(selftest_report, "a", encoding="utf-8") as fh:
            fh.write(("OK   webui: " if result["ok"] else "FAIL webui: ") + result["detail"] + "\n")
        return 0 if result["ok"] else 1
    return 0
