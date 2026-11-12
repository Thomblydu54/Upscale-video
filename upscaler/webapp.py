"""Interface moderne (HTML/CSS/JS) affichée dans une fenêtre native via pywebview (WebView2)."""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import threading
import time
import traceback

from concurrent.futures import ThreadPoolExecutor

from . import __version__, engine, tools, updater
from .engine import (ENCODERS, MODELS, OUTPUT_SIZES, SPEEDS, Cancelled, Settings, Upscaler,
                     UpscaleError, fmt_time, fmt_time_ms)

VIDEO_EXT = (".mp4", ".mkv", ".mov", ".avi", ".wmv", ".flv", ".webm", ".m4v", ".mpg",
             ".mpeg", ".ts", ".m2ts", ".mts", ".vob", ".3gp", ".ogv", ".divx", ".asf")
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
                      "-ss", f"{t:.2f}", "-i", path, "-frames:v", "1",
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
                               "out", "size", "output", "error")}
        if with_thumb:
            d["thumb"] = f.get("thumb", "")
        return d

    def _compute_out(self, f: dict) -> None:
        info = f.get("info")
        if not info:
            return
        try:
            plan = engine.make_plan(info, self._settings)
            f["out"] = f"{plan.out_w}×{plan.out_h}"
        except UpscaleError:
            f["out"] = "déjà ≥"

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
                        if name.lower().endswith(VIDEO_EXT):
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
                     "size": size, "output": "", "error": "", "thumb": "", "info": None}
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
                f["dur"] = fmt_time(info.duration)
                f["status"] = "Prêt"
                f["state"] = "ready"
                self._compute_out(f)
            try:
                f["thumb"] = _thumbnail(f["path"], info.duration)
            except Exception:
                f["thumb"] = ""
            self._emit(type="thumb", id=fid, thumb=f["thumb"])
        except Exception as e:  # noqa: BLE001
            with self._lock:
                f["status"] = "Illisible"
                f["state"] = "error"
                f["error"] = str(e)
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
            "settings": self._settings.to_dict(),
            "gpus": self._gpus,
            "files": files,
            "running": self._running,
            "install_mode": updater.install_mode(),
        }

    def poll(self) -> dict:
        with self._lock:
            events, self._events = self._events, []
            logs, self._logs = self._logs, []
            files = [self._file_public(self._files[i]) for i in self._order]
            return {"events": events, "logs": logs, "files": files, "running": self._running,
                    "task": self._task, "progress": self._progress}

    def pick_files(self) -> list:
        import webview
        types = ("Vidéos (" + ";".join("*" + e for e in VIDEO_EXT) + ")", "Tous les fichiers (*.*)")
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
        return True

    def clear_files(self) -> bool:
        with self._lock:
            if self._running:
                return False
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
        with self._lock:
            if self._running:
                return False
            todo = [fid for fid in self._order
                    if self._files[fid]["state"] in ("ready", "error", "cancelled")
                    and self._files[fid].get("info")]
            if not todo:
                self._toast("info", "Rien à traiter", "Ajoutez une vidéo (ou retirez celles déjà terminées).")
                return False
            self._running = True
            self._task = "queue"
            self._progress = None
            for fid in todo:
                self._files[fid].update(state="queued", status="En file", progress=0.0, error="")
            settings = Settings.from_dict(self._settings.to_dict())
        tools.keep_awake(True)
        threading.Thread(target=self._run_queue, args=(todo, settings), daemon=True).start()
        return True

    def cancel(self) -> bool:
        up = self._upscaler
        if up and self._running:
            up.cancel()
            self._log("Annulation demandée…")
            return True
        return False

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
                "res": f["res"], "out": f["out"]}

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

    def _run_queue(self, todo: list, settings: Settings) -> None:
        ok = failed = 0
        t0 = time.monotonic()
        cancelled = False
        try:
            for i, fid in enumerate(todo, 1):
                f = self._files.get(fid)
                if not f:
                    continue
                if cancelled:
                    f.update(state="ready", status="Prêt")
                    continue
                f.update(state="running", status="Démarrage…")
                self._log(f"──── [{i}/{len(todo)}] {f['name']}")
                self._upscaler = Upscaler(on_log=self._log, on_progress=self._on_progress(fid, i, len(todo)))
                try:
                    out = self._upscaler.process(f["path"], settings)
                    ok += 1
                    f.update(state="done", status="Terminé", progress=1.0, output=out)
                    self._last_output_dir = os.path.dirname(out)
                    self._log(f"✔ Terminé : {out}")
                except Cancelled:
                    cancelled = True
                    f.update(state="cancelled", status="Annulé", progress=0.0)
                    self._log("Traitement annulé.")
                except UpscaleError as e:
                    failed += 1
                    f.update(state="error", status="Erreur", error=str(e))
                    self._log(f"✖ {f['name']} : {e}")
                except Exception as e:  # noqa: BLE001
                    failed += 1
                    f.update(state="error", status="Erreur", error=str(e))
                    self._log(f"✖ Erreur inattendue : {e}\n{traceback.format_exc()}")
        finally:
            with self._lock:
                self._running = False
                self._task = ""
                self._upscaler = None
            tools.keep_awake(False)
            elapsed = fmt_time(time.monotonic() - t0)
            self._emit(type="queue_done", ok=ok, failed=failed, cancelled=cancelled, elapsed=elapsed)
            if cancelled:
                self._toast("info", "Traitement annulé")
            elif failed:
                self._toast("error", f"{failed} vidéo(s) en erreur",
                            "Ouvrez le journal pour voir le détail.")
            elif ok:
                self._toast("success", f"{ok} vidéo(s) terminée(s) en {elapsed}",
                            "Vos vidéos en haute résolution sont prêtes.")

    def _run_preview(self, fid: int, settings: Settings, index=None) -> None:
        f = self._files.get(fid)
        try:
            self._upscaler = Upscaler(on_log=self._log)
            info = f["info"]
            if index is None:
                index = int(info.frames * 0.5)
            index = max(0, min(int(index), max(0, info.frames - 1)))
            t = self._frame_time(info, index)
            label = fmt_time_ms(index / info.fps_float)
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
                       model=settings.model, index=index, time=label)
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
    kwargs = {}
    if tools.IS_WIN:
        kwargs["gui"] = "edgechromium"
    webview.start(on_start, debug=bool(os.environ.get("UPSCALER_DEBUG")), **kwargs)
    if selftest_report:
        with open(selftest_report, "a", encoding="utf-8") as fh:
            fh.write(("OK   webui: " if result["ok"] else "FAIL webui: ") + result["detail"] + "\n")
        return 0 if result["ok"] else 1
    return 0
