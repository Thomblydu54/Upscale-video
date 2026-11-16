"""Choix de fermeture : aucune pause implicite après « Continuer », reprise durable."""
from __future__ import annotations

import copy
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import test_remote as protocol
from upscaler import engine
from upscaler.extension_runtime import ExtensionError, RemoteDetached, read_json
from upscaler.webapp import Api


def wait_until(check, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.02)
    raise AssertionError("Le changement d'état attendu n'a pas eu lieu.")


class ClosingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"UPSCALER_DATA_DIR": self.tmp.name, "APPDATA": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        detect = mock.patch.object(Api, "_detect_gpus")
        detect.start()
        self.addCleanup(detect.stop)
        self.api = Api()
        self.addCleanup(self.api._frame_pool.shutdown, wait=False)
        self.api._window = mock.Mock()
        # destroy() provoque réellement un deuxième événement natif de fermeture.
        self.api._window.destroy.side_effect = lambda: self.assertIsNone(self.api._on_closing())
        self.remote = mock.Mock()
        self.remote.public.return_value = {"role": "controller", "running": True, "pending": 0}
        self.api._remote = self.remote

    def add_job(self, remote=True, state="running", fid=1):
        source = Path(self.tmp.name, f"source-{fid}.mp4")
        source.write_bytes(b"SOURCE")
        target = {"worker": "a" * 32, "name": "PC GPU", "pin": "b" * 64} if remote else None
        job = self.api._jobs.create(str(source), str(source.with_name(f"result-{fid}.mp4")),
                                    engine.Settings(output_size="x2"), remote=target)
        self.api._files[fid] = {"id": fid, "name": source.name, "state": state, "progress": 0.3, "job": job}
        self.api._order.append(fid)
        return job

    def active_runner(self, detachable=True):
        up = mock.Mock(spec=["pause", "cancel"] + (["detach"] if detachable else []))
        def done():
            self.api._running = False
        up.pause.side_effect = done
        up.cancel.side_effect = done
        if detachable:
            up.detach.side_effect = done
        self.api._running, self.api._task, self.api._upscaler = True, "queue", up
        return up

    def test_prompt_cancel_then_continue_does_not_pause_or_cancel(self):
        self.add_job()
        up = self.active_runner()
        self.assertIs(self.api._on_closing(), False)
        self.assertEqual(self.api._events[-1]["type"], "close_requested")
        self.assertTrue(self.api._events[-1]["can_continue"])
        up.detach.assert_not_called()
        up.pause.assert_not_called()
        self.assertTrue(self.api.close_application("cancel")["ok"])
        self.assertTrue(self.api._running)
        self.api._window.destroy.assert_not_called()
        self.assertIs(self.api._on_closing(), False)
        self.assertTrue(self.api.close_application("continue")["ok"])
        wait_until(lambda: self.api._window.destroy.called)
        up.detach.assert_called_once()
        up.pause.assert_not_called()
        up.cancel.assert_not_called()
        self.remote.command.assert_not_called()
        self.remote.stop.assert_not_called()

    def test_close_pause_spools_pause_and_keeps_job(self):
        job = self.add_job()
        up = self.active_runner()
        self.assertIs(self.api._on_closing(), False)
        self.assertTrue(self.api.close_application("pause")["ok"])
        wait_until(lambda: self.api._window.destroy.called)
        up.pause.assert_called_once()
        up.detach.assert_not_called()
        up.cancel.assert_not_called()
        self.remote.command.assert_called_with(job.data["remote"]["request_id"], "pause")
        self.remote.abandon.assert_not_called()
        self.assertTrue(Path(job.folder).exists())

    def test_local_processing_still_pauses_and_never_detaches(self):
        self.add_job(remote=False)
        up = self.active_runner(detachable=False)
        self.assertFalse(self.api.close_application("continue")["ok"])
        self.assertIs(self.api._on_closing(), False)
        wait_until(lambda: self.api._window.destroy.called)
        up.pause.assert_called_once()
        up.cancel.assert_not_called()
        self.remote.command.assert_not_called()

    def test_old_extension_cannot_fake_continuation(self):
        self.add_job()
        up = self.active_runner(detachable=False)
        self.assertIs(self.api._on_closing(), False)
        self.assertFalse(self.api._events[-1]["can_continue"])
        self.assertFalse(self.api.close_application("continue")["ok"])
        self.api._window.destroy.assert_not_called()
        up.pause.assert_not_called()

    def test_background_worker_continue_keeps_service_running(self):
        self.remote.public.return_value = {"role": "worker", "running": True}
        self.assertIs(self.api._on_closing(), False)
        self.assertTrue(self.api.close_application("continue")["ok"])
        wait_until(lambda: self.api._window.destroy.called)
        self.remote.stop.assert_not_called()
        self.remote.command.assert_not_called()

    def test_background_worker_pause_preserves_startup_opt_in(self):
        self.remote.public.return_value = {"role": "worker", "running": True}
        self.assertIs(self.api._on_closing(), False)
        self.assertTrue(self.api.close_application("pause")["ok"])
        wait_until(lambda: self.api._window.destroy.called)
        self.remote.stop.assert_called_once_with(keep_enabled=True)
        self.remote.set_startup.assert_not_called()

    def test_stop_failure_keeps_window_open(self):
        self.remote.public.return_value = {"role": "worker", "running": True}
        self.remote.stop.side_effect = ExtensionError("Checkpoint toujours en cours")
        self.assertTrue(self.api.close_application("pause")["ok"])
        wait_until(lambda: any(e["type"] == "close_failed" for e in self.api._events))
        self.api._window.destroy.assert_not_called()
        self.assertFalse(self.api._closing_ok)
        self.assertFalse(self.api._closing_in_progress)

    def test_detachment_does_not_launch_next_local_job(self):
        remote_job = self.add_job(state="queued")
        local_job = self.add_job(remote=False, state="queued", fid=2)
        up = mock.Mock()
        up.process.side_effect = RemoteDetached()
        self.remote.runner.return_value = up
        self.api._running, self.api._task = True, "queue"
        with mock.patch("upscaler.webapp.Upscaler") as local:
            self.api._run_jobs()
        local.assert_not_called()
        for job, fid in ((remote_job, 1), (local_job, 2)):
            self.assertEqual(self.api._files[fid]["state"], "paused")
            self.assertTrue(Path(job.folder).exists())
        self.remote.abandon.assert_not_called()

    def test_repeated_close_events_and_double_choice_are_idempotent(self):
        self.add_job()
        up = self.active_runner()
        for _ in range(3):
            self.assertIs(self.api._on_closing(), False)
        self.assertEqual(sum(e["type"] == "close_requested" for e in self.api._events), 1)
        self.assertTrue(self.api.close_application("continue")["ok"])
        self.assertFalse(self.api.close_application("pause")["ok"])
        self.assertFalse(self.api.close_application("cancel")["ok"])
        self.assertFalse(self.api.start())
        self.assertFalse(self.api.resume())
        self.assertFalse(self.api.preview())
        self.assertFalse(self.api.clear_files())
        self.assertFalse(self.api.remove_file(1))
        wait_until(lambda: self.api._window.destroy.called)
        up.detach.assert_called_once()
        up.pause.assert_not_called()
        up.cancel.assert_not_called()
        self.api._window.destroy.assert_called_once()
        self.remote.command.assert_not_called()

    def test_finished_job_between_prompt_and_choice_is_not_recreated(self):
        job = self.add_job()
        up = self.active_runner()
        self.assertIs(self.api._on_closing(), False)
        self.api._jobs.delete(job)
        self.api._files[1].update(state="done", job=None)
        self.api._running, self.api._task, self.api._upscaler = False, "", None
        self.assertTrue(self.api.close_application("continue")["ok"])
        wait_until(lambda: self.api._window.destroy.called)
        up.detach.assert_not_called()
        up.cancel.assert_not_called()
        self.remote.command.assert_not_called()
        self.assertFalse(Path(job.folder).exists())

    def test_new_local_job_since_prompt_rejects_stale_continue(self):
        self.add_job()
        old = self.active_runner()
        self.assertIs(self.api._on_closing(), False)
        self.api._files[1].update(state="done", job=None)
        self.add_job(remote=False, fid=2)
        local = self.active_runner(detachable=False)
        response = self.api.close_application("continue")
        self.assertFalse(response["ok"])
        self.assertFalse(response["options"]["can_continue"])
        self.assertIn("local", response["error"])
        self.api._window.destroy.assert_not_called()
        local.pause.assert_not_called()
        old.detach.assert_not_called()

    def test_choice_between_jobs_stops_dispatch_before_next_local_file(self):
        remote_job = self.add_job(state="queued")
        local_job = self.add_job(remote=False, state="queued", fid=2)
        stale_up = self.active_runner(detachable=False)
        self.assertTrue(self.api._close_options()["can_continue"])
        self.assertTrue(self.api.close_application("continue")["ok"])
        with mock.patch("upscaler.webapp.Upscaler") as local:
            self.api._run_jobs()
        wait_until(lambda: self.api._window.destroy.called)
        local.assert_not_called()
        stale_up.pause.assert_not_called()
        stale_up.cancel.assert_not_called()
        self.remote.runner.assert_not_called()
        for job, fid in ((remote_job, 1), (local_job, 2)):
            self.assertEqual(self.api._files[fid]["state"], "paused")
            self.assertTrue(Path(job.folder).exists())

    def test_pause_between_jobs_stops_dispatch_before_creating_remote_work(self):
        job = self.add_job(state="queued")
        self.api._running, self.api._task = True, "queue"
        self.assertTrue(self.api.close_application("pause")["ok"])
        self.api._run_jobs()
        wait_until(lambda: self.api._window.destroy.called)
        self.remote.runner.assert_not_called()
        self.assertEqual(self.api._files[1]["state"], "paused")
        self.assertTrue(Path(job.folder).exists())

    def test_unavailable_extension_spools_pause_for_later(self):
        job = self.add_job(state="paused")
        self.api._remote = None
        self.assertIs(self.api._on_closing(), False)
        self.assertTrue(self.api.close_application("pause")["ok"])
        wait_until(lambda: self.api._window.destroy.called)
        commands = list(Path(self.tmp.name, "extensions-data/remote-compute/commands").glob("*.json"))
        self.assertEqual(len(commands), 1)
        self.assertEqual(read_json(commands[0])["desired"], "pause")
        self.assertEqual(read_json(commands[0])["request_id"], job.id)
        self.assertTrue(Path(job.folder).exists())

    def test_save_failure_does_not_hide_window_or_abandon_work(self):
        self.add_job()
        up = self.active_runner()
        with mock.patch.object(self.api._jobs, "save", side_effect=OSError("Disque plein")):
            self.assertTrue(self.api.close_application("continue")["ok"])
            wait_until(lambda: any(e["type"] == "close_failed" for e in self.api._events))
        self.api._window.destroy.assert_not_called()
        self.assertFalse(self.api._closing_ok)
        self.assertFalse(self.api._closing_in_progress)
        up.detach.assert_called_once()
        self.remote.command.assert_not_called()
        self.remote.abandon.assert_not_called()

    def test_slow_detachment_keeps_window_and_dispatch_stop_instead_of_forced_exit(self):
        self.add_job()
        up = self.active_runner()
        up.detach.side_effect = None  # le réseau bloque encore le retour du runner
        self.api._closing_in_progress, self.api._detach_req = True, True
        with mock.patch("upscaler.webapp.time.monotonic", side_effect=[0, 61]), mock.patch("os._exit") as force_exit:
            self.api._finish_close("continue", "queue", up, False)
        force_exit.assert_not_called()
        self.api._window.destroy.assert_not_called()
        self.assertFalse(self.api._closing_ok)
        self.assertFalse(self.api._closing_in_progress)
        self.assertTrue(self.api._detach_req)  # aucun fichier suivant au retour tardif du réseau
        self.assertEqual(self.api._events[-1]["type"], "close_failed")
        up.cancel.assert_not_called()
        up.pause.assert_not_called()
        self.remote.command.assert_not_called()

    def test_idle_local_app_closes_without_prompt(self):
        self.api._remote = None
        self.assertIsNone(self.api._on_closing())
        self.assertFalse(self.api._events)
        self.assertFalse(self.api.close_application("invalid")["ok"])

    def test_worker_pause_waits_for_inflight_service_startup(self):
        entered, release = threading.Event(), threading.Event()
        self.remote.public.return_value = {"role": "worker", "running": True}
        self.remote.config.return_value = {"enabled": True, "startup": False}
        self.api._settings.auto_update = False

        def starting():
            entered.set()
            if not release.wait(10):
                raise AssertionError("Démarrage non libéré")

        self.remote.ensure_running.side_effect = starting
        with mock.patch.object(self.api, "_cleanup"), mock.patch.object(self.api, "_auto_resume"), \
             mock.patch("upscaler.webapp.updater.cleanup_old_versions"):
            thread = threading.Thread(target=self.api._startup_tasks, daemon=True)
            thread.start()
            try:
                self.assertTrue(entered.wait(3))
                self.assertTrue(self.api.close_application("pause")["ok"])
                self.assertFalse(self.api._service_startup_done.is_set())
                self.remote.stop.assert_not_called()
            finally:
                release.set()
                thread.join(timeout=5)
            wait_until(lambda: self.api._window.destroy.called)
        self.remote.stop.assert_called_once_with(keep_enabled=True)
        self.remote.ensure_running.assert_called_once()
        self.remote.set_startup.assert_not_called()

    def test_cancelling_early_prompt_does_not_lose_automatic_resume(self):
        self.add_job(state="paused")
        self.api._files[1]["info"] = True
        self.api._restored = 1
        self.assertIs(self.api._on_closing(), False)
        with mock.patch.object(self.api, "_launch_runner") as launch:
            thread = threading.Thread(target=self.api._auto_resume, daemon=True)
            thread.start()
            try:
                time.sleep(1.2)
                launch.assert_not_called()
                self.assertTrue(self.api.close_application("cancel")["ok"])
            finally:
                self.api._close_prompt = False
                thread.join(timeout=3)
            launch.assert_called_once()


