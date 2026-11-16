"""Processus d'interface du test de panne, tué sans appeler aucun hook de fermeture.

Utilise le vrai Api/dispatcher et le vrai service détaché installé ; seul le GPU,
sur l'autre pair du test, est remplacé par un moteur déterministe.
"""
from __future__ import annotations

import os
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from upscaler import engine
from upscaler.extension_runtime import atomic_json, read_json
from upscaler.webapp import Api


def run(folder):
    Api._detect_gpus = lambda self: None
    api = Api()
    if not api._remote:
        raise RuntimeError("Extension de test absente")
    api._remote.ensure_running()  # ce processus lance lui-même son service enfant
    atomic_json(folder / "ready.json", {"pid": os.getpid(), "service": read_json(api._remote.status_path)["pid"]})
    deadline = time.monotonic() + 60
    while not (folder / "launch.json").exists():
        if time.monotonic() >= deadline:
            raise RuntimeError("Lancement non autorisé par le test")
        time.sleep(0.05)
    task = read_json(folder / "launch.json")
    source = Path(task["source"])
    api._settings = engine.Settings.from_dict(task["settings"])
    info = engine.VideoInfo(str(source), 32, 24, "25/1", 25, 1, 25, None,
                            "progressive", "bt709", "yuv420p", "h264")
    file = {"id": 1, "name": source.name, "path": str(source), "status": "Prêt", "state": "ready",
            "progress": 0, "res": "32×24", "dur": "00:00:01", "out": "64×48", "size": source.stat().st_size,
            "output": "", "error": "", "thumb": "", "info": info, "kind": "video", "fps": 25, "job": None}
    api._files, api._order, api._next_id = {1: file}, [1], 2
    if not api.start():
        raise RuntimeError("La vraie file n'a pas démarré")
    atomic_json(folder / "running.json", {"job": file["job"].id, "settings": file["job"].data["settings"]})
    while True:
        time.sleep(0.1)  # le test fera kill(), sans close_application/pause/detach/finally


if __name__ == "__main__":
    folder = Path(sys.argv[1])
    try:
        run(folder)
    except BaseException:
        atomic_json(folder / "error.json", {"error": traceback.format_exc()})
        raise
