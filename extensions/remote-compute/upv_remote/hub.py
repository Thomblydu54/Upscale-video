"""Relais privé sur le PC principal. HTTPS, rôles séparés, aucun shell/contrôle du bureau.

Le relais ne reçoit aucun chemin de fichier d'un pair distant. Seul le contrôleur LOCAL
peut publier un média précis, qui est ensuite lu par morceaux par le GPU appairé.
"""
from __future__ import annotations

import contextlib
import copy
import json
import math
import os
import secrets
import shutil
import ssl
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from upscaler.extension_runtime import atomic_json, read_json, sha256_file
from .common import (CHUNK, JSON_LIMIT, MAX_FILE, TERMINAL, ProtocolError, digest, filename,
                     identifier, loopback, safe_settings, token, token_hash)


class Hub:
    def __init__(self, folder, controller_token: str, pin: str):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.folder / "hub.json"
        self.state = read_json(self.path, {"workers": {}, "jobs": {}, "pairs": {}})
        self.state.setdefault("cancelled_requests", {})
        self.master = token_hash(controller_token)
        self.pin = pin
        self.lock = threading.RLock()
        self.io_lock = threading.RLock()
        self.failures = {}
        # Un ACK enregistré avant une coupure reste acquis ; ces copies ne sont plus utiles.
        for job in self.state["jobs"].values():
            if job["status"] in TERMINAL:
                shutil.rmtree(self.folder / job["id"], ignore_errors=True)

    def save(self):
        atomic_json(self.path, self.state)

    def auth(self, bearer: str):
        hashed = token_hash(bearer)
        with self.lock:
            if secrets.compare_digest(hashed, self.master):
                return "controller", ""
            for wid, worker in self.state["workers"].items():
                if not worker.get("revoked") and secrets.compare_digest(hashed, worker["token_hash"]):
                    return "worker", wid
        raise ProtocolError("Appairage absent ou révoqué.", 401)

    def create_pair(self):
        with self.lock:
            secret = token()
            self.state["pairs"] = {token_hash(secret): time.time() + 600}  # un seul code actif, 10 min
            self.save()
            return {"secret": secret, "expires": time.time() + 600, "pin": self.pin}

    def pair(self, data: dict, ip: str):
        with self.lock:
            now = time.time()
            # Borné, y compris face à des adresses différentes ; pas de journal contenant les codes.
            self.failures = {k: v for k, v in self.failures.items() if v[1] > now - 60}
            count, _ = self.failures.get(ip, (0, now))
            if count >= 5 or len(self.failures) > 200:
                raise ProtocolError("Trop de tentatives d'appairage. Réessayez dans une minute.", 429)
            key = token_hash(data.get("secret", ""))
            expiry = self.state["pairs"].get(key, 0)
            if expiry < now:
                self.failures[ip] = (count + 1, now)
                raise ProtocolError("Code d'appairage expiré ou déjà utilisé.", 403)
            del self.state["pairs"][key]
            wid, secret = uuid.uuid4().hex, token()
            self.state["workers"][wid] = {"id": wid, "name": filename(data.get("name", "PC de calcul")),
                "token_hash": token_hash(secret), "seen": now, "gpus": [], "gpu": "auto", "revoked": False}
            self.save()
            return {"id": wid, "token": secret, "protocol": 1}

    def workers(self):
        with self.lock:
            return [{k: v for k, v in w.items() if k != "token_hash"} |
                    {"online": time.time() - w.get("seen", 0) < 15}
                    for w in self.state["workers"].values() if not w.get("revoked")]

    def revoke(self, wid: str):
        with self.lock:
            if any(j["worker"] == wid and j["status"] != "cleaned"
                   for j in self.state["jobs"].values()):
                raise ProtocolError("Abandonnez les travaux de ce PC et attendez leur nettoyage avant de le révoquer.", 409)
            w = self.state["workers"].get(wid)
            if not w:
                raise ProtocolError("PC inconnu.", 404)
            w["revoked"] = True
            self.save()
            return {}

    def ping(self, wid: str, data: dict):
        with self.lock:
            w = self.state["workers"][wid]
            w["seen"] = time.time()
            if data.get("name"):
                w["name"] = filename(data["name"])
            w["gpu"] = str(data.get("gpu", "auto"))[:8]
            w["version"] = str(data.get("version", ""))[:20]
            w["gpus"] = [{"id": str(g.get("id", ""))[:8], "name": str(g.get("name", ""))[:160]}
                         for g in data.get("gpus", [])[:16] if isinstance(g, dict)]
            self.save()
            return {"protocol": 1}

    def public_job(self, job):
        worker = self.state["workers"].get(job["worker"], {})
        return {k: copy.deepcopy(v) for k, v in job.items() if k != "source_path"} | {
            "worker_online": time.time() - worker.get("seen", 0) < 15}

    def job(self, jid: str, role: str, wid: str):
        identifier(jid)
        job = self.state["jobs"].get(jid)
        if not job or (role == "worker" and job["worker"] != wid):
            raise ProtocolError("Traitement introuvable.", 404)
        return job

    def create_job(self, data: dict):
        worker = identifier(data.get("worker", ""))
        request_id = identifier(data.get("request_id", ""), request=True)
        with self.lock:
            w = self.state["workers"].get(worker)
            if not w or w.get("revoked"):
                raise ProtocolError("PC de calcul non appairé.", 404)
            for job in self.state["jobs"].values():
                if job["request_id"] == request_id:
                    if job["worker"] != worker or job["source_sha256"] != data.get("source_sha256"):
                        raise ProtocolError("Ce traitement existe déjà avec une autre source/destination.", 409)
                    return self.public_job(job)
            if request_id in self.state["cancelled_requests"]:
                raise ProtocolError("Ce travail a été abandonné.", 409)
            if sum(j["worker"] == worker and j["status"] != "cleaned"
                   for j in self.state["jobs"].values()) >= 100:
                raise ProtocolError("Ce PC a déjà 100 travaux en attente de livraison/nettoyage.", 409)
            source = Path(str(data.get("source_path", ""))).resolve(strict=True)
            size = source.stat().st_size
            name = filename(source.name)
            from .common import VIDEO_EXT
            from upscaler.engine import IMAGE_EXT
            if not source.is_file() or source.suffix.lower() not in VIDEO_EXT + IMAGE_EXT:
                raise ProtocolError("Ce fichier n'est pas un média pris en charge.")
            if size != data.get("source_size") or not 0 < size <= MAX_FILE:
                raise ProtocolError("Le fichier source a changé ou sa taille est invalide.")
            result = filename(data.get("result_name", "result.mp4"))
            if Path(result).suffix.lower() not in (".mp4", ".mkv", ".png", ".jpg", ".webp"):
                raise ProtocolError("Format de résultat invalide.")
            kind = data.get("kind", "process")
            if kind not in ("process", "preview"):
                raise ProtocolError("Type de travail inconnu.")
            t = float(data.get("time_s", 0))
            if not math.isfinite(t) or t < 0 or t > 1e8:
                raise ProtocolError("Position d'aperçu invalide.")
            jid = uuid.uuid4().hex
            job = {"id": jid, "request_id": request_id, "worker": worker, "created": time.time(),
                   "source_path": str(source), "source_name": name, "source_size": size,
                   "source_mtime": source.stat().st_mtime_ns,
                   "source_sha256": digest(data.get("source_sha256", "")),
                   "result_name": result, "settings": safe_settings(data.get("settings", {}), data.get("gpu", "auto")),
                   "status": "queued", "desired": "run", "progress": {}, "kind": kind, "time_s": t}
            self.state["jobs"][jid] = job
            self.save()
            return self.public_job(job)

    def control(self, jid: str, desired: str, sequence=None):
        if desired not in ("run", "pause", "cancel"):
            raise ProtocolError("Commande de traitement inconnue.")
        with self.io_lock, self.lock:
            job = self.job(jid, "controller", "")
            sequence = int(sequence or time.time_ns())
            if job["status"] not in TERMINAL and sequence > job.get("command_seq", 0):
                job["command_seq"] = sequence
                job["desired"] = desired
                if desired == "cancel":
                    job.update(status="cancelled", source_path="")
                    shutil.rmtree(self.folder / jid, ignore_errors=True)
                self.save()
            return self.public_job(job)

    def command_request(self, request_id: str, desired: str, sequence):
        with self.lock:
            if desired == "cancel":
                self.state["cancelled_requests"][request_id] = time.time()
                self.save()
            ids = [j["id"] for j in self.state["jobs"].values() if j["request_id"] == request_id]
        for jid in ids:
            self.control(jid, desired, sequence)

    def report(self, jid: str, wid: str, data: dict):
        with self.lock:
            job = self.job(jid, "worker", wid)
            if job["status"] not in (*TERMINAL, "ready"):
                status = data.get("status", job["status"])
                if status not in ("receiving", "running", "paused", "uploading", "error"):
                    raise ProtocolError("État de traitement invalide.")
                job["status"] = status
                job["error"] = str(data.get("error", ""))[:2000]
                p = data.get("progress", {})
                clean = {}
                for k in ("done", "total", "speed", "eta", "elapsed"):
                    v = float(p.get(k, 0))
                    clean[k] = max(0, v) if math.isfinite(v) else 0
                clean["stage"] = str(p.get("stage", "Calcul distant"))[:120]
                job["progress"] = clean
                job["log"] = str(data.get("log", ""))[-1200:]
                self.save()
            return self.public_job(job)

    def source_chunk(self, job, offset: int):
        if job["status"] in TERMINAL or not job.get("source_path"):
            raise ProtocolError("Ce média n'est plus disponible.", 410)
        source = Path(job["source_path"])
        stat = source.stat()
        if stat.st_size != job["source_size"] or stat.st_mtime_ns != job["source_mtime"]:
            raise ProtocolError("La vidéo originale a été modifiée pendant son envoi.", 409)
        if not 0 <= offset <= stat.st_size:
            raise ProtocolError("Position de transfert invalide.")
        with source.open("rb") as f:
            f.seek(offset)
            return f.read(CHUNK)

    def begin_result(self, jid, wid, data):
        with self.io_lock, self.lock:
            job = self.job(jid, "worker", wid)
            if job["status"] in TERMINAL:
                return self.public_job(job)
            size, sha = int(data.get("size", 0)), digest(data.get("sha256", ""))
            if not 0 < size <= MAX_FILE:
                raise ProtocolError("Taille de résultat invalide.")
            folder = self.folder / jid
            folder.mkdir(exist_ok=True, mode=0o700)
            path = folder / "result.part"
            old = job.get("result", {})
            if old and (old["sha256"] != sha or old["size"] != size):
                raise ProtocolError("Le résultat a changé depuis le début du transfert.", 409)
            job["result"] = {"size": size, "sha256": sha,
                             "finish": str(data.get("finish", ""))[:30],
                             "clean": str(data.get("clean", ""))[:40]}
            if job["status"] != "ready":
                job["status"] = "uploading"
            self.save()
            return {**self.public_job(job), "offset": path.stat().st_size if path.exists() else 0}

    def result_chunk(self, jid, wid, offset, content):
        with self.io_lock, self.lock:
            job = self.job(jid, "worker", wid)
            if job["status"] != "uploading" or "result" not in job:
                raise ProtocolError("Le transfert n'est plus actif.", 409)
            path = self.folder / jid / "result.part"
            current = path.stat().st_size if path.exists() else 0
            if offset != current or offset + len(content) > job["result"]["size"]:
                raise ProtocolError(f"Position de reprise attendue : {current}.", 409)
            with path.open("ab") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            return {"offset": current + len(content)}

    def finish_result(self, jid, wid):
        with self.io_lock:
            with self.lock:
                job = self.job(jid, "worker", wid)
                if job["status"] in (*TERMINAL, "ready"):
                    return self.public_job(job)
                result = dict(job.get("result", {}))
            path = self.folder / jid / "result.part"
            if (not result or not path.exists() or path.stat().st_size != result["size"]
                    or sha256_file(path) != result["sha256"]):
                with contextlib.suppress(OSError):
                    path.unlink()
                raise ProtocolError("Résultat incomplet/corrompu : le transfert doit reprendre à zéro.", 409)
            with self.lock:
                job["status"] = "ready"
                self.save()
                return self.public_job(job)

    def acknowledge(self, jid, sha):
        with self.io_lock, self.lock:
            job = self.job(jid, "controller", "")
            if (job["status"] not in ("ready", "delivered", "cleaned")
                    or not secrets.compare_digest(str(sha), job.get("result", {}).get("sha256", ""))):
                raise ProtocolError("Réception non vérifiée : les copies distantes sont conservées.", 409)
            job.update(status="delivered" if job["status"] != "cleaned" else "cleaned",
                       desired="cleanup", source_path="", delivered=time.time())
            self.save()  # d'abord l'ACK durable, ensuite seulement la suppression du cache
            shutil.rmtree(self.folder / jid, ignore_errors=True)
            return self.public_job(job)

    def cleaned(self, jid, wid):
        with self.lock:
            job = self.job(jid, "worker", wid)
            if job["status"] not in TERMINAL:
                raise ProtocolError("Aucun reçu de livraison : suppression refusée.", 409)
            job.update(status="cleaned", cleaned=time.time(), source_path="")
            self.save()
            return {}

    def reap(self):
        with self.lock:
            old = [k for k, j in self.state["jobs"].items()
                   if j["status"] == "cleaned" and time.time() - j.get("cleaned", 0) > 30 * 86400]
            for k in old:
                del self.state["jobs"][k]
            if old:
                self.save()


