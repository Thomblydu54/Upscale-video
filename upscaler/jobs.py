"""File d'attente persistante.

Chaque traitement (« job ») est enregistré sur le disque avec :
  * les réglages figés au moment du clic (jamais modifiés ensuite) ;
  * le chemin de sortie choisi au moment du clic ;
  * l'état du moteur (images déjà traitées, segments déjà encodés…).

Ainsi un traitement mis en pause — ou interrompu par la fermeture de l'application,
un plantage ou l'arrêt du PC — reprend exactement où il s'était arrêté, avec
exactement les mêmes réglages, même si ceux de l'interface ont changé entre-temps.
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid

from .engine import Settings

PENDING = ("queued", "running", "paused")


def data_dir() -> str:
    base = (os.environ.get("UPSCALER_DATA_DIR") or os.environ.get("LOCALAPPDATA")
            or os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".local", "share"))
    if not os.environ.get("UPSCALER_DATA_DIR"):
        base = os.path.join(base, "UpscaleVideoAI")
    return base


def jobs_root() -> str:
    path = os.path.join(data_dir(), "jobs")
    os.makedirs(path, exist_ok=True)
    return path


def _write_json(path: str, data: dict) -> None:
    """Écriture atomique : un arrêt brutal ne laisse jamais un fichier à moitié écrit."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass
    os.replace(tmp, path)


class Job:
    def __init__(self, data: dict):
        self.data = data

    # raccourcis
    id = property(lambda self: self.data["id"])
    path = property(lambda self: self.data["path"])
    output = property(lambda self: self.data["output"])
    status = property(lambda self: self.data.get("status", "queued"))
    work = property(lambda self: self.data["work"])

    @property
    def settings(self) -> Settings:
        """Copie neuve des réglages figés (ne partage rien avec l'interface)."""
        data = dict(self.data["settings"])
        data.setdefault("clean", "off")  # traitement créé avant l'option : réglages inchangés
        return Settings.from_dict(data)

    @property
    def folder(self) -> str:
        return os.path.join(jobs_root(), self.id)


class JobStore:
    def __init__(self):
        self._lock = threading.RLock()

    def create(self, path: str, output: str, settings: Settings, summary: str = "") -> Job:
        jid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        snap = settings.to_dict()  # instantané : copie indépendante des réglages du moment
        if snap.get("temp_dir"):
            work = os.path.join(snap["temp_dir"], "UpscaleVideoAI-reprise", jid)
        else:
            work = os.path.join(jobs_root(), jid, "travail")
        job = Job({"id": jid, "path": path, "name": os.path.basename(path), "output": output,
                   "settings": snap, "summary": summary, "status": "queued", "state": {},
                   "progress": 0.0, "work": work, "created": time.time()})
        os.makedirs(job.folder, exist_ok=True)
        self.save(job)
        return job

    def save(self, job: Job) -> None:
        with self._lock:
            try:
                os.makedirs(job.folder, exist_ok=True)
                _write_json(os.path.join(job.folder, "job.json"), job.data)
            except OSError:
                pass

    def update(self, job: Job, **values) -> None:
        with self._lock:
            job.data.update(values)
            self.save(job)

    def delete(self, job: Job) -> None:
        with self._lock:
            shutil.rmtree(job.work, ignore_errors=True)
            shutil.rmtree(job.folder, ignore_errors=True)
            try:  # vidéo finale à moitié assemblée (arrêt pendant l'assemblage)
                os.remove(job.output + ".partiel")
            except OSError:
                pass
            try:  # dossier parent « UpscaleVideoAI-reprise » vide
                os.rmdir(os.path.dirname(job.work))
            except OSError:
                pass

    def known_ids(self) -> set:
        try:
            return {n for n in os.listdir(jobs_root()) if os.path.isdir(os.path.join(jobs_root(), n))}
        except OSError:
            return set()

    def load_pending(self) -> list[Job]:
        """Traitements à reprendre, dans l'ordre où ils ont été lancés."""
        jobs = []
        root = jobs_root()
        for name in os.listdir(root):
            folder = os.path.join(root, name)
            if not os.path.isdir(folder):
                continue
            data = None
            for fp in (os.path.join(folder, "job.json"), os.path.join(folder, "job.json.tmp")):
                try:
                    with open(fp, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    break
                except (OSError, ValueError):
                    continue
            if data and data.get("status") in PENDING and data.get("path") and data.get("settings"):
                jobs.append(Job(data))
            else:  # traitement terminé / annulé, ou dossier orphelin
                if data and data.get("work") and data.get("output"):
                    self.delete(Job(dict(data, id=name)))
                shutil.rmtree(folder, ignore_errors=True)
        jobs.sort(key=lambda j: (j.data.get("created", 0), j.id))
        return jobs
