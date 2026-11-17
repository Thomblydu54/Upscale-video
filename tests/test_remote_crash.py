"""Pannes du principal : vrais processus tués, HTTPS tronqué et calcul autonome sans GPU."""
from __future__ import annotations

import copy
import hashlib
import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

import test_remote as protocol
from upscaler import engine
from upscaler.extension_runtime import atomic_json, read_json, tls_identity
from upscaler.jobs import JobStore
from upscaler.webapp import Api
from upv_remote_tests.hub import Handler


def wait_until(check, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.03)
    raise AssertionError("Le scénario de panne n'a pas atteint l'état attendu.")


class OfflineWorkerTests(unittest.TestCase):
    setUp = protocol.RemoteTests.setUp
    tearDown = protocol.RemoteTests.tearDown
    create = protocol.RemoteTests.create
    target = protocol.RemoteTests.target

    def stop_relay(self):
        self.port = self.server.server_port
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def restart_relay(self):
        self.hub = protocol.Hub(self.hub.folder, self.master, self.pin)
        self.plugin.hub = self.hub
        self.server = protocol.Server("127.0.0.1", self.port, self.hub,
                                      self.folder / "tls.crt", self.folder / "tls.key")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def prepared_source(self, job):
        folder = self.worker.folder / job["id"]
        folder.mkdir(parents=True)
        source = folder / ("input-" + job["id"] + ".mp4")
        source.write_bytes(self.source.read_bytes())
        return folder, source

    def test_loss_just_after_last_source_chunk_still_computes_before_reconnection(self):
        job = self.create()
        original = self.worker.client.request

        def request(method, path, *args, **kw):
            content = original(method, path, *args, **kw)
            if method == "GET" and "/source?offset=" in path:
                offset = int(path.rsplit("=", 1)[1])
                if offset + len(content) == job["source_size"]:
                    self.stop_relay()
                    time.sleep(1.05)  # fait aussi tomber l'ancien report de fin de transfert
            return content

        with mock.patch.object(self.worker.client, "request", side_effect=request):
            with self.assertRaises(OSError):  # calcul fini ; seul le retour est impossible
                self.worker.run_one(job)
        spool = self.worker.folder / job["id"]
        expected = b"RESULTAT:" + self.source.read_bytes()
        self.assertEqual((spool / "result.mp4").read_bytes(), expected)
        self.assertEqual(read_json(spool / "worker.json")["phase"], "uploading")
        self.assertEqual(len(protocol.FakeUpscaler.calls), 1)
        self.assertEqual(self.hub.state["jobs"][job["id"]]["desired"], "run")
        self.restart_relay()
        self.worker.run_one(self.controller.get(f"/v1/jobs/{job['id']}"))
        output = self.folder / "recovered.mp4"
        protocol.RemoteRunner(self.plugin, self.target(job["request_id"])).process(
            str(self.source), self.settings, str(output))
        self.worker.step()
        self.assertEqual(output.read_bytes(), expected)
        self.assertEqual(len(protocol.FakeUpscaler.calls), 1)
        self.assertFalse(spool.exists())
        self.assertTrue(self.source.exists())

    def test_offline_does_not_override_known_pause_cancel_error_or_owner_stop(self):
        cases = []
        for desired, status in (("pause", "running"), ("cancel", "cancelled"), ("run", "error")):
            job = self.create()
            job.update(desired=desired, status=status)
            folder, _ = self.prepared_source(job)
            cases.append((job, folder))
        self.stop_relay()
        for job, folder in cases:
            with self.subTest(desired=job["desired"], status=job["status"]):
                self.worker.run_one(job)
                self.assertTrue(folder.exists())
                self.assertFalse((folder / "result.mp4").exists())
        self.worker.stop.set()
        job = dict(cases[0][0], desired="run", status="running")
        self.worker.run_one(job)
        self.assertFalse(protocol.FakeUpscaler.calls)

    def test_security_rejection_is_not_a_network_outage_permission(self):
        job = self.create()
        self.prepared_source(job)
        with mock.patch.object(self.worker, "_report", side_effect=protocol.ProtocolError("Certificat refusé", 495)):
            with self.assertRaises(protocol.ProtocolError) as error:
                self.worker.run_one(job)
        self.assertEqual(error.exception.status, 495)
        self.assertFalse(protocol.FakeUpscaler.calls)

    def test_incomplete_control_json_is_a_retryable_protocol_failure_not_keyerror(self):
        job = self.create()
        with mock.patch.object(self.worker.client, "post", return_value={"status": "running"}):
            with self.assertRaises(protocol.ProtocolError) as error:
                self.worker._report(job, "running", {})
        self.assertGreaterEqual(error.exception.status, 500)

    def test_active_certificate_or_authorization_refusal_checkpoints_and_pauses(self):
        job = self.create()
        self.prepared_source(job)
        instances = []

        class Blocking(protocol.FakeUpscaler):
            def __init__(self, **kw):
                super().__init__(**kw)
                instances.append(self)

            def process(self, source, settings, output, job_dir=None, state=None, on_state=None):
                on_state({"fake_done": 4})
                if not self._pause.wait(4):
                    raise AssertionError("Le calcul n'a pas été mis en pause après refus TLS.")
                raise engine.Paused()

        reports = 0
        original = self.worker._report

        def refuse_after_start(job, status, *args, **kw):
            nonlocal reports
            reports += 1
            if reports > 1:
                raise protocol.ProtocolError("Certificat refusé", 495)
            return original(job, status, *args, **kw)

        self.worker.factory = Blocking
        with mock.patch.object(self.worker, "_report", side_effect=refuse_after_start):
            self.worker.run_one(job)
        self.assertGreaterEqual(reports, 2)
        self.assertTrue(instances[0]._pause.is_set())
        self.assertEqual(read_json(self.worker.folder / job["id"] / "worker.json")["checkpoint"]["fake_done"], 4)
        self.assertFalse((self.worker.folder / job["id"] / "result.mp4").exists())

    def test_partial_source_waits_instead_of_computing_corrupt_input(self):
        job = self.create()
        original = self.worker.client.request
        received = threading.Event()

        def request(method, path, *args, **kw):
            content = original(method, path, *args, **kw)
            if method == "GET" and "/source?offset=" in path and not received.is_set():
                received.set()
                self.stop_relay()
            return content

        with mock.patch.object(self.worker.client, "request", side_effect=request):
            with self.assertRaises(OSError):
                self.worker.run_one(job)
        spool = self.worker.folder / job["id"]
        self.assertEqual((spool / "input.part").stat().st_size, protocol.CHUNK)
        self.assertFalse((spool / "result.mp4").exists())
        self.assertFalse(protocol.FakeUpscaler.calls)
        self.restart_relay()
        self.worker.run_one(self.controller.get(f"/v1/jobs/{job['id']}"))
        self.assertEqual((spool / "result.mp4").read_bytes(), b"RESULTAT:" + self.source.read_bytes())
        self.assertEqual(len(protocol.FakeUpscaler.calls), 1)

    def test_truncated_control_response_keeps_monitor_alive_until_explicit_pause(self):
        job = self.create()
        entered, release, truncated = threading.Event(), threading.Event(), threading.Event()
        errors = []
        original_send = Handler._send

        class Blocking(protocol.FakeUpscaler):
            def process(self, source, settings, output, job_dir=None, state=None, on_state=None):
                on_state({"fake_done": 3})
                entered.set()
                while not release.wait(0.01):
                    if self._pause.is_set():
                        raise engine.Paused()
                return super().process(source, settings, output, job_dir, state, on_state)

        def send(handler, data, status=200, binary=False):
            if (handler.path.endswith("/report") and isinstance(data, dict)
                    and data.get("status") == "running" and entered.is_set() and not truncated.is_set()):
                raw = json.dumps(data).encode()
                handler.send_response(200)
                handler.send_header("Content-Length", str(len(raw)))
                handler.send_header("Connection", "close")
                handler.end_headers()
                handler.wfile.write(raw[:5])  # comme une alimentation coupée au milieu de la réponse
                handler.wfile.flush()
                handler.close_connection = True
                truncated.set()
                return
            return original_send(handler, data, status, binary)

        def work():
            try:
                self.worker.run_one(job)
            except Exception as error:
                errors.append(error)

        self.worker.factory = Blocking
        with mock.patch.object(Handler, "_send", send):
            thread = threading.Thread(target=work, daemon=True)
            thread.start()
            try:
                self.assertTrue(entered.wait(8))
                self.assertTrue(truncated.wait(8))
                self.controller.post(f"/v1/jobs/{job['id']}/control", {"desired": "pause"})
                thread.join(timeout=6)
                self.assertFalse(thread.is_alive(), "Le moniteur n'a pas survécu à la réponse tronquée.")
            finally:
                release.set()
                self.worker.stop.set()
                thread.join(timeout=10)
        self.assertFalse(errors, errors)
        self.assertEqual(self.hub.state["jobs"][job["id"]]["status"], "paused")
        self.assertEqual(read_json(self.worker.folder / job["id"] / "worker.json")["checkpoint"]["fake_done"], 3)
        self.assertFalse((self.worker.folder / job["id"] / "result.mp4").exists())

    def test_abandoned_before_submission_is_cancelled_not_stuck_paused_and_new_job_works(self):
        request_id = uuid.uuid4().hex
        self.hub.command_request(request_id, "cancel", time.time_ns())
        target = self.target(request_id)

        class LocalPlugin:
            def ensure_running(self):
                pass

            def controller(inner, pin=None):
                return self.controller

            def command(inner, request_id, desired):
                self.hub.command_request(request_id, desired, time.time_ns())

        runner = protocol.RemoteRunner(LocalPlugin(), target)
        with self.assertRaises(engine.Cancelled):
            runner.process(str(self.source), self.settings, str(self.folder / "abandoned.mp4"))
        self.assertFalse(self.hub.state["jobs"])
        self.assertFalse(protocol.FakeUpscaler.calls)
        fresh = self.create()  # une nouvelle demande ne doit pas hériter de l'abandon précédent
        self.worker.run_one(fresh)
        self.assertEqual(self.hub.state["jobs"][fresh["id"]]["status"], "ready")
        self.assertEqual(len(protocol.FakeUpscaler.calls), 1)

    def test_http_disconnect_before_status_is_retryable_not_a_dead_monitor(self):
        with mock.patch("http.client.HTTPSConnection.getresponse", side_effect=http.client.BadStatusLine("coupure")):
            with self.assertRaises(ConnectionError):
                self.controller.get("/v1/controller/state")

    def test_truncated_body_and_invalid_json_are_normalized(self):
        certificate = b"test pinned certificate"
        client = protocol.Client("https://relay.invalid", hashlib.sha256(certificate).hexdigest())

        class Socket:
            def getpeercert(self, binary_form=False):
                return certificate

        class Response:
            status = 200

            def __init__(self, body, length, status=200):
                self.body, self.length, self.status = body, length, status

            def read(self, maximum):
                return self.body[:maximum]

        class Connection:
            def __init__(self, response):
                self.sock, self.response = Socket(), response

            def connect(self):
                pass

            def request(self, *args, **kwargs):
                pass

            def getresponse(self):
                return self.response

            def close(self):
                pass

        with mock.patch("http.client.HTTPSConnection", return_value=Connection(Response(b"{\"x\"", 10))):
            with self.assertRaises(ConnectionError):
                client.get("/state")
        for body, status, expected in ((b"{invalid", 200, 502), (b"[]", 200, 502), (b"[]", 403, 403)):
            with self.subTest(body=body, status=status), mock.patch(
                    "http.client.HTTPSConnection", return_value=Connection(Response(body, len(body), status))):
                with self.assertRaises(protocol.ProtocolError) as error:
                    client.get("/state")
                self.assertEqual(error.exception.status, expected)

    def test_received_pause_is_applied_before_a_failing_heartbeat(self):
        job = self.create()
        self.prepared_source(job)
        received_pause = threading.Event()
        original_report = self.worker._report
        instances = []

        def report(job, status, *args, **kw):
            value = original_report(job, status, *args, **kw)
            if status == "running" and instances:
                received_pause.set()
                return dict(value, desired="pause")
            return value

        class Blocking(protocol.FakeUpscaler):
            def __init__(self, **kw):
                super().__init__(**kw)
                instances.append(self)

            def process(self, source, settings, output, job_dir=None, state=None, on_state=None):
                on_state({"fake_done": 3})
                if not self._pause.wait(4):
                    raise AssertionError("Pause reçue mais perdue avant le heartbeat")
                raise engine.Paused()

        self.worker.factory = Blocking
        with mock.patch.object(self.worker, "_report", side_effect=report), \
             mock.patch.object(self.worker.client, "post", wraps=self.worker.client.post) as post:
            # Le prochain ping échouerait : il ne doit pas être nécessaire pour appliquer la pause.
            original_post = post._mock_wraps
            post.side_effect = lambda route, *args, **kw: (_ for _ in ()).throw(ConnectionError()) if route == "/v1/worker/ping" else original_post(route, *args, **kw)
            self.worker.run_one(job)
        self.assertTrue(received_pause.is_set())
        self.assertTrue(instances[0]._pause.is_set())
        self.assertEqual(self.hub.state["jobs"][job["id"]]["status"], "paused")