class Handler(BaseHTTPRequestHandler):
    server_version = "UpscaleRelay/1"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass  # jamais de token, code d'appairage ni URL privée dans les logs

    def _send(self, data, status=200, binary=False):
        raw = data if binary else json.dumps(data, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream" if binary else "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)
        self.close_connection = True

    def _body(self, maximum=JSON_LIMIT, binary=False):
        if self.headers.get("Transfer-Encoding"):
            raise ProtocolError("Transfert HTTP non accepté.")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ProtocolError("Longueur de requête invalide.") from None
        if not 0 <= length <= maximum:
            raise ProtocolError("Requête trop volumineuse.", 413)
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ProtocolError("Requête interrompue.")
        if binary:
            return raw
        try:
            data = json.loads(raw or b"{}", parse_constant=lambda s: (_ for _ in ()).throw(ValueError(s)))
            if not isinstance(data, dict):
                raise ValueError()
            return data
        except (ValueError, UnicodeError):
            raise ProtocolError("JSON invalide.") from None

    def handle_expect_100(self):
        self._send({"error": "Expect non accepté."}, 417)
        return False

    def _dispatch(self):
        if self.headers.get("Origin") is not None:
            raise ProtocolError("Les requêtes depuis un site web sont refusées.", 403)
        url = urlsplit(self.path)
        path = url.path
        if url.scheme or url.netloc or len(self.path) > 500:
            raise ProtocolError("Chemin de requête invalide.")
        hub = self.server.hub
        if self.command == "POST" and path == "/v1/pair":
            return self._send(hub.pair(self._body(), self.client_address[0]))
        auth = self.headers.get("Authorization", "")
        bearer = auth[7:] if auth.startswith("Bearer ") else ""
        if len(bearer) != 43:
            raise ProtocolError("Authentification requise.", 401)
        role, wid = hub.auth(bearer)
        if role == "controller" and not loopback(self.client_address[0]):
            raise ProtocolError("Administration locale uniquement.", 403)
        if role == "worker":
            with hub.lock:
                hub.state["workers"][wid]["seen"] = time.time()
        if path.startswith("/v1/controller/"):
            # Les fonctions d'administration sont locales ET protégées par une clé distincte.
            if role != "controller" or not loopback(self.client_address[0]):
                raise ProtocolError("Commande réservée à l'application principale locale.", 403)
            if self.command == "GET" and path == "/v1/controller/state":
                return self._send({"workers": hub.workers(), "jobs": len(hub.state["jobs"]), "pin": hub.pin})
            if self.command == "POST" and path == "/v1/controller/pair":
                return self._send(hub.create_pair())
            if self.command == "POST" and path == "/v1/controller/revoke":
                return self._send(hub.revoke(identifier(self._body().get("id", ""))))
            if self.command == "POST" and path == "/v1/controller/jobs":
                return self._send(hub.create_job(self._body()))
        if self.command == "POST" and path == "/v1/worker/ping" and role == "worker":
            return self._send(hub.ping(wid, self._body()))
        if self.command == "GET" and path == "/v1/worker/jobs" and role == "worker":
            with hub.lock:
                jobs = [{k: j[k] for k in ("id", "status", "desired")} for j in hub.state["jobs"].values()
                        if j["worker"] == wid and j["status"] != "cleaned"]
            return self._send({"jobs": jobs[:100]})
        parts = path.strip("/").split("/")
        if len(parts) in (3, 4) and parts[:2] == ["v1", "jobs"]:
            jid = identifier(parts[2])
            action = parts[3] if len(parts) == 4 else ""
            with hub.lock:
                job = hub.public_job(hub.job(jid, role, wid))
            if self.command == "GET" and action == "":
                return self._send(job)
            if role == "controller" and not loopback(self.client_address[0]):
                raise ProtocolError("Commande locale requise.", 403)
            if self.command == "POST" and action == "control" and role == "controller":
                data = self._body()
                return self._send(hub.control(jid, data.get("desired", ""), data.get("sequence")))
            if self.command == "POST" and action == "ack" and role == "controller":
                return self._send(hub.acknowledge(jid, self._body().get("sha256", "")))
            if self.command == "GET" and action == "source" and role == "worker":
                offset = int(parse_qs(url.query).get("offset", ["0"])[0])
                with hub.lock:
                    content = hub.source_chunk(hub.job(jid, role, wid), offset)
                return self._send(content, binary=True)
            if self.command == "GET" and action == "result" and role == "controller":
                if job["status"] != "ready":
                    raise ProtocolError("Le résultat n'est pas encore prêt.", 409)
                offset = int(parse_qs(url.query).get("offset", ["0"])[0])
                if not 0 <= offset <= job["result"]["size"]:
                    raise ProtocolError("Position invalide.")
                with (hub.folder / jid / "result.part").open("rb") as f:
                    f.seek(offset)
                    content = f.read(CHUNK)
                return self._send(content, binary=True)
            if role == "worker":
                if self.command == "POST" and action == "report":
                    return self._send(hub.report(jid, wid, self._body()))
                if self.command == "POST" and action == "result":
                    return self._send(hub.begin_result(jid, wid, self._body()))
                if self.command == "PUT" and action == "result":
                    offset = int(parse_qs(url.query).get("offset", ["0"])[0])
                    return self._send(hub.result_chunk(jid, wid, offset, self._body(CHUNK, binary=True)))
                if self.command == "POST" and action == "finish":
                    return self._send(hub.finish_result(jid, wid))
                if self.command == "POST" and action == "cleaned":
                    return self._send(hub.cleaned(jid, wid))
        raise ProtocolError("Fonction inconnue.", 404)

    def _handle(self):
        try:
            self._dispatch()
        except ProtocolError as e:
            self._send({"error": str(e)}, e.status)
        except (ValueError, TypeError, KeyError, OverflowError):
            self._send({"error": "Paramètre invalide."}, 400)
        except FileNotFoundError:
            self._send({"error": "Fichier de ce traitement introuvable."}, 404)
        except (BrokenPipeError, ConnectionError, TimeoutError, ssl.SSLError):
            self.close_connection = True
        except OSError:
            self._send({"error": "Écriture/lecture impossible. Vérifiez l'espace disque du relais."}, 507)
        except Exception:
            self._send({"error": "Erreur interne du relais."}, 500)

    do_GET = do_POST = do_PUT = _handle


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 16

    def __init__(self, host, port, hub, cert, key):
        self.hub = hub
        self.slots = threading.BoundedSemaphore(16)
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.context.load_cert_chain(cert, key)
        super().__init__((host, port), Handler)

    def get_request(self):
        sock, address = self.socket.accept()
        sock.settimeout(12)
        return self.context.wrap_socket(sock, server_side=True, do_handshake_on_connect=False), address

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            request.do_handshake()
            super().process_request_thread(request, client_address)
        except (OSError, ssl.SSLError):
            self.shutdown_request(request)
        finally:
            self.slots.release()

    def handle_error(self, *args):
        pass
