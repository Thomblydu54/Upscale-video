"""Exécutant GPU volontaire. Connexions sortantes uniquement ; aucun accès au bureau.

La source, les segments de reprise et le résultat restent dans UN dossier par traitement.
Aucun de ces fichiers n'est supprimé avant un reçu de livraison (ou un abandon explicite).
"""
from __future__ import annotations

import contextlib
import copy
import math
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path

from upscaler import __version__, engine, tools
from upscaler.extension_runtime import ExtensionError, atomic_json, read_json, sha256_file
from .common import (CHUNK, MAX_FILE, TERMINAL, VIDEO_EXT, Client, ProtocolError,
                     digest, filename, identifier, safe_settings)


class Worker:
    def __init__(self, folder, config, log=lambda s: None, factory=engine.Upscaler):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.config, self.log, self.factory = config, log, factory
        self.client = Client(config["url"], config["pin"], config["token"], timeout=6)
        self.stop = threading.Event()
        self.active = None
        self.status = "Connexion au PC principal…"
        self.gpus = []
        self.progress = {}
        self.last_log = ""
        self._lock = threading.RLock()

    def snapshot(self):
        return {"message": self.status, "active": self.active, "gpus": self.gpus,
                "progress": self.progress, "gpu": self.config.get("gpu", "auto")}

    def detect(self):
        try:
            gpus, _ = tools.detect_gpus()
            self.gpus = [{"id": str(i), "name": n} for i, n in gpus]
        except Exception:
            self.gpus = []

    def _log(self, text):
        self.last_log = str(text)[-1200:]
        self.log(text)

    def _report(self, job, status, progress=None, error=""):
        self.status = {"receiving": "Réception du média", "running": "Calcul sur ce PC",
                       "uploading": "Retour du résultat", "paused": "Traitement en pause",
                       "error": "Erreur de traitement"}.get(status, status)
        self.progress = progress or self.progress
        return self.client.post(f"/v1/jobs/{job['id']}/report", {
            "status": status, "progress": self.progress, "error": error, "log": self.last_log})

    def _check(self, job, status, progress=None):
        if self.stop.is_set():
            raise engine.Paused()
        remote = self._report(job, status, progress)
        if remote["status"] in TERMINAL or remote["desired"] in ("cancel", "cleanup"):
            raise engine.Cancelled()
        if remote["desired"] == "pause":
            raise engine.Paused()
        return remote

    def _cleanup(self, job):
        identifier(job.get("id", ""))
        if job.get("status") not in TERMINAL:
            raise ProtocolError("Aucun reçu ni abandon : nettoyage refusé.")
        folder = self.folder / job["id"]
        # Le serveur a reçu un SHA vérifié du contrôleur, ou un abandon explicite.
        if folder.exists():
            shutil.rmtree(folder)  # si un antivirus bloque, réessayer au prochain passage
        self.client.post(f"/v1/jobs/{job['id']}/cleaned")
        self.status = "Fichiers de travail supprimés · prêt"
        self.log("Fichiers distants supprimés après reçu de livraison / abandon : " + job["id"])

    def step(self):
        self.client.post("/v1/worker/ping", {"version": __version__, "gpus": self.gpus,
                                           "gpu": self.config.get("gpu", "auto"), "name": self.config.get("name", "PC de calcul")})
        jobs = self.client.get("/v1/worker/jobs")["jobs"]
        for job in jobs:
            if job["status"] in TERMINAL:
                self._cleanup(job)
        for summary in jobs:
            identifier(summary.get("id", ""))
            if summary["status"] in (*TERMINAL, "ready", "error") or summary["desired"] != "run":
                continue
            job = self.client.get(f"/v1/jobs/{summary['id']}")
            self.run_one(job)
            return
        self.active = None
        self.status = "Appairé · en attente" if not jobs else "En attente d'un travail / reçu de livraison"

    def run(self):
        threading.Thread(target=self.detect, daemon=True).start()
        delay = 1
        while not self.stop.is_set():
            try:
                self.step()
                delay = 1
            except (OSError, ProtocolError) as e:
                self.status = "Liaison interrompue · nouvelle tentative automatique"
                if isinstance(e, ProtocolError) and e.status in (401, 403, 495):
                    self.status = str(e)
                self.log(self.status + " : " + str(e))
                delay = min(10, delay * 2)
            except Exception as e:
                self.status = "Service en attente : " + str(e)[:300]
                self.log(self.status)
                delay = min(10, delay * 2)
            self.stop.wait(delay)

    def run_one(self, job):
        # Défense aussi face à un relais compromis : jamais de chemin arbitraire du réseau.
        identifier(job.get("id", ""))
        if self.config.get("worker_id") and job.get("worker") != self.config["worker_id"]:
            raise ProtocolError("Ce travail appartient à un autre PC.")
        source_ext = Path(filename(job.get("source_name", ""))).suffix.lower()
        result_ext = Path(filename(job.get("result_name", ""))).suffix.lower()
        if source_ext not in VIDEO_EXT + engine.IMAGE_EXT or result_ext not in (".mp4", ".mkv", ".png", ".jpg", ".webp"):
            raise ProtocolError("Format de média non autorisé.")
        if not isinstance(job.get("source_size"), int) or not 0 < job["source_size"] <= MAX_FILE:
            raise ProtocolError("Taille de source invalide.")
        digest(job.get("source_sha256", ""))
        if job.get("kind") not in ("process", "preview"):
            raise ProtocolError("Type de travail non autorisé.")
        timestamp = float(job.get("time_s", 0))
        if not math.isfinite(timestamp) or not 0 <= timestamp <= 1e8:
            raise ProtocolError("Position d'aperçu invalide.")
        folder = self.folder / job["id"]
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        state_path = folder / "worker.json"
        state = read_json(state_path, {"checkpoint": {}})
        # Les noms du réseau ne sont jamais employés comme chemins de destination.
        source = folder / ("input-" + job["id"] + source_ext)
        result = folder / ("result" + result_ext)
        partial = folder / "input.part"
        self.active = {"id": job["id"], "name": job["source_name"]}
        self.progress = {}
        tools.keep_awake(True)
        try:
            if not source.exists():
                if partial.exists() and partial.stat().st_size > job["source_size"]:
                    partial.unlink()
                offset = partial.stat().st_size if partial.exists() else 0
                self._check(job, "receiving", {"done": offset, "total": job["source_size"], "stage": "Envoi vers le PC de calcul"})
                last = time.monotonic()
                with partial.open("ab") as f:
                    while offset < job["source_size"]:
                        if self.stop.is_set():
                            raise engine.Paused()
                        content = self.client.request("GET", f"/v1/jobs/{job['id']}/source?offset={offset}", raw=True)
                        if not content or offset + len(content) > job["source_size"]:
                            raise ProtocolError("Source tronquée.")
                        f.write(content)
                        f.flush()
                        os.fsync(f.fileno())
                        offset += len(content)
                        if time.monotonic() - last > 1:
                            self._check(job, "receiving", {"done": offset, "total": job["source_size"], "stage": "Envoi vers le PC de calcul"})
                            last = time.monotonic()
                self.status = "Vérification de la source (SHA-256)"
                if sha256_file(partial, check=lambda: self._local_check()) != job["source_sha256"]:
                    partial.unlink()
                    raise ExtensionError("La source reçue est corrompue. Relancez le travail.")
                os.replace(partial, source)
            if not result.exists() or state.get("phase") not in ("computed", "uploading"):
                self._compute(job, source, result, folder, state, state_path)
            self._upload(job, result, state, state_path)
        except engine.Paused:
            with contextlib.suppress(OSError, ProtocolError):
                self._report(job, "paused")
            self.status = "En pause · avancement enregistré"
        except engine.Cancelled:
            # Ne pas supprimer ici : attendre l'état terminal signé par le relais.
            self.status = "Abandon demandé · attente de confirmation"
        except (OSError, ProtocolError):
            raise  # perte de réseau/disque : on conserve les données, jamais de suppression implicite
        except Exception as e:
            self._report(job, "error", error=str(e))
            self.log("Erreur IA : " + str(e))
        finally:
            tools.keep_awake(False)
            self.active = None

    def _local_check(self):
        if self.stop.is_set():
            raise engine.Paused()

    def _compute(self, job, source, result, folder, state, state_path):
        settings = engine.Settings.from_dict(safe_settings(job["settings"], job["settings"].get("gpu", "auto")))
        # Les fichiers temporaires restent confinés au travail de l'extension.
        settings.temp_dir = str(folder / "temp")
        finished = threading.Event()
        progress_lock = threading.Lock()
        progress = {"done": 0, "total": 0, "stage": "Démarrage de l'IA distante"}
        def record_progress(p):
            values = {k: getattr(p, k) for k in ("done", "total", "speed", "eta", "elapsed", "stage")}
            for key, value in list(values.items()):
                if isinstance(value, (int, float)) and not math.isfinite(value):
                    values[key] = 0  # une ETA inconnue n'est pas un nombre JSON valide
            with progress_lock:
                progress.update(values)

        up = self.factory(on_log=self._log, on_progress=record_progress)

        def monitor():
            while not finished.wait(0.5):
                if self.stop.is_set():
                    up.pause()
                    return
                try:
                    with progress_lock:
                        snapshot = dict(progress)
                    remote = self._report(job, "running", snapshot)
                    self.client.post("/v1/worker/ping", {"version": __version__, "gpus": self.gpus,
                                                       "gpu": self.config.get("gpu", "auto"), "name": self.config.get("name", "PC de calcul")})
                    if remote["status"] in TERMINAL or remote["desired"] in ("cancel", "cleanup"):
                        up.cancel()
                        return
                    if remote["desired"] == "pause":
                        up.pause()
                        return
                except (OSError, ProtocolError):
                    # Une coupure du PC principal n'annule PAS un calcul GPU déjà lancé.
                    self.status = "Calcul en cours · PC principal momentanément inaccessible"

        def checkpoint(data):
            state["checkpoint"] = copy.deepcopy(data)
            atomic_json(state_path, state)

        self._check(job, "running", dict(progress))
        thread = threading.Thread(target=monitor, daemon=True)
        thread.start()
        try:
            if job["kind"] == "preview":
                cmp_, ai = up.preview(str(source), settings, time_s=job["time_s"])
                shutil.copyfile(ai, result)
                state["finish"] = getattr(up, "auto_finish_choice", "")
            else:
                up.process(str(source), settings, str(result), job_dir=str(folder / "resume"),
                           state=copy.deepcopy(state.get("checkpoint", {})), on_state=checkpoint)
            with result.open("r+b") as durable:
                os.fsync(durable.fileno())
            state["phase"] = "computed"
            atomic_json(state_path, state)
        finally:
            finished.set()
            thread.join(timeout=14)
            # Les aperçus de ce travail n'ont plus d'utilité. Aucun autre fichier n'est touché.
            if job["kind"] == "preview":
                preview_dir = Path(tempfile.gettempdir(), "UpscaleVideoAI_apercus")
                for path in preview_dir.glob(source.stem + "_*"):
                    with contextlib.suppress(OSError):
                        path.unlink()

    def _upload(self, job, result, state, state_path):
        if self.stop.is_set():
            raise engine.Paused()
        self.status = "Vérification du résultat (SHA-256)"
        size = result.stat().st_size
        sha = state.get("result_sha256")
        if not sha:
            sha = sha256_file(result, check=self._local_check)
            state.update(result_sha256=sha, phase="uploading")
            atomic_json(state_path, state)
        remote = self.client.post(f"/v1/jobs/{job['id']}/result", {
            "size": size, "sha256": sha, "finish": state.get("finish", "")})
        if remote["status"] in (*TERMINAL, "ready"):
            return
        offset = remote["offset"]
        last = time.monotonic()
        with result.open("rb") as f:
            f.seek(offset)
            while offset < size:
                if self.stop.is_set():
                    raise engine.Paused()
                content = f.read(CHUNK)
                if not content:
                    raise ExtensionError("Résultat local tronqué.")
                response = self.client.request("PUT", f"/v1/jobs/{job['id']}/result?offset={offset}", content, timeout=120)
                offset = response["offset"]
                if time.monotonic() - last > 1:
                    self._check(job, "uploading", {"done": offset, "total": size, "stage": "Renvoi du résultat"})
                    last = time.monotonic()
        self.client.post(f"/v1/jobs/{job['id']}/finish", timeout=1800)
        self.status = "Résultat envoyé · attente du reçu avant suppression"