class AbruptControllerTests(unittest.TestCase):
    setUp = protocol.PackageTests.setUp
    tearDown = protocol.PackageTests.tearDown

    def exercise_process_death(self, kill_relay):
        folder = Path(self.tmp.name)
        appdata = mock.patch.dict(os.environ, {"APPDATA": str(folder / "app-config")})
        appdata.start()
        self.addCleanup(appdata.stop)
        self.manager._install_verified(self.raw, self.info)
        plugin = self.manager.load()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        pin = tls_identity(plugin.folder)
        master = protocol.token()
        url = f"https://127.0.0.1:{port}"
        plugin._save({"role": "controller", "enabled": True, "port": port, "url": url,
                      "pin": pin, "token": master, "startup": False, "revision": "abrupt-test"})
        markers = folder / "interface"
        markers.mkdir()
        source = folder / "source.mp4"
        source.write_bytes(os.urandom(protocol.CHUNK + 31))
        settings = engine.Settings(output_size="x2", interpolation="custom", interp_fps=120, finish="auto",
                                   output_dir=str(folder / "outputs"), temp_dir="", auto_update=False)
        entered, release = threading.Event(), threading.Event()
        instances, errors = [], []
        protocol.FakeUpscaler.calls.clear()

        class Blocking(protocol.FakeUpscaler):
            def __init__(self, **kw):
                super().__init__(**kw)
                instances.append(self)

            def process(self, source, settings, output, job_dir=None, state=None, on_state=None):
                on_state(dict(state or {}, fake_done=3))
                entered.set()
                while not release.wait(0.01):
                    if self._pause.is_set():
                        raise engine.Paused()
                    if self._cancel.is_set():
                        raise engine.Cancelled()
                return super().process(source, settings, output, job_dir, state, on_state)

        worker = worker_thread = reopened = None
        with (markers / "process.log").open("wb") as log:
            process = subprocess.Popen([sys.executable, str(Path(__file__).with_name("remote_crash_controller.py")), str(markers)],
                                       cwd=protocol.ROOT, stdout=log, stderr=log)
            try:
                def ready():
                    error = read_json(markers / "error.json")
                    if error:
                        raise AssertionError(error["error"])
                    if process.poll() is not None:
                        raise AssertionError((markers / "process.log").read_text(encoding="utf-8", errors="replace"))
                    return (markers / "ready.json").exists()
                wait_until(ready, timeout=35)
                service_pid = read_json(markers / "ready.json")["service"]
                self.assertNotIn(service_pid, (os.getpid(), process.pid))
                controller = protocol.Client(url, pin, master)
                pair = controller.post("/v1/controller/pair")
                identity = protocol.Client(url, pin).post("/v1/pair", {"secret": pair["secret"], "name": "GPU panne"})
                wait_until(lambda: any(w["id"] == identity["id"] for w in plugin.public()["workers"]))
                plugin.select(identity["id"])
                worker = protocol.Worker(folder / "gpu", {"url": url, "pin": pin, "token": identity["token"],
                    "worker_id": identity["id"], "gpu": "auto"}, factory=Blocking)
                worker.detect = lambda: None
                worker_thread = threading.Thread(target=worker.run, daemon=True)
                worker_thread.start()
                atomic_json(markers / "launch.json", {"source": str(source), "settings": settings.to_dict()})
                self.assertTrue(entered.wait(15), read_json(markers / "error.json"))
                wait_until(lambda: (markers / "running.json").exists())
                initial = read_json(markers / "running.json")
                job_path = folder / "jobs" / initial["job"] / "job.json"
                saved = read_json(job_path)
                jid = saved["state"]["remote_id"]
                snapshot = copy.deepcopy(saved["settings"])
                self.assertEqual(snapshot, initial["settings"])
                process.kill()  # SIGKILL / TerminateProcess : absolument aucun hook Python/natif
                process.wait(timeout=10)
                self.assertNotEqual(process.returncode, 0)
                self.assertEqual(read_json(job_path)["status"], "running")  # aucun finally de file exécuté
                self.assertTrue(plugin.public()["running"], "Le service doit survivre à son lanceur tué.")
                self.assertEqual(controller.get(f"/v1/jobs/{jid}")["desired"], "run")
                if kill_relay:
                    os.kill(service_pid, signal.SIGTERM)  # simule aussi la perte de toutes les fonctions du principal
                    wait_until(lambda: not plugin.public()["running"])
                    with self.assertRaises(OSError):
                        controller.get("/v1/controller/state")
                    wait_until(lambda: "inaccessible" in worker.status)
                self.assertFalse(instances[0]._pause.is_set())
                self.assertFalse(instances[0]._cancel.is_set())
                release.set()
                spool = folder / "gpu" / jid
                if kill_relay:
                    wait_until(lambda: read_json(spool / "worker.json").get("phase") == "uploading")
                    self.assertEqual((spool / "result.mp4").read_bytes(), b"RESULTAT:" + source.read_bytes())
                    self.assertTrue(spool.exists())
                    plugin.ensure_running()  # retour du principal, même identité/TLS/stockage
                wait_until(lambda: controller.get(f"/v1/jobs/{jid}")["status"] == "ready", timeout=25)
                self.assertTrue(spool.exists())  # résultat conservé en l'absence de reçu du contrôleur tué
                for command in (plugin.folder / "commands").glob("*.json"):
                    self.assertEqual(read_json(command)["desired"], "run")
                with mock.patch.object(Api, "_detect_gpus"), mock.patch.object(Api, "_probe"), \
                     mock.patch.object(Api, "_load_settings", return_value=engine.Settings(output_size="x4")):
                    reopened = Api()
                file = next(iter(reopened._files.values()))
                file["info"] = engine.VideoInfo(str(source), 32, 24, "25/1", 25, 1, 25, None,
                                                "progressive", "bt709", "yuv420p", "h264")
                self.assertEqual(file["job"].data["settings"], snapshot)
                self.assertEqual(file["job"].data["state"]["remote_id"], jid)
                reopened._auto_resume()
                wait_until(lambda: not reopened._running, timeout=25)
                self.assertEqual(file["state"], "done", file.get("error"))
                self.assertEqual(Path(file["output"]).read_bytes(), b"RESULTAT:" + source.read_bytes())
                wait_until(lambda: controller.get(f"/v1/jobs/{jid}")["status"] == "cleaned")
                self.assertFalse(spool.exists())
                self.assertEqual(len(protocol.FakeUpscaler.calls), 1)
                self.assertTrue(source.exists())
                self.assertFalse(JobStore().load_pending())
                self.assertFalse(errors, errors)
            finally:
                release.set()
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=10)
                if reopened:
                    if reopened._running:
                        reopened.pause()
                        wait_until(lambda: not reopened._running)
                    reopened._frame_pool.shutdown(wait=False)
                if worker:
                    worker.stop.set()
                if worker_thread:
                    worker_thread.join(timeout=15)
                plugin.stop()

    def test_killing_interface_process_keeps_its_detached_service_and_calculation_alive(self):
        self.exercise_process_death(kill_relay=False)

    def test_killing_interface_and_relay_finishes_offline_then_recovers_without_recompute(self):
        self.exercise_process_death(kill_relay=True)


