"""Test de l'extension installée depuis le vrai .exe (sans GPU, sans Internet public)."""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import threading
from pathlib import Path

from upscaler import tools
from upscaler.extension_runtime import sha256_file, tls_identity
from .common import CHUNK, Client, ProtocolError, token
from .hub import Hub, Server
from .runner import RemoteRunner
from .worker import Worker


def run():
    with tempfile.TemporaryDirectory(prefix="upv_ext_selftest_") as tmp:
        folder = Path(tmp)
        pin = tls_identity(folder)
        master = token()
        hub = Hub(folder / "hub", master, pin)
        server = Server("127.0.0.1", 0, hub, folder / "tls.crt", folder / "tls.key")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"https://127.0.0.1:{server.server_port}"
            controller = Client(url, pin, master)
            try:
                Client(url, "0" * 64, master).get("/v1/controller/state")
            except ProtocolError as e:
                assert e.status == 495
            else:
                raise AssertionError("Un certificat non appairé a été accepté.")
            pair = controller.post("/v1/controller/pair")
            identity = Client(url, pin).post("/v1/pair", {"secret": pair["secret"], "name": "GPU selftest"})
            try:
                Client(url, pin).post("/v1/pair", {"secret": pair["secret"], "name": "intrus"})
            except ProtocolError:
                pass
            else:
                raise AssertionError("Code d'appairage réutilisable.")
            source = folder / "source.png"
            source.write_bytes(tools.make_png(16, 16) + os.urandom(CHUNK + 7))
            from upscaler import engine
            settings = engine.Settings(output_size="x2", finish="auto", interp_fps=60)
            job = controller.post("/v1/controller/jobs", {"request_id": "selftest", "worker": identity["id"],
                "source_path": str(source), "source_size": source.stat().st_size,
                "source_sha256": sha256_file(source), "result_name": "result.png", "settings": settings.to_dict()})

            class Passthrough:
                def __init__(self, **kw):
                    pass

                def process(self, source, settings, output, **kw):
                    assert settings.finish == "auto" and settings.interp_fps == 60
                    shutil.copyfile(source, output)
                    return output

            worker = Worker(folder / "worker", {"url": url, "pin": pin, "token": identity["token"]}, factory=Passthrough)
            worker.run_one(job)
            assert (folder / "worker" / job["id"]).exists(), "Suppression prématurée !"
            assert hub.state["jobs"][job["id"]]["status"] == "ready"

            class LocalPlugin:
                def ensure_running(self):
                    pass

                def controller(self, pin=None):
                    return controller

                def command(self, request_id, desired):
                    pass

            target = {"worker": identity["id"], "name": "GPU selftest", "request_id": "selftest", "pin": pin}
            output = folder / "received.png"
            runner = RemoteRunner(LocalPlugin(), target)
            runner.process(str(source), settings, str(output))
            assert hashlib.sha256(output.read_bytes()).hexdigest() == sha256_file(source)
            worker.step()
            assert not (folder / "worker" / job["id"]).exists()
            assert not (folder / "hub" / job["id"]).exists()
            assert source.exists()
            return "TLS épinglé, code unique, transfert >2 Mo, réglages figés, reçu SHA-256 puis nettoyage : OK"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
