"""Cycle de vie de l'extension. Installer ≠ activer, appairer ≠ démarrer avec Windows."""
from __future__ import annotations

import os
import socket
import threading
import time
import uuid
from pathlib import Path

from upscaler import tools
from upscaler.extension_runtime import (ExtensionError, ServiceLock, atomic_json, load_private,
    read_json, save_private, set_startup, spawn_service, tls_identity)
from .common import TERMINAL, Client, endpoint, filename, identifier, pairing_code, parse_pairing, token
from .hub import Hub, Server
from .runner import RemoteRunner
from .worker import Worker


class Plugin:
    def __init__(self, folder, log=lambda s: None):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.config_path = self.folder / "private.dat"
        self.status_path = self.folder / "status.json"
        self.log = log
        self.lock = threading.RLock()

    def config(self):
        return load_private(self.config_path)

    def _save(self, config):
        save_private(self.config_path, config)

    def public(self):
        config = self.config()
        status = read_json(self.status_path)
        alive = (time.time() - status.get("updated", 0) < 8 and status.get("running", False)
                 and self._service_locked())
        try:
            ips = [ip for ip in socket.gethostbyname_ex(socket.gethostname())[2]
                   if not ip.startswith("127.")]
        except OSError:
            ips = []
        # Aucune clé, aucun bearer ni code d'appairage dans poll()/init().
        return {"installed": True, "role": config.get("role", ""), "enabled": config.get("enabled", False),
                "startup": config.get("startup", False), "running": bool(alive),
                "port": config.get("port", 38443), "url": config.get("url", ""),
                "name": config.get("name", socket.gethostname()), "gpu": config.get("gpu", "auto"),
                "selected": config.get("selected", ""), "local_ips": ips,
                "workers": status.get("workers", []), "message": status.get("message", "Service arrêté"),
                "active": status.get("active"), "progress": status.get("progress", {}), "gpus": status.get("gpus", []),
                "pending": status.get("pending", 0)}

    def controller(self, pin=None):
        config = self.config()
        if config.get("role") != "controller" or not config.get("pin"):
            raise ExtensionError("Configurez ce PC comme PC principal dans Extensions.")
        if pin and pin != config["pin"]:
            raise ExtensionError("Ce travail appartient à un autre relais. Sa destination n'a pas été changée.")
        return Client(f"https://127.0.0.1:{config['port']}", config["pin"], config["token"], timeout=5)

    def selected(self):
        config = self.config()
        selected = config.get("selected")
        if not selected or config.get("role") != "controller":
            return None
        worker = next((w for w in self.public()["workers"] if w["id"] == selected), None)
        if not worker:
            raise ExtensionError("Le PC distant choisi n'est plus appairé. Choisissez une destination.")
        return {"worker": selected, "name": worker["name"], "gpu": worker.get("gpu", "auto"),
                "pin": config["pin"]}

    def select(self, worker_id):
        with self.lock:
            config = self.config()
            if worker_id and not any(w["id"] == worker_id for w in self.public()["workers"]):
                raise ExtensionError("PC distant inconnu.")
            config["selected"] = worker_id or ""
            self._save(config)
        return self.public()

    def _pending(self):
        hub = read_json(self.folder / "hub" / "hub.json")
        hub_busy = any(j.get("status") != "cleaned" for j in hub.get("jobs", {}).values())
        work = self.folder / "worker"
        worker_busy = work.exists() and any(p.is_dir() for p in work.iterdir())
        return hub_busy or worker_busy

    def _can_reconfigure(self):
        if self._pending():
            raise ExtensionError("Des travaux/copies attendent encore une livraison ou un nettoyage. "
                                 "Terminez-les ou abandonnez-les avant de changer de rôle/appairage.")

    def configure_controller(self, values):
        with self.lock:
            config = self.config()
            raw_port = str(values.get("port", 38443))
            if not raw_port.isdigit() or len(raw_port) > 5:
                raise ExtensionError("Le port doit être un nombre entier.")
            port = int(raw_port)
            if not 1024 <= port <= 65535:
                raise ExtensionError("Choisissez un port TCP entier entre 1024 et 65535.")
            url = endpoint(values.get("url", "")) if values.get("url") else ""
            # Changer l'adresse/port ne change pas l'identité ni les travaux du relais.
            if config.get("role") not in (None, "controller"):
                self._can_reconfigure()
            self.stop()
            pin = tls_identity(self.folder)
            startup = bool(values.get("startup", False))
            set_startup(startup)
            config.update(role="controller", enabled=True, port=port, url=url, pin=pin,
                          token=config.get("token") if config.get("role") == "controller" else token(),
                          startup=startup, revision=uuid.uuid4().hex)
            self._save(config)
            self.ensure_running()
            return self.public()

    def join(self, values):
        with self.lock:
            self._can_reconfigure()
            pair = parse_pairing(values.get("code", ""))
            name = filename(values.get("name") or socket.gethostname())
            gpu = str(values.get("gpu", "auto"))
            if gpu != "auto" and not (gpu.isdigit() and len(gpu) <= 3):
                raise ExtensionError("GPU invalide.")
            self.stop()
            client = Client(pair["url"], pair["pin"], timeout=15)
            result = client.post("/v1/pair", {"secret": pair["secret"], "name": name})
            startup = bool(values.get("startup", False))
            config = {"role": "worker", "enabled": True, "url": pair["url"], "pin": pair["pin"],
                      "token": result["token"], "worker_id": result["id"], "name": name,
                      "gpu": gpu, "startup": startup, "selected": "", "revision": uuid.uuid4().hex}
            self._save(config)  # le code à usage unique ne doit jamais être perdu après la réponse
            set_startup(startup)
            self.ensure_running()
            return self.public()

    def update_worker(self, values):
        with self.lock:
            config = self.config()
            if config.get("role") != "worker":
                raise ExtensionError("Appairez d'abord ce PC de calcul.")
            gpu = str(values.get("gpu", config.get("gpu", "auto")))
            if gpu != "auto" and not (gpu.isdigit() and len(gpu) <= 3):
                raise ExtensionError("GPU invalide.")
            url = endpoint(values.get("url") or config["url"])
            name = filename(values.get("name") or config.get("name") or socket.gethostname())
            # Même certificat et même identité : corriger une IP ne recrée pas les travaux.
            Client(url, config["pin"], config["token"]).post("/v1/worker/ping", {
                "gpu": gpu, "name": name, "gpus": self.public().get("gpus", [])})
            self.stop()
            startup = bool(values.get("startup", config.get("startup", False)))
            set_startup(startup)
            config.update(url=url, gpu=gpu, name=name, startup=startup, enabled=True, revision=uuid.uuid4().hex)
            self._save(config)
            self.ensure_running()
            return self.public()

    def create_pair(self):
        config = self.config()
        if not config.get("url"):
            raise ExtensionError("Indiquez d'abord l'adresse publique https://IP-Freebox:port.")
        self.ensure_running()
        pair = self.controller().post("/v1/controller/pair")
        return {"code": pairing_code(config["url"], config["pin"], pair["secret"]),
                "expires": pair["expires"]}

    def set_startup(self, enabled):
        with self.lock:
            config = self.config()
            if enabled and not config.get("role"):
                raise ExtensionError("Configurez d'abord le rôle de ce PC.")
            set_startup(bool(enabled))
            config["startup"] = bool(enabled)
            self._save(config)
        return self.public()

    def ensure_running(self):
        config = self.config()
        if not config.get("enabled"):
            raise ExtensionError("Le service est désactivé. Réactivez-le dans Extensions.")
        if self.public()["running"]:
            return
        started = time.time()
        spawn_service()
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if self.public()["running"]:
                return
            state = read_json(self.status_path)
            if state.get("failed") and state.get("updated", 0) >= started:
                raise ExtensionError(state.get("message", "Échec du démarrage du service."))
            time.sleep(0.2)
        raise ExtensionError("Le service ne démarre pas. Consultez Extensions → Ouvrir le dossier des journaux.")

    def start(self):
        with self.lock:
            config = self.config()
            if not config.get("role"):
                raise ExtensionError("Choisissez d'abord le rôle de ce PC.")
            if self.public()["running"] and config.get("enabled"):
                return self.public()
            config.update(enabled=True, revision=uuid.uuid4().hex)
            self._save(config)
            self.ensure_running()
            return self.public()

    def _service_locked(self):
        path = self.folder / "service.lock"
        if not path.exists():
            return False
        try:
            with ServiceLock(path):
                return False
        except ExtensionError:
            return True

    def stop(self, keep_enabled=False):
        config = self.config()
        enabled = config.get("enabled", False)
        config["enabled"] = False
        self._save(config)
        deadline = time.monotonic() + 35
        while self._service_locked() and time.monotonic() < deadline:
            time.sleep(0.2)
        if self._service_locked():
            raise ExtensionError("Le service enregistre encore son avancement. Réessayez dans quelques secondes.")
        if keep_enabled:
            config["enabled"] = enabled
            self._save(config)
        return self.public()

    def before_remove(self):
        with self.lock:
            self._can_reconfigure()
            self.stop()
            self.set_startup(False)

    def command(self, request_id, desired):
        identifier(request_id, request=True)
        if desired not in ("run", "pause", "cancel"):
            raise ExtensionError("Commande de travail inconnue.")
        folder = self.folder / "commands"
        sequence = time.time_ns()
        atomic_json(folder / f"{sequence:020d}-{uuid.uuid4().hex}.json", {
            "request_id": request_id, "desired": desired, "sequence": sequence})

    def abandon(self, job):
        remote = job.data.get("remote")
        if remote:
            self.command(remote.get("request_id", job.id), "cancel")

    def runner(self, target, **callbacks):
        return RemoteRunner(self, target, **callbacks)

    def preview(self, path, settings, time_s, target, on_log):
        folder = Path(tools.make_work_dir("upv_remote_preview_"))
        target = dict(target, request_id="preview-" + uuid.uuid4().hex)
        up = self.runner(target, on_log=on_log, kind="preview", time_s=time_s)
        return up, folder

    def action(self, action, values):
        if action == "configure_controller":
            return self.configure_controller(values)
        if action == "join":
            return self.join(values)
        if action == "update_worker":
            return self.update_worker(values)
        if action == "pair":
            return self.create_pair()
        if action == "select":
            return self.select(values.get("worker", ""))
        if action == "start":
            return self.start()
        if action == "stop":
            self.set_startup(False)
            return self.stop()
        if action == "startup":
            return self.set_startup(values.get("enabled", False))
        if action == "revoke":
            self.controller().post("/v1/controller/revoke", {"id": values.get("id", "")})
            if self.config().get("selected") == values.get("id"):
                self.select("")
            return self.public()
        if action == "open_logs":
            tools.open_path(str(self.folder))
            return self.public()
        raise ExtensionError("Action d'extension inconnue.")

    def _service_log(self, text):
        path = self.folder / "service.log"
        try:
            if path.exists() and path.stat().st_size > 1 << 20:
                path.write_text("[Ancien journal supprimé]\n", encoding="utf-8")
            with path.open("a", encoding="utf-8") as f:
                f.write(time.strftime("[%Y-%m-%d %H:%M:%S] ") + str(text)[:2000] + "\n")
        except OSError:
            pass

    def run_service(self):
        with ServiceLock(self.folder / "service.lock"):
            config = self.config()
            if not config.get("enabled") or config.get("role") not in ("controller", "worker"):
                return
            stop = threading.Event()
            heartbeat_done = threading.Event()
            server = worker = None
            status = {"role": config["role"], "running": True, "updated": time.time(), "pid": os.getpid()}
            try:
                if config["role"] == "controller":
                    hub = Hub(self.folder / "hub", config["token"], config["pin"])
                    server = Server("0.0.0.0", config["port"], hub,
                                    self.folder / "tls.crt", self.folder / "tls.key")
                    threading.Thread(target=server.serve_forever, daemon=True).start()
                else:
                    worker = Worker(self.folder / "worker", config, self._service_log)
                    stop = worker.stop

                def heartbeat():
                    while not heartbeat_done.is_set():
                        try:
                            current = self.config()
                            if not current.get("enabled") or current.get("revision") != config.get("revision"):
                                stop.set()
                            if server:
                                for path in sorted((self.folder / "commands").glob("*.json")):
                                    command = read_json(path)
                                    hub.command_request(command["request_id"], command["desired"], command["sequence"])
                                    path.unlink()
                                hub.reap()
                                with hub.lock:
                                    status.update(workers=hub.workers(), message="Relais HTTPS actif · en attente du PC de calcul",
                                        pending=sum(j["status"] != "cleaned" for j in hub.state["jobs"].values()))
                            else:
                                status.update(worker.snapshot())
                            if stop.is_set():
                                status["message"] = "Arrêt en cours · enregistrement de l'avancement"
                            status.update(running=True, updated=time.time(), failed=False)
                            atomic_json(self.status_path, status)
                        except Exception as e:
                            self._service_log("Service : " + str(e))
                        heartbeat_done.wait(1)

                thread = threading.Thread(target=heartbeat, daemon=True)
                thread.start()
                if worker:
                    worker.run()
                else:
                    # Sur ce thread durable, empêcher la veille seulement pendant les travaux.
                    while not stop.wait(1):
                        with hub.lock:
                            busy = any(j["desired"] == "run" and j["status"] not in (*TERMINAL, "error")
                                       for j in hub.state["jobs"].values())
                        tools.keep_awake(busy)
                stop.set()
                heartbeat_done.set()
                thread.join(timeout=3)
                status.update(running=False, updated=time.time(), message="Service arrêté · données de reprise conservées")
            except Exception as e:
                status.update(running=False, updated=time.time(), failed=True, message=str(e))
                self._service_log("Échec du service : " + str(e))
            finally:
                stop.set()
                heartbeat_done.set()
                tools.keep_awake(False)
                if server:
                    server.shutdown()
                    server.server_close()
                atomic_json(self.status_path, status)