class DurableRemoteQueueTests(unittest.TestCase):
    setUp = protocol.PackageTests.setUp
    tearDown = protocol.PackageTests.tearDown

    def test_remote_job_requires_a_durable_manifest_before_dispatch(self):
        source = Path(self.tmp.name, "source.mp4")
        source.write_bytes(b"SOURCE")
        remote = {"worker": "a" * 32, "name": "GPU", "pin": "b" * 64}
        with mock.patch("upscaler.extension_runtime.durable_replace", side_effect=OSError("Disque non synchronisé")):
            with self.assertRaises(OSError):
                JobStore().create(str(source), str(source.with_name("out.mp4")), engine.Settings(), remote=remote)
        self.assertFalse(JobStore().load_pending())

    def test_remote_state_write_failure_is_not_silently_acknowledged(self):
        source = Path(self.tmp.name, "source.mp4")
        source.write_bytes(b"SOURCE")
        job = JobStore().create(str(source), str(source.with_name("out.mp4")), engine.Settings(),
                                remote={"worker": "a" * 32, "name": "GPU", "pin": "b" * 64})
        before = read_json(Path(job.folder) / "job.json")
        with mock.patch("upscaler.extension_runtime.durable_replace", side_effect=OSError("Disque plein")):
            with self.assertRaises(OSError):
                JobStore().update(job, state={"remote_id": "c" * 32}, strict=True)
        self.assertEqual(read_json(Path(job.folder) / "job.json"), before)
        self.assertTrue(source.exists())


if __name__ == "__main__":
    unittest.main()
