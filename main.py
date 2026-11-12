"""Point d'entrée d'Upscale Video AI.

    UpscaleVideoAI.exe                  -> interface moderne (WebView2), sinon interface classique
    UpscaleVideoAI.exe --classic        -> force l'interface classique (Tkinter)
    UpscaleVideoAI.exe --selftest FILE  -> vérifications automatiques (utilisé par la CI)
"""
import os
import subprocess
import sys
import tempfile
import time
import traceback

WEBVIEW2_URL = "https://developer.microsoft.com/fr-fr/microsoft-edge/webview2/"


def close_splash():
    try:  # ferme l'écran de démarrage PyInstaller
        import pyi_splash  # type: ignore
        pyi_splash.close()
    except Exception:
        pass


def write_crash_log(text: str) -> str:
    base = os.environ.get("APPDATA") or tempfile.gettempdir()
    folder = os.path.join(base, "UpscaleVideoAI")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "erreurs.log")
    with open(path, "a", encoding="utf-8") as f:
        f.write(time.strftime("\n===== %Y-%m-%d %H:%M:%S =====\n") + text + "\n")
    return path


def selftest(report_path: str) -> int:
    lines, ok = [], True

    def check(name, fn):
        nonlocal ok
        try:
            res = fn()
            lines.append(f"OK   {name}: {res}")
        except Exception as e:  # noqa: BLE001
            ok = False
            lines.append(f"FAIL {name}: {e}\n{traceback.format_exc()}")

    from upscaler import engine, tools

    check("ffmpeg", lambda: tools.find_tool("ffmpeg"))
    check("ffprobe", lambda: tools.find_tool("ffprobe"))
    check("realesrgan", lambda: tools.find_tool(tools.UPSCALER_BIN))

    def model_files(name):
        for ext in (".param", ".bin"):
            path = os.path.join(tools.models_dir(), name + ext)
            if not os.path.isfile(path):
                raise FileNotFoundError(path)
        return "présent"

    for m in engine.MODELS:
        names = [f"{m.key}-x{s}" for s in m.scales] if m.key == "realesr-animevideov3" else [m.key]
        for n in names:
            check(f"modèle {n}", lambda n=n: model_files(n))

    def probe_clip():
        tmp = tempfile.mkdtemp(prefix="upv_selftest_")
        clip = os.path.join(tmp, "clip.mp4")
        tools.run([tools.find_tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
                   "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25", "-t", "1",
                   "-pix_fmt", "yuv420p", clip], check=True,
                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        info = engine.probe(clip)
        plan = engine.make_plan(info, engine.Settings(output_size="1080p"))
        return f"{info.summary()} -> {plan.out_w}x{plan.out_h}"

    check("probe", probe_clip)

    # --- interpolation (RIFE)
    check("rife", lambda: tools.find_tool(engine.RIFE_BIN))

    def rife_model():
        files = sorted(os.listdir(engine.rife_model_dir()))
        if not any(f.endswith(".param") for f in files) or not any(f.endswith(".bin") for f in files):
            raise FileNotFoundError(f"{engine.rife_model_dir()} : {files}")
        return ", ".join(files)

    check(f"modèle {engine.RIFE_MODEL}", rife_model)

    # --- images : détection et format de sortie
    def image_detect():
        tmp = tempfile.mkdtemp(prefix="upv_selftest_")
        out = []
        for name in ("photo.png", "photo.jpg", "photo.webp"):
            path = os.path.join(tmp, name)
            tools.run([tools.find_tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
                       "-f", "lavfi", "-i", "testsrc2=size=300x200", "-frames:v", "1", path],
                      check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            info = engine.probe(path)
            if not info.is_image:
                raise RuntimeError(f"{name} pris pour une vidéo")
            st = engine.Settings(output_size="x2")
            plan = engine.make_plan(info, st)
            dst = engine.default_output_path(info, st, plan)
            out.append(f"{name} -> {os.path.basename(dst)}")
        return "; ".join(out)

    check("images (détection)", image_detect)

    # --- vrai RIFE sur le processeur (-g -1) : valide tout le pipeline d'interpolation
    def rife_cpu_pipeline():
        tmp = tempfile.mkdtemp(prefix="upv_rife_")
        clip = os.path.join(tmp, "clip.mp4")
        ff = tools.find_tool("ffmpeg")
        tools.run([ff, "-hide_banner", "-loglevel", "error", "-y",
                   "-f", "lavfi", "-i", "testsrc2=size=96x64:rate=10:duration=1.5",
                   "-f", "lavfi", "-i", "smptebars=size=96x64:rate=10:duration=1.5",
                   "-filter_complex", "[0:v][1:v]concat=n=2:v=1[v]", "-map", "[v]",
                   "-pix_fmt", "yuv420p", clip], check=True,
                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        orig = engine.make_plan

        def small_batches(info, st):
            plan = orig(info, st)
            plan.batch_size = 7
            return plan

        engine.make_plan = small_batches
        logs = []
        try:
            up = engine.Upscaler(on_log=logs.append)
            out = up.process(clip, engine.Settings(output_size="x1", interpolation="x2", gpu="-1"))
        except engine.UpscaleError as e:
            if "RIFE" in str(e):
                lines.append(f"INFO rife-cpu: impossible ici ({str(e)[:300]!r})")
                return "ignoré"
            raise
        finally:
            engine.make_plan = orig
        r = tools.run([tools.find_tool("ffprobe"), "-v", "error", "-count_frames", "-select_streams",
                       "v:0", "-show_entries", "stream=nb_read_frames,r_frame_rate", "-of", "csv=p=0",
                       out], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        rate, frames = r.stdout.decode().strip().split(",")[:2]
        if int(frames) != 60 or rate != "20/1":
            raise RuntimeError(f"attendu 60 images à 20/1, obtenu {frames} à {rate}")
        cuts = [l for l in logs if "changement" in l]
        return f"30 → {frames} images, {rate} i/s, {cuts[-1] if cuts else ''}"

    check("interpolation RIFE (processeur)", rife_cpu_pipeline)

    def frame_grab():
        tmp = tempfile.mkdtemp(prefix="upv_selftest_")
        clip = os.path.join(tmp, "clip.mp4")
        tools.run([tools.find_tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
                   "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25", "-t", "2",
                   "-pix_fmt", "yuv420p", clip], check=True,
                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        data = engine.grab_frame_jpeg(clip, 1.0, 640)
        if not data.startswith(b"\xff\xd8"):
            raise RuntimeError("JPEG invalide")
        return f"{len(data)} octets"

    check("extraction d'image (aperçu)", frame_grab)

    # --- mises à jour : téléchargement + vérification + échange d'un exe en cours d'exécution
    from upscaler import updater
    try:
        info = updater.check(current="0.0.0")
    except Exception as e:  # noqa: BLE001  (limite de l'API GitHub, réseau…)
        info = None
        lines.append(f"INFO mise à jour : API GitHub injoignable ({e})")
    if info:
        def update_download():
            tmp = tempfile.mkdtemp(prefix="upv_upd_")
            t0 = time.time()
            updater.download(info["url"], os.path.join(tmp, "dl.bin"), info["size"], info["sha256"])
            return (f"v{info['version']} ({info['size'] / 1e6:.0f} Mo) téléchargée en "
                    f"{time.time() - t0:.0f} s, SHA-256 {'vérifié' if info['sha256'] else 'non fourni'}")

        check("téléchargement de mise à jour", update_download)

        if updater.install_mode() == "onefile":
            def update_swap():
                import shutil
                tmp = tempfile.mkdtemp(prefix="upv_swap_")
                app = os.path.join(tmp, "App.exe")
                shutil.copy2(sys.executable, app)
                proc = subprocess.Popen([app, "--idle", "60"], env=updater._clean_env())
                time.sleep(5)
                if proc.poll() is not None:
                    raise RuntimeError("la copie de test ne tourne pas")
                shutil.copy2(sys.executable, app + ".new")
                old = updater.swap_executable(app + ".new", app)
                proc.kill()
                marker = os.environ.get("UPV_CHILD_MARKER")
                if marker:
                    updater._spawn_detached([app, "--update-child-test", marker], cwd=tmp)
                    lines.append("INFO update-child: lancé")
                return f"exe en cours d'exécution remplacé (ancien : {os.path.basename(old)})"

            check("installation de mise à jour", update_swap)

    try:  # non bloquant : pas de GPU sur les machines de CI
        gpus, raw = tools.detect_gpus()
        lines.append(f"INFO gpu: {gpus or 'aucun GPU Vulkan'}")
    except Exception as e:  # noqa: BLE001
        lines.append(f"INFO gpu: détection impossible ({e})")

    def classic_gui():
        from upscaler import gui as g
        root = g.create_root()
        app = g.App(root)
        root.update()
        root.after(500, root.destroy)
        root.mainloop()
        return f"OK (dnd={g._DND}, theme_dark={app.dark})"

    check("interface classique", classic_gui)

    def webview2():
        from upscaler import webapp
        if not webapp.webview2_available():
            raise RuntimeError("runtime WebView2 introuvable")
        return "runtime présent"

    check("webview2", webview2)

    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    # interface moderne : ouvre réellement la fenêtre et attend que la page soit prête
    try:
        from upscaler import webapp
        if webapp.run(selftest_report=report_path) != 0:
            ok = False
    except Exception as e:  # noqa: BLE001
        ok = False
        with open(report_path, "a", encoding="utf-8") as f:
            f.write(f"FAIL webui: {e}\n{traceback.format_exc()}\n")

    with open(report_path, "a", encoding="utf-8") as f:
        f.write("SELFTEST PASSED\n" if ok else "SELFTEST FAILED\n")
    return 0 if ok else 1


def run_classic():
    from upscaler.gui import main as gui_main
    close_splash()
    gui_main([])


def run_modern() -> bool:
    """Lance l'interface moderne. Retourne False si elle n'est pas disponible."""
    try:
        from upscaler import webapp
    except Exception:
        write_crash_log("Import de l'interface moderne impossible :\n" + traceback.format_exc())
        return False
    if not webapp.webview2_available():
        close_splash()
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            if messagebox.askyesno(
                    "Upscale Video AI",
                    "L'interface moderne nécessite « Microsoft Edge WebView2 » (gratuit, déjà présent "
                    "sur la plupart des PC Windows 10/11).\n\nVoulez-vous ouvrir la page de "
                    "téléchargement ?\n\nEn attendant, l'interface classique va s'ouvrir."):
                import webbrowser
                webbrowser.open(WEBVIEW2_URL)
            root.destroy()
        except Exception:
            pass
        return False
    close_splash()
    try:
        webapp.run()
        return True
    except Exception:
        write_crash_log("Erreur de l'interface moderne :\n" + traceback.format_exc())
        return False


def main():
    args = sys.argv[1:]
    if len(args) >= 2 and args[0] == "--idle":  # utilisé par l'auto-test des mises à jour
        close_splash()
        time.sleep(float(args[1]))
        return
    if len(args) >= 2 and args[0] == "--update-child-test":
        # vérifie qu'une instance relancée après une mise à jour démarre correctement,
        # même après la fermeture de l'instance qui l'a lancée
        close_splash()
        time.sleep(10)
        try:
            from upscaler import tools, webapp
            meipass = getattr(sys, "_MEIPASS", "")
            ok = (os.path.isdir(meipass) and os.path.isfile(tools.find_tool("ffmpeg"))
                  and os.path.isfile(webapp.ui_index()))
            msg = f"{'CHILD OK' if ok else 'CHILD FAIL'} {meipass}"
        except Exception as e:  # noqa: BLE001
            msg = f"CHILD FAIL {e}"
        with open(args[1], "w", encoding="utf-8") as f:
            f.write(msg + "\n")
        return
    if len(args) >= 2 and args[0] == "--selftest":
        close_splash()
        sys.exit(selftest(args[1]))
    if "--classic" in args or os.environ.get("UPSCALER_UI") == "classic":
        run_classic()
        return
    if not run_modern():
        run_classic()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        path = write_crash_log(traceback.format_exc())
        try:
            import tkinter as tk
            from tkinter import messagebox
            r = tk.Tk()
            r.withdraw()
            messagebox.showerror("Upscale Video AI", f"Erreur inattendue.\nDétails : {path}")
        except Exception:
            pass
        raise