class ClosingPackageTests(unittest.TestCase):
    setUp = protocol.PackageTests.setUp
    tearDown = protocol.PackageTests.tearDown

    def test_new_detachment_extension_rejects_old_app_without_shared_exception(self):
        with mock.patch("upscaler.extensions.__version__", "1.9.1"):
            with self.assertRaisesRegex(ExtensionError, "incompatible"):
                self.manager._install_verified(self.raw, self.info)
        self.assertIsNone(self.manager.load())


class RemoteClosingTests(unittest.TestCase):
    # Même vrai relais HTTPS et même stockage temporaire que les tests de protocole.
    setUp = protocol.RemoteTests.setUp
    tearDown = protocol.RemoteTests.tearDown
    target = protocol.RemoteTests.target
    create = protocol.RemoteTests.create

    def exercise_close_and_reopen(self, choice, disconnect=False, before_transfer=False):
        env = mock.patch.dict(os.environ, {"UPSCALER_DATA_DIR": str(self.folder / "app-data"),
                                         "APPDATA": str(self.folder / "app-config")})
        env.start()
        self.addCleanup(env.stop)
        entered, release = threading.Event(), threading.Event()

        class BlockingUpscaler(protocol.FakeUpscaler):
            def process(self, source, settings, output, job_dir=None, state=None, on_state=None):
                on_state(dict(state or {}, fake_done=3))
                entered.set()
                while not release.wait(0.01):
                    if self._pause.is_set():
                        raise engine.Paused()
                return super().process(source, settings, output, job_dir, state, on_state)

        self.worker.factory = BlockingUpscaler
        target, plugin = self.target(), self.plugin

        class QueuePlugin:
            def public(inner):
                return {"role": "controller", "running": True, "pending": 1}

            def selected(inner):
                return dict(target)

            def runner(inner, target, **kw):
                return protocol.RemoteRunner(plugin, target, **kw)

            def command(inner, request, desired):
                plugin.command(request, desired)

        info = engine.VideoInfo(str(self.source), 32, 24, "25/1", 25, 1, 25, None,
                                "progressive", "bt709", "yuv420p", "h264")
        with mock.patch.object(Api, "_detect_gpus"), \
             mock.patch.object(Api, "_load_settings", return_value=copy.deepcopy(self.settings)):
            api = Api()
            self.addCleanup(api._frame_pool.shutdown, wait=False)
            api._remote = QueuePlugin()
            api._window = mock.Mock()
            api._window.destroy.side_effect = lambda: self.assertIsNone(api._on_closing())
            file = {"id": 1, "name": self.source.name, "path": str(self.source), "status": "Prêt", "state": "ready",
                    "progress": 0, "res": "32×24", "dur": "00:00:01", "out": "64×48", "size": self.source.stat().st_size,
                    "output": "", "error": "", "thumb": "", "info": info, "kind": "video", "fps": 25, "job": None}
            api._files, api._order, api._next_id = {1: file}, [1], 2
            api._settings.output_dir = str(self.folder / "outputs")
            api._settings.temp_dir = ""
            self.assertTrue(api.start())
            job = file["job"]
            snapshot = copy.deepcopy(job.data["settings"])
            self.worker.detect = lambda: None
            thread = threading.Thread(target=self.worker.run, daemon=True)
            if not before_transfer:
                thread.start()
            try:
                if before_transfer:
                    wait_until(lambda: job.data["state"].get("remote_id"))
                else:
                    self.assertTrue(entered.wait(10))
                jid = job.data["state"]["remote_id"]
                self.assertIs(api._on_closing(), False)
                self.assertEqual(api._events[-1]["type"], "close_requested")
                self.assertTrue(api.close_application(choice)["ok"])
                wait_until(lambda: api._window.destroy.called and not api._running)
                self.assertEqual(len(api._jobs.load_pending()), 1)
                if before_transfer:
                    self.assertFalse((self.folder / "worker" / jid).exists())
                    thread.start()  # le relais sert encore la source, malgré la fenêtre fermée
                    self.assertTrue(entered.wait(10))
                self.assertTrue((self.folder / "worker" / jid).exists())
                if choice == "pause":
                    wait_until(lambda: self.hub.state["jobs"][jid]["status"] == "paused")
                    self.assertEqual(self.hub.state["jobs"][jid]["desired"], "pause")
                    saved = read_json(self.folder / "worker" / jid / "worker.json")
                    self.assertEqual(saved["checkpoint"]["fake_done"], 3)
                else:
                    self.assertEqual(self.hub.state["jobs"][jid]["desired"], "run")
                    if disconnect:
                        port = self.server.server_port
                        self.server.shutdown()
                        self.server.server_close()
                        self.thread.join(timeout=3)
                        # Comme un principal complètement éteint : plus aucun serveur,
                        # mais le GPU termine puis conserve le résultat sans reçu.
                        release.set()
                        result = self.folder / "worker" / jid / "result.mp4"
                        wait_until(lambda: result.exists() and read_json(result.parent / "worker.json").get("phase") == "uploading")
                        self.assertEqual(result.read_bytes(), b"RESULTAT:" + self.source.read_bytes())
                        self.hub = protocol.Hub(self.hub.folder, self.master, self.pin)
                        self.plugin.hub = self.hub
                        self.server = protocol.Server("127.0.0.1", port, self.hub,
                                                      self.folder / "tls.crt", self.folder / "tls.key")
                        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
                        self.thread.start()
                    self.assertNotIn(self.hub.state["jobs"][jid]["status"], ("delivered", "cleaned", "cancelled"))
                release.set()
                with mock.patch.object(Api, "_probe"):
                    reopened = Api()
                self.addCleanup(reopened._frame_pool.shutdown, wait=False)
                reopened._remote = QueuePlugin()
                restored = next(iter(reopened._files.values()))
                restored["info"] = info
                self.assertEqual(restored["job"].data["settings"], snapshot)
                self.assertEqual(restored["job"].data["state"]["remote_id"], jid)
                reopened._auto_resume()
                wait_until(lambda: not reopened._running, timeout=20)
                self.assertEqual(restored["state"], "done", restored.get("error"))
                self.assertEqual(Path(restored["output"]).read_bytes(), b"RESULTAT:" + self.source.read_bytes())
                wait_until(lambda: self.hub.state["jobs"][jid]["status"] == "cleaned")
                self.assertFalse((self.folder / "worker" / jid).exists())
                self.assertEqual(len(protocol.FakeUpscaler.calls), 1)
                self.assertTrue(self.source.exists())
            finally:
                release.set()
                self.worker.stop.set()
                if thread.ident is not None:
                    thread.join(timeout=20)

    def test_continue_closes_controller_survives_total_relay_loss_then_delivers(self):
        self.exercise_close_and_reopen("continue", disconnect=True)

    def test_close_pause_checkpoints_then_reopens_with_same_settings(self):
        self.exercise_close_and_reopen("pause")

    def test_continue_keeps_relay_serving_source_after_window_closes(self):
        self.exercise_close_and_reopen("continue", before_transfer=True)

    def exercise_worker_window(self, choice):
        env = mock.patch.dict(os.environ, {"UPSCALER_DATA_DIR": str(self.folder / "worker-app-data"),
                                         "APPDATA": str(self.folder / "worker-app-config")})
        env.start()
        self.addCleanup(env.stop)
        entered, release = threading.Event(), threading.Event()

        class BlockingUpscaler(protocol.FakeUpscaler):
            def process(self, source, settings, output, job_dir=None, state=None, on_state=None):
                on_state(dict(state or {}, fake_done=3))
                entered.set()
                while not release.wait(0.01):
                    if self._pause.is_set():
                        raise engine.Paused()
                return super().process(source, settings, output, job_dir, state, on_state)

        plugin = protocol.mod.Plugin(self.folder / "window-service")
        config = {"role": "worker", "enabled": True, "startup": True, "revision": "closing-test",
                  "url": self.url, "pin": self.pin, "token": self.identity["token"], "worker_id": self.identity["id"]}
        plugin._save(config)
        job = self.create()
        jid = job["id"]
        spool = plugin.folder / "worker" / jid
        worker = protocol.Worker(plugin.folder / "worker", config, factory=BlockingUpscaler)
        worker.detect = lambda: None
        services = []
        with mock.patch("upv_remote_tests.plugin.Worker", return_value=worker) as factory, \
             mock.patch("upv_remote_tests.plugin.set_startup") as startup, \
             mock.patch.object(Api, "_detect_gpus"):
            try:
                services.append(threading.Thread(target=plugin.run_service, daemon=True))
                services[-1].start()
                self.assertTrue(entered.wait(10))
                wait_until(lambda: plugin.public()["running"])
                api = Api()
                self.addCleanup(api._frame_pool.shutdown, wait=False)
                api._remote, api._window = plugin, mock.Mock()
                api._window.destroy.side_effect = lambda: self.assertIsNone(api._on_closing())
                self.assertIs(api._on_closing(), False)
                self.assertTrue(api._events[-1]["worker"])
                self.assertTrue(api.close_application(choice)["ok"])
                wait_until(lambda: api._window.destroy.called)
                self.assertTrue(plugin.config()["enabled"])
                self.assertTrue(plugin.config()["startup"])
                self.assertTrue(spool.exists())
                self.assertEqual(self.hub.state["jobs"][jid]["desired"], "run")
                if choice == "pause":
                    services[-1].join(timeout=3)
                    self.assertFalse(services[-1].is_alive())
                    self.assertEqual(read_json(spool / "worker.json")["checkpoint"]["fake_done"], 3)
                    self.assertEqual(self.hub.state["jobs"][jid]["status"], "paused")
                    replacement = protocol.Worker(plugin.folder / "worker", plugin.config(), factory=BlockingUpscaler)
                    replacement.detect = lambda: None
                    factory.return_value = replacement
                    release.set()
                    services.append(threading.Thread(target=plugin.run_service, daemon=True))
                    services[-1].start()  # comme la réouverture de l'application du PC GPU
                else:
                    self.assertTrue(services[-1].is_alive())
                    self.assertFalse(worker.stop.is_set())
                    release.set()
                wait_until(lambda: self.hub.state["jobs"][jid]["status"] == "ready")
                self.assertTrue(spool.exists())  # pas encore de reçu, donc aucun nettoyage
                output = self.folder / "worker-window-result.mp4"
                protocol.RemoteRunner(self.plugin, self.target(job["request_id"])).process(
                    str(self.source), self.settings, str(output))
                wait_until(lambda: self.hub.state["jobs"][jid]["status"] == "cleaned")
                self.assertEqual(output.read_bytes(), b"RESULTAT:" + self.source.read_bytes())
                self.assertEqual(len(protocol.FakeUpscaler.calls), 1)
                self.assertFalse(spool.exists())
            finally:
                release.set()
                plugin.stop(keep_enabled=True)
                for service in services:
                    service.join(timeout=20)
            startup.assert_not_called()

    def test_worker_window_continue_leaves_real_service_and_gpu_work_running(self):
        self.exercise_worker_window("continue")

    def test_worker_window_pause_checkpoints_real_service_and_preserves_startup(self):
        self.exercise_worker_window("pause")

    def test_detach_before_submission_sends_no_command(self):
        plugin = mock.Mock()
        runner = protocol.RemoteRunner(plugin, self.target())
        runner.detach()
        with self.assertRaises(RemoteDetached):
            runner.process(str(self.source), self.settings, str(self.folder / "later.mp4"))
        plugin.ensure_running.assert_not_called()
        plugin.command.assert_not_called()
        self.assertFalse(self.hub.state["jobs"])

    def test_detach_while_creating_request_saves_remote_id_without_duplicate(self):
        created, respond = threading.Event(), threading.Event()
        original = self.controller.post
        state, errors = {}, []
        runner = protocol.RemoteRunner(self.plugin, self.target())
        output = self.folder / "after-create.mp4"

        def post(route, *args, **kw):
            value = original(route, *args, **kw)
            if route == "/v1/controller/jobs":
                created.set()
                if not respond.wait(10):
                    raise AssertionError("Réponse de création non libérée")
            return value

        def process():
            try:
                runner.process(str(self.source), self.settings, str(output), on_state=lambda value: state.update(value))
            except Exception as error:
                errors.append(error)

        with mock.patch.object(self.controller, "post", side_effect=post):
            thread = threading.Thread(target=process, daemon=True)
            thread.start()
            try:
                self.assertTrue(created.wait(10))
                runner.detach()
            finally:
                respond.set()
                thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], RemoteDetached)
        self.assertEqual(len(self.hub.state["jobs"]), 1)
        jid = state["remote_id"]
        job = self.controller.get(f"/v1/jobs/{jid}")
        self.assertEqual(job["desired"], "run")
        self.worker.run_one(job)
        protocol.RemoteRunner(self.plugin, runner.target).process(str(self.source), self.settings, str(output), state=state)
        self.assertEqual(len(self.hub.state["jobs"]), 1)
        self.assertEqual(output.read_bytes(), b"RESULTAT:" + self.source.read_bytes())
        self.worker.step()
        self.assertEqual(self.hub.state["jobs"][jid]["status"], "cleaned")

    def test_detach_during_result_transfer_defers_ack_and_resumes_partial_file(self):
        job = self.create()
        jid = job["id"]
        self.worker.run_one(job)
        state = {}
        output = self.folder / "received.mp4"

        def progress(value):
            if value.stage == "Récupération du résultat amélioré":
                runner.detach()

        runner = protocol.RemoteRunner(self.plugin, self.target(job["request_id"]), on_progress=progress)
        with self.assertRaises(RemoteDetached):
            runner.process(str(self.source), self.settings, str(output), on_state=lambda value: state.update(value))
        self.assertGreater(Path(str(output) + ".partiel").stat().st_size, 0)
        self.assertFalse(output.exists())
        self.assertEqual(self.hub.state["jobs"][jid]["status"], "ready")
        self.assertEqual(self.hub.state["jobs"][jid]["desired"], "run")
        self.worker.step()
        self.assertTrue((self.folder / "worker" / jid).exists())
        protocol.RemoteRunner(self.plugin, runner.target).process(str(self.source), self.settings, str(output), state=state)
        self.assertEqual(output.read_bytes(), b"RESULTAT:" + self.source.read_bytes())
        self.worker.step()
        self.assertEqual(len(protocol.FakeUpscaler.calls), 1)
        self.assertFalse((self.folder / "worker" / jid).exists())

    def test_detach_after_rename_defers_ack_until_verified_file_reopens(self):
        job = self.create()
        self.worker.run_one(job)
        output = self.folder / "final.mp4"
        state = {}
        runner = protocol.RemoteRunner(self.plugin, self.target(job["request_id"]))
        from upscaler.extension_runtime import durable_replace

        def replace(*args):
            durable_replace(*args)
            runner.detach()

        with mock.patch("upv_remote_tests.runner.durable_replace", side_effect=replace):
            with self.assertRaises(RemoteDetached):
                runner.process(str(self.source), self.settings, str(output), on_state=lambda value: state.update(value))
        self.assertTrue(output.exists())
        self.assertEqual(self.hub.state["jobs"][job["id"]]["status"], "ready")
        self.assertTrue((self.folder / "worker" / job["id"]).exists())
        protocol.RemoteRunner(self.plugin, runner.target).process(str(self.source), self.settings, str(output), state=state)
        self.worker.step()
        self.assertEqual(output.read_bytes(), b"RESULTAT:" + self.source.read_bytes())
        self.assertEqual(len(protocol.FakeUpscaler.calls), 1)
        self.assertFalse((self.folder / "worker" / job["id"]).exists())


if __name__ == "__main__":
    unittest.main()
