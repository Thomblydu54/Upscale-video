"""Adaptateur de file d'attente : mêmes réglages figés, destination et transfert persistants."""
from __future__ import annotations

import contextlib
import os
import threading
import time
from pathlib import Path

from upscaler import engine
from upscaler.extension_runtime import durable_replace, sha256_file
from .common import ProtocolError


class RemoteRunner:
    def __init__(self, plugin, target, on_log=lambda s: None, on_progress=lambda p: None,
                 kind="process", time_s=0):
        self.plugin, self.target = plugin, dict(target)
        self.log, self.progress = on_log, on_progress
        self.kind, self.time_s = kind, time_s
        self._pause, self._cancel = threading.Event(), threading.Event()
        self.auto_finish_choice = ""
        self._started = time.monotonic()
        self._client = None
        self._jid = ""
        self._state = {}
        self._save = lambda s: None
        self._last_saved = -1

    def pause(self):
        self.plugin.command(self.target["request_id"], "pause")
        self._pause.set()

    def cancel(self):
        self.plugin.command(self.target["request_id"], "cancel")
        self._cancel.set()

    def _check(self):
        desired = "cancel" if self._cancel.is_set() else "pause" if self._pause.is_set() else ""
        if desired:
            self.plugin.command(self.target["request_id"], desired)
            if self._client and self._jid:
                with contextlib.suppress(OSError, ProtocolError):
                    self._client.post(f"/v1/jobs/{self._jid}/control", {"desired": desired})
            if desired == "cancel":
                raise engine.Cancelled()
            self.log("Pause distante enregistrée. Le PC de calcul l'applique dès qu'il reçoit la demande.")
            raise engine.Paused()

    def _emit(self, stage, fraction, p=None, transfer=None):
        p = p or {}
        done, total = int(p.get("done", 0)), int(p.get("total", 0))
        progress = engine.Progress(done, total, float(p.get("speed", 0)), float(p.get("eta", 0)),
                                   time.monotonic() - self._started, stage=stage)
        progress.fraction = max(0, min(1, fraction))
        progress.remote = True
        progress.transfer = transfer
        self.progress(progress)
        if abs(fraction - self._last_saved) >= 0.01:
            self._last_saved = fraction
            self._state["remote_fraction"] = fraction
            self._save(dict(self._state))

    def process(self, path, settings, output, job_dir=None, state=None, on_state=None):
        self._state = dict(state or {})
        self._save = on_state or (lambda s: None)
        self._check()
        try:
            self.plugin.ensure_running()
            self._client = self.plugin.controller(self.target.get("pin"))
        except Exception as e:
            self.log("Calcul distant en pause : " + str(e))
            raise engine.Paused() from e
        stat = os.stat(path)
        fingerprint = [stat.st_size, stat.st_mtime_ns]
        if self._state.get("remote_source_stat") not in (None, fingerprint):
            raise engine.UpscaleError("La source a changé depuis le lancement de ce travail distant.")
        sha = self._state.get("remote_source_sha256")
        if not sha:
            self._emit("Vérification de la source avant envoi", 0)
            sha = sha256_file(path, check=self._check,
                progress=lambda n: self._emit("Vérification de la source avant envoi", 0.02 * n / stat.st_size,
                                             transfer={"done": n, "total": stat.st_size}))
            self._state.update(remote_source_sha256=sha, remote_source_stat=fingerprint)
            self._save(dict(self._state))
        self._jid = self._state.get("remote_id", "")
        while not self._jid:
            self._check()
            try:
                job = self._client.post("/v1/controller/jobs", {
                    "request_id": self.target["request_id"], "worker": self.target["worker"],
                    "source_path": str(Path(path).resolve()), "source_size": stat.st_size,
                    "source_sha256": sha, "settings": settings.to_dict(), "gpu": self.target.get("gpu", "auto"),
                    "result_name": Path(output).name, "kind": self.kind, "time_s": self.time_s})
                self._jid = job["id"]
                self._state["remote_id"] = self._jid
                self._save(dict(self._state))
            except ProtocolError as e:
                if e.status < 500:
                    self.log(str(e))
                    raise engine.Paused() from e
                self._wait_link(e)
            except OSError as e:
                self._wait_link(e)
        self.plugin.command(self.target["request_id"], "run")
        while True:
            self._check()
            try:
                self._client.post(f"/v1/jobs/{self._jid}/control", {"desired": "run"})
                break
            except (OSError, ProtocolError) as e:
                if isinstance(e, ProtocolError) and e.status in (401, 403, 404, 495):
                    self.log(str(e))
                    raise engine.Paused() from e
                self._wait_link(e)
        self.log("Calcul confié à « " + self.target.get("name", "PC distant") + " » ; GPU " + self.target.get("gpu", "auto"))
        last_log = ""
        while True:
            self._check()
            try:
                job = self._client.get(f"/v1/jobs/{self._jid}")
                status, p = job["status"], job.get("progress", {})
                if job.get("log") and job["log"] != last_log:
                    self.log("[PC distant] " + job["log"])
                    last_log = job["log"]
                if status in ("ready", "delivered", "cleaned"):
                    return self._receive(job, output)
                if status == "error":
                    self.log("Erreur sur le PC de calcul : " + (job.get("error") or "Calcul interrompu."))
                    self.log("Les fichiers et les réglages sont conservés. Corrigez l'erreur puis cliquez sur Reprendre, ou abandonnez explicitement ce travail.")
                    self._emit("Erreur distante · fichiers conservés, consulter le journal",
                               float(self._state.get("remote_fraction", 0.02)))
                    raise engine.Paused()
                if status == "cancelled":
                    raise engine.Cancelled()
                stage = p.get("stage") or "En attente du PC de calcul"
                if status == "receiving":
                    ratio = p.get("done", 0) / max(1, p.get("total", 0))
                    self._emit(stage, 0.02 + 0.13 * ratio, transfer=p)
                elif status == "uploading":
                    ratio = p.get("done", 0) / max(1, p.get("total", 0))
                    self._emit("Renvoi du résultat vers votre PC", 0.9 + 0.05 * ratio, transfer=p)
                elif status == "running":
                    ratio = p.get("done", 0) / max(1, p.get("total", 0))
                    self._emit(stage + " · " + self.target.get("name", "PC distant"), 0.15 + 0.75 * ratio, p)
                else:
                    self._emit("En attente du PC de calcul" if status == "queued" else stage,
                               float(self._state.get("remote_fraction", 0.02)))
                if not job.get("worker_online", True):
                    self._emit("PC de calcul déconnecté · reconnexion automatique",
                               float(self._state.get("remote_fraction", 0.02)))
            except (OSError, ProtocolError) as e:
                if isinstance(e, ProtocolError) and e.status in (401, 403, 404, 495):
                    self.log("Travail conservé en pause : " + str(e))
                    raise engine.Paused() from e
                self._wait_link(e)
            self._pause.wait(0.8)

    def _wait_link(self, error):
        self._check()
        self._emit("Relais inaccessible · reconnexion automatique",
                   float(self._state.get("remote_fraction", 0)))
        self._pause.wait(1.5)

    def _receive(self, job, output):
        result = job.get("result", {})
        sha, size = result.get("sha256"), result.get("size", 0)
        if not sha or not size:
            raise engine.UpscaleError("Le relais n'a pas fourni d'empreinte du résultat.")
        self.auto_finish_choice = result.get("finish", "")
        final = Path(output)
        partial = Path(output + ".partiel")
        final.parent.mkdir(parents=True, exist_ok=True)
        if final.exists():
            if final.stat().st_size != size or sha256_file(final, check=self._check) != sha:
                self.log("Le fichier de destination existe déjà. Les copies distantes sont conservées.")
                raise engine.Paused()
        else:
            if job["status"] != "ready":
                self.log("Un reçu existe, mais le fichier local a été déplacé/supprimé. Reprise manuelle nécessaire.")
                raise engine.Paused()
            for attempt in range(3):
                if partial.exists() and partial.stat().st_size > size:
                    partial.unlink()
                offset = partial.stat().st_size if partial.exists() else 0
                try:
                    with partial.open("ab") as f:
                        while offset < size:
                            self._check()
                            content = self._client.request("GET", f"/v1/jobs/{self._jid}/result?offset={offset}", raw=True)
                            if not content or offset + len(content) > size:
                                raise ProtocolError("Réception du résultat interrompue.", 503)
                            f.write(content)
                            f.flush()
                            offset += len(content)
                            self._emit("Récupération du résultat amélioré", 0.95 + 0.04 * offset / size,
                                       transfer={"done": offset, "total": size})
                        os.fsync(f.fileno())
                    self._emit("Vérification finale du fichier reçu (SHA-256)", 0.99)
                    if sha256_file(partial, check=self._check) == sha:
                        durable_replace(partial, final)
                        # Le reçu n'est émis qu'une fois le résultat final enregistré et vérifié.
                        break
                    partial.unlink()
                    self.log("Réception corrompue : nouveau transfert, le résultat distant reste conservé.")
                except OSError as e:
                    self.log("Réception en pause (disque/réseau) : " + str(e))
                    raise engine.Paused() from e
            else:
                raise engine.Paused()
        self._client.post(f"/v1/jobs/{self._jid}/ack", {"sha256": sha})
        self.log("Résultat reçu et vérifié. Le PC de calcul peut supprimer ses fichiers de travail.")
        self._emit("Résultat reçu et vérifié", 1)
        return str(final)
