"""Tests hors GPU/réseau public : deux pairs HTTPS réels sur loopback, disque persistant."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import uuid
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from upscaler import engine
from upscaler.extension_runtime import ExtensionError, load_private, read_json as json_read, save_private, sha256_file, tls_identity
from upscaler.extensions import ExtensionManager
from scripts.build_extensions import package

spec = importlib.util.spec_from_file_location("upv_remote_tests",
    ROOT / "extensions/remote-compute/upv_remote/__init__.py",
    submodule_search_locations=[str(ROOT / "extensions/remote-compute/upv_remote")])
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)
from upv_remote_tests.common import CHUNK, Client, ProtocolError, pairing_code, parse_pairing, token
from upv_remote_tests.hub import Hub, Server
from upv_remote_tests.runner import RemoteRunner
from upv_remote_tests.worker import Worker


class FakeUpscaler:
    calls = []
    delay = 0.002

    def __init__(self, on_log=lambda s: None, on_progress=lambda p: None):
        self.on_log, self.on_progress = on_log, on_progress
        self._pause = threading.Event()
        self._cancel = threading.Event()
        self.auto_finish_choice = "glow"

    def pause(self):
        self._pause.set()

    def cancel(self):
        self._cancel.set()

    def process(self, source, settings, output, job_dir=None, state=None, on_state=None):
        self.calls.append(copy.deepcopy(settings.to_dict()))
        state = dict(state or {})
        Path(job_dir).mkdir(parents=True, exist_ok=True)
        for i in range(state.get("fake_done", 0), 20):
            if self._cancel.is_set():
                raise engine.Cancelled()
            if self._pause.is_set():
                raise engine.Paused()
            self.on_progress(engine.Progress(i + 1, 20, 10, 0, i / 10, "IA"))
            state["fake_done"] = i + 1
            on_state(state)
            time.sleep(self.delay)
        Path(output).write_bytes(b"RESULTAT:" + Path(source).read_bytes())
        shutil.rmtree(job_dir)
        return output

    def preview(self, source, settings, time_s=None):
        folder = Path(tempfile.gettempdir(), "UpscaleVideoAI_apercus")
        folder.mkdir(exist_ok=True)
        ai = folder / (Path(source).stem + "_ia.png")
        cmp = folder / (Path(source).stem + "_comparaison.png")
        ai.write_bytes(b"PREVIEW:" + Path(source).read_bytes())
        cmp.write_bytes(b"COMPARISON")
        return str(cmp), str(ai)


class FakePlugin:
    def __init__(self, client, hub):
        self.client, self.hub = client, hub

    def ensure_running(self):
        pass

    def controller(self, pin=None):
        return self.client

    def command(self, request_id, desired):
        self.hub.command_request(request_id, desired, time.time_ns())


class RemoteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.pin = tls_identity(self.folder)
        self.master = token()
        self.hub = Hub(self.folder / "hub", self.master, self.pin)
        self.server = Server("127.0.0.1", 0, self.hub, self.folder / "tls.crt", self.folder / "tls.key")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"https://127.0.0.1:{self.server.server_port}"
        self.controller = Client(self.url, self.pin, self.master)
        pair = self.controller.post("/v1/controller/pair")
        self.pair_secret = pair["secret"]
        self.identity = Client(self.url, self.pin).post("/v1/pair", {"secret": self.pair_secret, "name": "GPU distant"})
        self.worker_client = Client(self.url, self.pin, self.identity["token"])
        self.worker = Worker(self.folder / "worker", {"url": self.url, "pin": self.pin,
            "token": self.identity["token"], "gpu": "auto"}, factory=FakeUpscaler)
        self.source = self.folder / "video.mp4"
        self.source.write_bytes(os.urandom(CHUNK * 2 + 1234))
        self.settings = engine.Settings(output_size="x2", interpolation="custom", interp_fps=60,
            finish="auto", output_dir="C:\\private-output", temp_dir="C:\\private-temp", gpu="9")
        self.plugin = FakePlugin(self.controller, self.hub)
        FakeUpscaler.calls.clear()

    def tearDown(self):
        self.worker.stop.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def create(self, **kw):
        data = {"request_id": uuid.uuid4().hex, "worker": self.identity["id"],
                "source_path": str(self.source), "source_size": self.source.stat().st_size,
                "source_sha256": sha256_file(self.source), "settings": self.settings.to_dict(),
                "gpu": "auto", "result_name": "video_upscaled.mp4"}
        data.update(kw)
        return self.controller.post("/v1/controller/jobs", data)

    def target(self, request_id=None):
        return {"worker": self.identity["id"], "name": "GPU distant", "gpu": "auto", "pin": self.pin,
                "request_id": request_id or uuid.uuid4().hex}

    def test_tls_identity_is_stable_and_pinning_is_mandatory(self):
        self.assertEqual(tls_identity(self.folder), self.pin)
        with self.assertRaises(ProtocolError) as caught:
            Client(self.url, "0" * 64, self.master).get("/v1/controller/state")
        self.assertEqual(caught.exception.status, 495)

    def test_pairing_single_use_expiry_and_code(self):
        code = pairing_code(self.url, self.pin, self.pair_secret)
        self.assertEqual(parse_pairing(code)["pin"], self.pin)
        with self.assertRaises(ProtocolError):
            Client(self.url, self.pin).post("/v1/pair", {"secret": self.pair_secret, "name": "intrus"})
        pair = self.controller.post("/v1/controller/pair")
        with self.hub.lock:
            self.hub.state["pairs"] = {k: 1 for k in self.hub.state["pairs"]}
        with self.assertRaises(ProtocolError):
            Client(self.url, self.pin).post("/v1/pair", {"secret": pair["secret"], "name": "late"})
        for value in ("UPV1-notvalid", "file:///etc/passwd", ""):
            with self.assertRaises(ProtocolError):
                parse_pairing(value)

    def test_auth_roles_and_job_isolation(self):
        job = self.create()
        for client, path in ((Client(self.url, self.pin), "/v1/controller/state"),
                             (self.worker_client, "/v1/controller/state")):
            with self.assertRaises(ProtocolError):
                client.get(path)
        pair = self.controller.post("/v1/controller/pair")
        other = Client(self.url, self.pin).post("/v1/pair", {"secret": pair["secret"], "name": "autre"})
        with self.assertRaises(ProtocolError):
            Client(self.url, self.pin, other["token"]).get(f"/v1/jobs/{job['id']}")
        self.assertNotIn("source_path", self.worker_client.get(f"/v1/jobs/{job['id']}"))
        with self.assertRaises(ProtocolError):
            self.worker_client.post(f"/v1/jobs/{job['id']}/ack", {"sha256": "0" * 64})

    def test_no_cleanup_before_verified_receipt_then_everything_is_purged(self):
        job = self.create()
        self.worker.run_one(job)
        remote = self.controller.get(f"/v1/jobs/{job['id']}")
        self.assertEqual(remote["status"], "ready")
        spool = self.folder / "worker" / job["id"]
        self.assertTrue(spool.exists())
        with self.assertRaises(ProtocolError):
            self.controller.post(f"/v1/jobs/{job['id']}/ack", {"sha256": "0" * 64})
        self.assertTrue(spool.exists())
        # Un nouveau runner retrouve le même travail, sans refaire le calcul.
        runner = RemoteRunner(self.plugin, self.target(job["request_id"]))
        output = self.folder / "final.mp4"
        result = runner.process(str(self.source), self.settings, str(output))
        self.assertEqual(Path(result).read_bytes(), b"RESULTAT:" + self.source.read_bytes())
        self.assertTrue(spool.exists())  # le GPU n'a pas encore reçu le reçu
        self.worker.step()
        self.assertFalse(spool.exists())
        self.assertFalse((self.hub.folder / job["id"]).exists())
        self.assertTrue(self.source.exists())
        self.assertEqual(len(FakeUpscaler.calls), 1)
        self.assertEqual(FakeUpscaler.calls[0]["interp_fps"], 60)
        self.assertEqual(FakeUpscaler.calls[0]["finish"], "auto")
        self.assertEqual(FakeUpscaler.calls[0]["gpu"], "auto")
        self.assertNotIn("C:", FakeUpscaler.calls[0]["temp_dir"])

    def test_source_and_result_resume_after_restart(self):
        job = self.create()
        spool = self.folder / "worker" / job["id"]
        spool.mkdir()
        (spool / "input.part").write_bytes(self.source.read_bytes()[:CHUNK + 13])
        # Un transfert résultat interrompu est repris à sa position persistante.
        expected = b"RESULTAT:" + self.source.read_bytes()
        sha = hashlib.sha256(expected).hexdigest()
        self.worker_client.post(f"/v1/jobs/{job['id']}/result", {"size": len(expected), "sha256": sha})
        self.worker_client.request("PUT", f"/v1/jobs/{job['id']}/result?offset=0", expected[:1234])
        self.worker.run_one(job)
        self.assertEqual(self.controller.get(f"/v1/jobs/{job['id']}")["status"], "ready")
        output = self.folder / "resume.mp4"
        Path(str(output) + ".partiel").write_bytes(expected[:4321])
        state = {"remote_id": job["id"], "remote_source_sha256": job["source_sha256"],
                 "remote_source_stat": [self.source.stat().st_size, self.source.stat().st_mtime_ns]}
        RemoteRunner(self.plugin, self.target(job["request_id"])).process(
            str(self.source), self.settings, str(output), state=state)
        self.assertEqual(output.read_bytes(), expected)
        self.worker.step()
        self.assertFalse(spool.exists())

    def test_idempotent_delivery_after_crash_between_rename_and_ack(self):
        job = self.create()
        self.worker.run_one(job)
        output = self.folder / "already.mp4"
        output.write_bytes(b"RESULTAT:" + self.source.read_bytes())
        runner = RemoteRunner(self.plugin, self.target(job["request_id"]))
        runner.process(str(self.source), self.settings, str(output), state={"remote_id": job["id"]})
        self.worker.step()
        self.assertEqual(self.controller.get(f"/v1/jobs/{job['id']}")["status"], "cleaned")

    def test_cancel_survives_hub_restart_and_never_deletes_original(self):
        job = self.create()
        spool = self.folder / "worker" / job["id"]
        spool.mkdir()
        (spool / "input.part").write_bytes(b"partial")
        self.plugin.command(job["request_id"], "cancel")
        self.hub = Hub(self.hub.folder, self.master, self.pin)
        self.server.hub = self.hub
        self.worker.step()
        self.assertFalse(spool.exists())
        self.assertTrue(self.source.exists())
        request = uuid.uuid4().hex
        self.hub.command_request(request, "cancel", time.time_ns())
        with self.assertRaises(ProtocolError):
            self.create(request_id=request)

    def test_pause_and_command_order(self):
        job = self.create()
        self.hub.control(job["id"], "pause", 20)
        self.hub.control(job["id"], "run", 30)
        self.hub.control(job["id"], "pause", 21)
        self.assertEqual(self.hub.state["jobs"][job["id"]]["desired"], "run")
        self.hub.control(job["id"], "pause", 40)
        self.worker.run_one(self.hub.public_job(self.hub.state["jobs"][job["id"]]))
        self.assertEqual(self.hub.state["jobs"][job["id"]]["status"], "paused", self.hub.state["jobs"][job["id"]].get("error"))
        self.assertEqual(len(FakeUpscaler.calls), 0)
        self.hub.control(job["id"], "run", 50)
        self.worker.step()
        self.assertEqual(self.hub.state["jobs"][job["id"]]["status"], "ready")

    def test_preview_uses_same_remote_pipeline(self):
        job = self.create(kind="preview", time_s=1.5, result_name="preview.png")
        self.worker.run_one(job)
        self.assertEqual(self.hub.state["jobs"][job["id"]]["result"]["finish"], "glow")
        output = self.folder / "preview.png"
        RemoteRunner(self.plugin, self.target(job["request_id"]), kind="preview", time_s=1.5).process(
            str(self.source), self.settings, str(output))
        self.assertTrue(output.read_bytes().startswith(b"PREVIEW:"))
        self.worker.step()

    def test_real_ffmpeg_engine_pause_power_restart_and_frame_equality(self):
        from upscaler import tools
        import subprocess
        try:
            ff = tools.find_tool("ffmpeg")
        except tools.ToolMissing:
            self.skipTest("FFmpeg absent (le protocole reste testé sans GPU)")
        tools.run([ff, "-v", "error", "-y", "-f", "lavfi", "-i",
                   "testsrc2=size=96x64:rate=10:duration=2", "-pix_fmt", "yuv420p", str(self.source)],
                  check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        settings = engine.Settings(output_size="x1", interpolation="x2", gpu="-1", finish="auto", clean="off")
        job = self.create(settings=settings.to_dict(), gpu="-1")
        original_plan = engine.make_plan

        def small_plan(info, st):
            plan = original_plan(info, st)
            plan.batch_size = 4
            return plan

        class PauseFirst(engine.Upscaler):
            def process(self, *args, on_state=None, **kw):
                def checkpoint(state):
                    on_state(state)
                    if state.get("src_done", 0) >= 6:
                        self.pause()
                return super().process(*args, on_state=checkpoint, **kw)

        config = {"url": self.url, "pin": self.pin, "token": self.identity["token"]}
        with mock.patch.object(engine, "make_plan", small_plan), mock.patch.object(engine, "SEG_MAX_FRAMES", 6):
            first = Worker(self.folder / "worker", config, factory=PauseFirst)
            first.run_one(job)
            if "RIFE" in self.hub.state["jobs"][job["id"]].get("error", ""):
                self.skipTest("RIFE CPU indisponible sur cette machine")
            self.assertEqual(self.hub.state["jobs"][job["id"]]["status"], "paused", self.hub.state["jobs"][job["id"]].get("error"))
            saved = json_read(self.folder / "worker" / job["id"] / "worker.json")
            self.assertGreater(saved["checkpoint"]["src_done"], 0)
            # Nouvel objet exécutant, comme après un arrêt du PC.
            second = Worker(self.folder / "worker", config)
            second.run_one(self.controller.get(f"/v1/jobs/{job['id']}"))
            remote = self.controller.get(f"/v1/jobs/{job['id']}")
            self.assertEqual(remote["status"], "ready", remote.get("error"))
            reference = self.folder / "reference.mp4"
            engine.Upscaler(on_log=lambda s: None).process(str(self.source), settings, str(reference))
        remote_file = self.folder / "worker" / job["id"] / "result.mp4"

        def frames(path):
            return tools.run([ff, "-v", "error", "-i", str(path), "-f", "framemd5", "-"],
                             check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout
        self.assertEqual(frames(reference), frames(remote_file))
        output = self.folder / "received.mp4"
        RemoteRunner(self.plugin, self.target(job["request_id"])).process(str(self.source), settings, str(output))
        self.worker.step()
        self.assertFalse((self.folder / "worker" / job["id"]).exists())

    def test_real_queue_dispatches_to_remote_and_keeps_original_snapshot(self):
        from upscaler.webapp import Api
        with mock.patch.dict(os.environ, {"UPSCALER_DATA_DIR": str(self.folder / "app-data"),
                                         "APPDATA": str(self.folder / "app-config")}), \
             mock.patch.object(Api, "_detect_gpus"), mock.patch.object(Api, "_load_settings", return_value=self.settings):
            api = Api()
            info = engine.VideoInfo(str(self.source), 32, 24, "25/1", 25, 1, 25, None,
                                    "progressive", "bt709", "yuv420p", "h264")
            file = {"id": 1, "name": self.source.name, "path": str(self.source), "status": "Prêt", "state": "ready",
                    "progress": 0, "res": "32×24", "dur": "00:00:01", "out": "64×48", "size": self.source.stat().st_size,
                    "output": "", "error": "", "thumb": "", "info": info, "kind": "video", "fps": 25, "job": None}
            target = self.target()
            fake_plugin = self.plugin

            class QueuePlugin:
                def selected(self):
                    return dict(target)

                def runner(self, target, **kw):
                    return RemoteRunner(fake_plugin, target, **kw)

                def abandon(self, job):
                    fake_plugin.command(job.id, "cancel")

            api._remote = QueuePlugin()
            api._files, api._order, api._next_id = {1: file}, [1], 2
            # Sortie locale de l'UI ; jamais envoyée comme chemin au PC GPU.
            api._settings.output_dir = str(self.folder / "outputs")
            api._settings.temp_dir = ""
            api._remote_cache_at = time.monotonic()  # ne pas demander de statut au faux plugin
            self.assertTrue(api.start())
            snapshot = copy.deepcopy(file["job"].data)
            api.set_settings({"output_size": "x4", "finish": "dore", "interp_fps": 120})
            stop = threading.Event()

            def work():
                while not stop.wait(0.02):
                    self.worker.step()

            thread = threading.Thread(target=work, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 15
                while api._running and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertFalse(api._running)
                self.assertEqual(file["state"], "done", file.get("error"))
                self.assertEqual(snapshot["remote"]["worker"], self.identity["id"])
                self.assertEqual(snapshot["settings"]["finish"], "auto")
                self.assertEqual(FakeUpscaler.calls[-1]["finish"], "auto")
                self.assertEqual(FakeUpscaler.calls[-1]["output_size"], "x2")
                self.assertEqual(FakeUpscaler.calls[-1]["interp_fps"], 60)
                self.assertTrue(Path(file["output"]).exists())
                self.assertTrue(self.source.exists())
            finally:
                stop.set()
                thread.join(timeout=5)
                api._frame_pool.shutdown(wait=False)

    def test_compromised_relay_cannot_make_worker_write_or_delete_arbitrary_paths(self):
        job = self.create()
        sentinel = self.folder / "do-not-delete.txt"
        sentinel.write_text("keep")
        for bad in ("../", str(self.folder), "../do-not-delete.txt", "x" * 32):
            forged = dict(job, id=bad, status="delivered")
            with self.assertRaises(ProtocolError):
                self.worker.run_one(forged)
            with self.assertRaises(ProtocolError):
                self.worker._cleanup(forged)
        for values in ({"source_name": "file.exe"}, {"result_name": "result.cmd"},
                       {"source_size": -1}, {"kind": "shell"}, {"time_s": float("inf")}):
            with self.assertRaises(ProtocolError):
                self.worker.run_one(dict(job, **values))
        self.assertEqual(sentinel.read_text(), "keep")

    def test_changed_original_and_malformed_paths_rejected(self):
        job = self.create()
        self.source.write_bytes(b"modified")
        with self.assertRaises(ProtocolError):
            self.worker_client.request("GET", f"/v1/jobs/{job['id']}/source?offset=0", raw=True)
        with self.assertRaises(ProtocolError):
            self.worker_client.get("/v1/jobs/../../etc/passwd")


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"UPSCALER_DATA_DIR": self.tmp.name})
        self.env.start()
        self.manifest, self.raw = package(ROOT / "extensions/remote-compute")
        self.info = {**self.manifest, "size": len(self.raw), "sha256": hashlib.sha256(self.raw).hexdigest()}
        self.manager = ExtensionManager()

    def tearDown(self):
        self.manager._unload("remote-compute")
        self.env.stop()
        self.tmp.cleanup()

    def test_install_is_optional_and_load_checks_integrity(self):
        self.assertIsNone(self.manager.load())
        self.manager._install_verified(self.raw, self.info)
        plugin = self.manager.load()
        self.assertFalse(plugin.public()["enabled"])
        self.assertFalse(plugin.public()["startup"])
        self.assertEqual(plugin.public()["role"], "")
        self.manager._unload("remote-compute")
        path = Path(self.tmp.name, "extensions/remote-compute/upv_remote/__init__.py")
        path.write_text("raise RuntimeError('untrusted')")
        with self.assertRaises(ExtensionError):
            self.manager.load()

    def test_repair_corruption_and_recover_interrupted_install(self):
        self.manager._install_verified(self.raw, self.info)
        self.manager._unload("remote-compute")
        folder = Path(self.tmp.name, "extensions/remote-compute")
        (folder / "upv_remote/__init__.py").write_text("raise RuntimeError('must not execute')")
        self.manager._install_verified(self.raw, self.info)
        self.assertFalse(self.manager.load().public()["enabled"])
        self.manager._unload("remote-compute")
        folder.rename(folder.with_name("remote-compute.previous"))
        recovery = ExtensionManager()
        self.assertIsNotNone(recovery.load())
        recovery._unload("remote-compute")

    def test_zip_slip_digest_and_duplicates_rejected(self):
        with self.assertRaises(ExtensionError):
            self.manager._install_verified(self.raw + b"x", self.info)
        for name in ("../outside.py", "/absolute.py", "upv_remote/../../bad.py", "upv_remote\\bad.py"):
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, "w") as z:
                z.writestr(name, b"bad")
            raw = stream.getvalue()
            info = dict(self.info, size=len(raw), sha256=hashlib.sha256(raw).hexdigest())
            with self.assertRaises(ExtensionError):
                self.manager._install_verified(raw, info)
        self.assertFalse(Path(self.tmp.name, "outside.py").exists())

    def test_headless_service_process_lifecycle(self):
        import socket
        self.manager._install_verified(self.raw, self.info)
        plugin = self.manager.load()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        startup_patch = mock.patch(type(plugin).__module__ + ".set_startup")
        startup_patch.start()
        self.addCleanup(startup_patch.stop)
        try:
            state = plugin.configure_controller({"port": port, "startup": False})
            self.assertTrue(state["running"])
            public = plugin.controller().get("/v1/controller/state")
            self.assertEqual(public["workers"], [])
            self.assertNotIn("token", plugin.public())
            # Deux appels de démarrage ne créent pas deux serveurs ni n'arrêtent le premier.
            plugin.start()
            plugin.ensure_running()
            self.assertTrue(plugin.public()["running"])
        finally:
            plugin.stop()
        self.assertFalse(plugin.public()["running"])
        plugin.start()
        self.assertTrue(plugin.public()["running"])
        plugin.stop()

    def test_private_storage_round_trip(self):
        path = Path(self.tmp.name, "private.dat")
        save_private(path, {"secret": "test", "unicode": "é"})
        self.assertEqual(load_private(path), {"secret": "test", "unicode": "é"})
        if os.name == "nt":
            self.assertNotIn(b"test", path.read_bytes())
        else:
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
