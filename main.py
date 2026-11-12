"""Point d'entrée d'Upscale Video AI.

    UpscaleVideoAI.exe                  -> interface graphique
    UpscaleVideoAI.exe --selftest FILE  -> vérifications automatiques (utilisé par la CI)
"""
import os
import subprocess
import sys
import tempfile
import traceback


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

    def gui():
        from upscaler import gui as g
        root = g.create_root()
        app = g.App(root)
        root.update()
        app._add_files([])
        root.after(500, root.destroy)
        root.mainloop()
        return f"OK (dnd={g._DND}, theme_dark={app.dark})"

    check("gui", gui)

    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n" + ("SELFTEST PASSED" if ok else "SELFTEST FAILED") + "\n")
    return 0 if ok else 1


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--selftest":
        try:
            import pyi_splash  # type: ignore
            pyi_splash.close()
        except Exception:
            pass
        sys.exit(selftest(sys.argv[2]))
    from upscaler.gui import main as gui_main
    try:  # ferme l'écran de démarrage PyInstaller une fois les modules chargés
        import pyi_splash  # type: ignore
        pyi_splash.close()
    except Exception:
        pass
    gui_main(sys.argv[1:])


if __name__ == "__main__":
    main()
