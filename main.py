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
