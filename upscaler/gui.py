"""Interface graphique (Tkinter) d'Upscale Video AI."""
from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
import traceback
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from . import __version__, engine, tools
from .jobs import JobStore
from .engine import (ENCODERS, IMAGE_EXT, IMAGE_FORMATS, INTERPOLATIONS, MODELS, MODELS_BY_KEY,
                     OUTPUT_SIZES, SPEEDS, Cancelled, Settings, Upscaler, UpscaleError, fmt_time)

APP_TITLE = "Upscale Video AI"
VIDEO_EXT = (".mp4", ".mkv", ".mov", ".avi", ".wmv", ".flv", ".webm", ".m4v", ".mpg",
             ".mpeg", ".ts", ".m2ts", ".mts", ".vob", ".3gp", ".ogv", ".divx", ".asf", ".gif")
MEDIA_EXT = VIDEO_EXT + IMAGE_EXT

try:  # glisser-déposer (optionnel)
    from tkinterdnd2 import DND_FILES, TkinterDnD  # type: ignore
    _DND = True
except Exception:  # pragma: no cover
    _DND = False


def config_path() -> str:
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    folder = os.path.join(base, "UpscaleVideoAI")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "settings.json")


def quality_label(crf: int) -> str:
    if crf <= 14:
        return "quasi sans perte (gros fichier)"
    if crf <= 18:
        return "excellente"
    if crf <= 22:
        return "très bonne"
    if crf <= 26:
        return "bonne"
    return "moyenne (petit fichier)"


class LabeledCombo:
    """Combobox en lecture seule qui associe des libellés à des clés."""

    def __init__(self, parent, items: list[tuple[str, str]], width: int = 42, command=None):
        self.items = items
        self.var = tk.StringVar()
        self.widget = ttk.Combobox(parent, textvariable=self.var, state="readonly",
                                   values=[lbl for _, lbl in items], width=width)
        if command:
            self.widget.bind("<<ComboboxSelected>>", lambda e: command())

    def set_items(self, items):
        current = self.get()
        self.items = items
        self.widget.configure(values=[lbl for _, lbl in items])
        self.set(current)

    def get(self) -> str:
        label = self.var.get()
        for k, lbl in self.items:
            if lbl == label:
                return k
        return self.items[0][0] if self.items else ""

    def set(self, key: str) -> None:
        for k, lbl in self.items:
            if str(k) == str(key):
                self.var.set(lbl)
                return
        if self.items:
            self.var.set(self.items[0][1])


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.events: queue.Queue = queue.Queue()
        self.files: list[dict] = []       # {path, iid, info, status}
        self.worker: threading.Thread | None = None
        self.upscaler: Upscaler | None = None
        self.running = False
        self.last_output_dir = ""
        self.jobs = JobStore()
        self._pause_req = False
        self._closing = False

        root.title(f"{APP_TITLE} {__version__} – Agrandissement vidéo par IA")
        root.geometry("1180x760")
        root.minsize(980, 640)
        self._set_icon()
        self._apply_theme()
        self._build()
        self._load_settings()
        self._on_model_change()
        self._update_quality_label()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(100, self._poll_events)
        threading.Thread(target=self._detect_gpus, daemon=True).start()
        self._restore_jobs()
        threading.Thread(target=self._cleanup, args=(self._collect_settings().temp_dir,),
                         daemon=True).start()

    # ------------------------------------------------------------------ apparence
    def _set_icon(self):
        ico = os.path.join(tools.resource_root(), "assets", "icon.ico")
        if tools.IS_WIN and os.path.isfile(ico):
            try:
                self.root.iconbitmap(default=ico)
            except Exception:
                pass

    def _apply_theme(self):
        try:
            import sv_ttk  # type: ignore
            sv_ttk.set_theme("dark")
            self.dark = True
        except Exception:
            self.dark = False
            style = ttk.Style()
            for theme in ("vista", "clam"):
                if theme in style.theme_names():
                    style.theme_use(theme)
                    break
        style = ttk.Style()
        style.configure("Title.TLabel", font=("Segoe UI", 16, "bold"))
        style.configure("Sub.TLabel", font=("Segoe UI", 9))
        style.configure("Big.TButton", font=("Segoe UI", 11, "bold"), padding=(14, 8))
        style.configure("Treeview", rowheight=26)

    # ------------------------------------------------------------------ construction
    def _build(self):
        root = self.root
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)

        header = ttk.Frame(root, padding=(16, 12, 16, 4))
        header.grid(row=0, column=0, sticky="ew")
        ttk.Label(header, text="Upscale Video AI", style="Title.TLabel").pack(side="left")
        ttk.Label(header, text="   Agrandissez vos vidéos jusqu'en 4K / 8K grâce à l'IA – 100 % gratuit et local",
                  style="Sub.TLabel").pack(side="left", pady=(6, 0))

        body = ttk.Frame(root, padding=(16, 4, 16, 8))
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(0, weight=3)
        body.columnconfigure(1, weight=2)
        body.rowconfigure(0, weight=3)
        body.rowconfigure(1, weight=2)

        # ---- file d'attente
        qf = ttk.LabelFrame(body, text=" Vidéos à traiter ", padding=8)
        qf.grid(row=0, column=0, sticky="nsew", padx=(0, 8), pady=(0, 8))
        qf.columnconfigure(0, weight=1)
        qf.rowconfigure(1, weight=1)
        bar = ttk.Frame(qf)
        bar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        self.btn_add = ttk.Button(bar, text="➕ Ajouter des vidéos…", command=self._add_dialog)
        self.btn_add.pack(side="left")
        self.btn_remove = ttk.Button(bar, text="Retirer", command=self._remove_selected)
        self.btn_remove.pack(side="left", padx=6)
        self.btn_clear = ttk.Button(bar, text="Vider", command=self._clear)
        self.btn_clear.pack(side="left")
        hint = "Glissez-déposez vos vidéos ici" if _DND else ""
        ttk.Label(bar, text=hint, style="Sub.TLabel").pack(side="right")

        cols = ("res", "dur", "out", "status")
        self.tree = ttk.Treeview(qf, columns=cols, show="tree headings", selectmode="extended")
        self.tree.heading("#0", text="Fichier")
        self.tree.heading("res", text="Source")
        self.tree.heading("dur", text="Durée")
        self.tree.heading("out", text="Sortie")
        self.tree.heading("status", text="Statut")
        self.tree.column("#0", width=260, stretch=True)
        self.tree.column("res", width=90, anchor="center", stretch=False)
        self.tree.column("dur", width=75, anchor="center", stretch=False)
        self.tree.column("out", width=90, anchor="center", stretch=False)
        self.tree.column("status", width=130, anchor="center", stretch=False)
        self.tree.grid(row=1, column=0, sticky="nsew")
        sb = ttk.Scrollbar(qf, orient="vertical", command=self.tree.yview)
        sb.grid(row=1, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=sb.set)
        if _DND:
            try:
                for w in (self.tree, qf):
                    w.drop_target_register(DND_FILES)
                    w.dnd_bind("<<Drop>>", self._on_drop)
            except Exception:
                pass

        # ---- journal
        lf = ttk.LabelFrame(body, text=" Journal ", padding=8)
        lf.grid(row=1, column=0, sticky="nsew", padx=(0, 8))
        lf.columnconfigure(0, weight=1)
        lf.rowconfigure(0, weight=1)
        self.log_box = ScrolledText(lf, height=8, wrap="word", font=("Consolas", 9),
                                    state="disabled", relief="flat",
                                    background="#1c1c1c" if self.dark else "#ffffff",
                                    foreground="#e6e6e6" if self.dark else "#000000",
                                    insertbackground="#e6e6e6")
        self.log_box.grid(row=0, column=0, sticky="nsew")

        # ---- réglages
        sf = ttk.LabelFrame(body, text=" Réglages ", padding=10)
        sf.grid(row=0, column=1, rowspan=2, sticky="nsew")
        sf.columnconfigure(1, weight=1)
        r = 0

        def row(label, widget, help_text=None):
            nonlocal r
            ttk.Label(sf, text=label).grid(row=r, column=0, sticky="w", pady=4, padx=(0, 8))
            widget.grid(row=r, column=1, sticky="ew", pady=4)
            r += 1
            if help_text is not None:
                help_text.grid(row=r, column=1, sticky="w", pady=(0, 4))
                r += 1

        self.cb_model = LabeledCombo(sf, [(m.key, m.label) for m in MODELS],
                                     command=self._on_model_change)
        self.lbl_model = ttk.Label(sf, text="", style="Sub.TLabel", wraplength=360, justify="left")
        row("Modèle IA", self.cb_model.widget, self.lbl_model)

        self.cb_size = LabeledCombo(sf, OUTPUT_SIZES, command=self._refresh_outputs)
        row("Résolution de sortie", self.cb_size.widget)

        self.cb_interp = LabeledCombo(sf, INTERPOLATIONS)
        row("Fluidité (interpolation IA)", self.cb_interp.widget,
            ttk.Label(sf, text="RIFE crée des images intermédiaires (vidéos uniquement)", style="Sub.TLabel"))

        self.cb_encoder = LabeledCombo(sf, [(e.key, e.label) for e in ENCODERS])
        row("Encodeur vidéo", self.cb_encoder.widget)

        qbox = ttk.Frame(sf)
        qbox.columnconfigure(0, weight=1)
        self.var_quality = tk.IntVar(value=18)
        self.scale_quality = ttk.Scale(qbox, from_=30, to=10, orient="horizontal",
                                       command=lambda v: self._update_quality_label())
        self.scale_quality.grid(row=0, column=0, sticky="ew")
        self.lbl_quality = ttk.Label(qbox, text="", width=30)
        self.lbl_quality.grid(row=0, column=1, sticky="w", padx=(8, 0))
        row("Qualité", qbox)

        self.cb_speed = LabeledCombo(sf, SPEEDS)
        row("Vitesse d'encodage", self.cb_speed.widget)

        self.cb_container = LabeledCombo(sf, [("mp4", "MP4 (recommandé)"),
                                              ("mkv", "MKV (garde tous les sous-titres)")])
        row("Format des vidéos", self.cb_container.widget)

        self.cb_imgfmt = LabeledCombo(sf, IMAGE_FORMATS)
        row("Format des images", self.cb_imgfmt.widget)

        out = ttk.Frame(sf)
        out.columnconfigure(0, weight=1)
        self.var_outdir = tk.StringVar()
        self.ent_outdir = ttk.Entry(out, textvariable=self.var_outdir)
        self.ent_outdir.grid(row=0, column=0, sticky="ew")
        ttk.Button(out, text="…", width=3, command=self._choose_outdir).grid(row=0, column=1, padx=(4, 0))
        row("Dossier de sortie", out,
            ttk.Label(sf, text="Vide = à côté de la vidéo d'origine", style="Sub.TLabel"))

        ttk.Separator(sf).grid(row=r, column=0, columnspan=2, sticky="ew", pady=8)
        r += 1
        ttk.Label(sf, text="Avancé", font=("Segoe UI", 10, "bold")).grid(row=r, column=0, sticky="w")
        r += 1

        self.cb_gpu = LabeledCombo(sf, [("auto", "Automatique")])
        row("Carte graphique", self.cb_gpu.widget)

        self.cb_deint = LabeledCombo(sf, [("auto", "Automatique"), ("on", "Toujours"),
                                          ("off", "Jamais")])
        row("Désentrelacement", self.cb_deint.widget)

        self.cb_tile = LabeledCombo(sf, [(0, "Automatique"), (512, "512"), (256, "256"),
                                         (128, "128 (peu de VRAM)"), (64, "64 (très peu de VRAM)")])
        row("Taille de tuile", self.cb_tile.widget)

        self.var_tta = tk.BooleanVar(value=False)
        row("", ttk.Checkbutton(sf, text="Mode TTA (qualité légèrement meilleure, 8x plus lent)",
                                variable=self.var_tta))

        tmp = ttk.Frame(sf)
        tmp.columnconfigure(0, weight=1)
        self.var_tmpdir = tk.StringVar()
        ttk.Entry(tmp, textvariable=self.var_tmpdir).grid(row=0, column=0, sticky="ew")
        ttk.Button(tmp, text="…", width=3, command=self._choose_tmpdir).grid(row=0, column=1, padx=(4, 0))
        row("Dossier temporaire", tmp,
            ttk.Label(sf, text="Vide = dossier temporaire Windows (prévoir ~5 Go libres)",
                      style="Sub.TLabel"))

        # ---- bas : progression + boutons
        bottom = ttk.Frame(root, padding=(16, 4, 16, 14))
        bottom.grid(row=2, column=0, sticky="ew")
        bottom.columnconfigure(0, weight=1)
        self.lbl_status = ttk.Label(bottom, text="Prêt. Ajoutez une ou plusieurs vidéos pour commencer.")
        self.lbl_status.grid(row=0, column=0, sticky="w")
        self.pb_file = ttk.Progressbar(bottom, mode="determinate", maximum=1000)
        self.pb_file.grid(row=1, column=0, sticky="ew", pady=(4, 2))
        self.lbl_total = ttk.Label(bottom, text="", style="Sub.TLabel")
        self.lbl_total.grid(row=2, column=0, sticky="w")

        btns = ttk.Frame(bottom)
        btns.grid(row=0, column=1, rowspan=3, sticky="e", padx=(16, 0))
        self.btn_preview = ttk.Button(btns, text="🔍 Aperçu", command=self._preview)
        self.btn_preview.pack(side="left", padx=4)
        self.btn_open = ttk.Button(btns, text="📂 Ouvrir le dossier", command=self._open_output)
        self.btn_open.pack(side="left", padx=4)
        self.btn_pause = ttk.Button(btns, text="⏸  Pause", command=self._pause, state="disabled")
        self.btn_pause.pack(side="left", padx=4)
        self.btn_cancel = ttk.Button(btns, text="Annuler", command=self._cancel, state="disabled")
        self.btn_cancel.pack(side="left", padx=4)
        self.btn_start = ttk.Button(btns, text="▶  Démarrer", style="Big.TButton", command=self._start)
        if self.dark:  # style "Accent" fourni par le thème Sun Valley
            try:
                self.btn_start.configure(style="Accent.TButton")
            except tk.TclError:
                pass
        self.btn_start.pack(side="left", padx=(4, 0))

    # ------------------------------------------------------------------ réglages
    def _collect_settings(self) -> Settings:
        import dataclasses
        return dataclasses.replace(
            getattr(self, "_loaded_settings", None) or Settings(),
            model=self.cb_model.get(), output_size=self.cb_size.get(),
            encoder=self.cb_encoder.get(), quality=int(round(self.scale_quality.get())),
            speed=self.cb_speed.get(), deinterlace=self.cb_deint.get(), gpu=str(self.cb_gpu.get()),
            tile=int(self.cb_tile.get() or 0), tta=bool(self.var_tta.get()),
            container=self.cb_container.get(), output_dir=self.var_outdir.get().strip(),
            temp_dir=self.var_tmpdir.get().strip(), interpolation=self.cb_interp.get(),
            image_format=self.cb_imgfmt.get())

    def _load_settings(self):
        data = {}
        try:
            with open(config_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            pass
        s = Settings.from_dict(data)
        self._loaded_settings = s
        self.cb_interp.set(s.interpolation)
        self.cb_imgfmt.set(s.image_format)
        self.cb_model.set(s.model)
        self.cb_size.set(s.output_size)
        self.cb_encoder.set(s.encoder)
        self.scale_quality.set(s.quality)
        self.cb_speed.set(s.speed)
        self.cb_deint.set(s.deinterlace)
        self._saved_gpu = s.gpu
        self.cb_gpu.set(s.gpu)
        self.cb_tile.set(s.tile)
        self.var_tta.set(s.tta)
        self.cb_container.set(s.container)
        self.var_outdir.set(s.output_dir)
        self.var_tmpdir.set(s.temp_dir)

    def _save_settings(self):
        try:
            with open(config_path(), "w", encoding="utf-8") as f:
                json.dump(self._collect_settings().to_dict(), f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    def _on_model_change(self):
        m = MODELS_BY_KEY.get(self.cb_model.get())
        if m:
            self.lbl_model.configure(text=m.description)
        self._refresh_outputs()

    def _update_quality_label(self):
        v = int(round(self.scale_quality.get()))
        self.lbl_quality.configure(text=f"CRF {v} – {quality_label(v)}")

    def _choose_outdir(self):
        d = filedialog.askdirectory(title="Dossier de sortie")
        if d:
            self.var_outdir.set(os.path.normpath(d))

    def _choose_tmpdir(self):
        d = filedialog.askdirectory(title="Dossier temporaire")
        if d:
            self.var_tmpdir.set(os.path.normpath(d))

    # ------------------------------------------------------------------ fichiers
    def _add_dialog(self):
        paths = filedialog.askopenfilenames(
            title="Choisir des vidéos",
            filetypes=[("Vidéos et images", " ".join("*" + e for e in MEDIA_EXT)),
                       ("Vidéos", " ".join("*" + e for e in VIDEO_EXT)),
                       ("Images", " ".join("*" + e for e in IMAGE_EXT)), ("Tous les fichiers", "*.*")])
        self._add_files(paths)

    def _on_drop(self, event):
        try:
            paths = self.root.tk.splitlist(event.data)
        except Exception:
            paths = [event.data]
        expanded = []
        for p in paths:
            if os.path.isdir(p):
                for name in sorted(os.listdir(p)):
                    if name.lower().endswith(MEDIA_EXT):
                        expanded.append(os.path.join(p, name))
            else:
                expanded.append(p)
        self._add_files(expanded)

    def _add_files(self, paths):
        known = {f["path"] for f in self.files}
        for p in paths:
            p = os.path.normpath(p)
            if not os.path.isfile(p) or p in known:
                continue
            iid = self.tree.insert("", "end", text=os.path.basename(p),
                                   values=("…", "…", "…", "En attente"))
            entry = {"path": p, "iid": iid, "info": None, "status": "En attente", "job": None}
            self.files.append(entry)
            known.add(p)
            threading.Thread(target=self._probe_entry, args=(entry,), daemon=True).start()

    def _probe_entry(self, entry):
        try:
            info = engine.probe(entry["path"])
            self.events.put(("probed", entry, info, None))
        except Exception as e:  # noqa: BLE001
            self.events.put(("probed", entry, None, str(e)))

    def _remove_selected(self):
        if self.running:
            return
        sel = set(self.tree.selection())
        for iid in sel:
            self.tree.delete(iid)
        for f in self.files:
            if f["iid"] in sel and f.get("job"):
                self.jobs.delete(f["job"])
        self.files = [f for f in self.files if f["iid"] not in sel]

    def _clear(self):
        if self.running:
            return
        for f in self.files:
            self.tree.delete(f["iid"])
            if f.get("job"):
                self.jobs.delete(f["job"])
        self.files = []

    def _refresh_outputs(self):
        s = self._collect_settings()
        for f in self.files:
            info = f.get("info")
            if not info:
                continue
            try:  # un fichier lancé garde ses réglages figés
                plan = engine.make_plan(info, f["job"].settings if f.get("job") else s)
                out = f"{plan.out_w}x{plan.out_h}"
            except UpscaleError:
                out = "déjà ≥"
            self.tree.set(f["iid"], "out", out)

    def _set_status(self, entry, text):
        entry["status"] = text
        try:
            self.tree.set(entry["iid"], "status", text)
        except tk.TclError:
            pass

    # ------------------------------------------------------------------ GPU
    def _detect_gpus(self):
        try:
            gpus, raw = tools.detect_gpus()
            self.events.put(("gpus", gpus, raw))
        except Exception as e:  # noqa: BLE001
            self.events.put(("gpus", [], str(e)))

    # ------------------------------------------------------------------ actions
    def _set_running(self, running: bool):
        self.running = running
        state = "disabled" if running else "normal"
        for b in (self.btn_start, self.btn_preview, self.btn_add, self.btn_remove, self.btn_clear):
            b.configure(state=state)
        self.btn_cancel.configure(state="normal" if running else "disabled")
        self.btn_pause.configure(state="normal" if running else "disabled")
        tools.keep_awake(running)

    def _cleanup(self, temp_dir: str):
        try:
            from . import cleanup, updater
            updater.cleanup_old_versions()
            dirs = {temp_dir} | {
                f["job"].data["settings"].get("temp_dir", "") for f in self.files if f.get("job")}
            cleanup.startup_cleanup(dirs, self.jobs.known_ids,
                                    lambda m: self.events.put(("log", m)))
        except Exception:  # noqa: BLE001
            pass

    def _restore_jobs(self):
        """Recharge les traitements en pause / interrompus et les reprend automatiquement."""
        try:
            pending = self.jobs.load_pending()
        except Exception:  # noqa: BLE001
            pending = []
        for job in pending:
            iid = self.tree.insert("", "end", text=os.path.basename(job.path),
                                   values=("…", "…", "…", "En pause ⏸"))
            entry = {"path": job.path, "iid": iid, "info": None, "status": "En pause ⏸", "job": job}
            self.files.append(entry)
            threading.Thread(target=self._probe_entry, args=(entry,), daemon=True).start()
        if pending:
            self._log(f"{len(pending)} traitement(s) en pause retrouvé(s) : reprise automatique…")
            self.root.after(2500, lambda: None if self.running else self._start())

    def _start(self):
        todo = [f for f in self.files if f["status"] not in ("Terminé ✔",)]
        if not todo:
            messagebox.showinfo(APP_TITLE, "Ajoutez d'abord une ou plusieurs vidéos.")
            return
        settings = self._collect_settings()  # instantané figé au moment du clic
        self._save_settings()
        for f in todo:
            if not f.get("job"):
                try:
                    info = f.get("info") or engine.probe(f["path"])
                    plan = engine.make_plan(info, settings)
                    out = engine.default_output_path(info, settings, plan)
                except Exception as e:  # noqa: BLE001
                    self._set_status(f, "Erreur ✖")
                    self._log(f"✖ {os.path.basename(f['path'])} : {e}")
                    continue
                f["job"] = self.jobs.create(f["path"], out, settings)
        todo = [f for f in todo if f.get("job")]
        if not todo:
            return
        self._pause_req = False
        self._set_running(True)
        self.upscaler = None
        self.worker = threading.Thread(target=self._run_queue, args=(todo,), daemon=True)
        self.worker.start()

    def _run_queue(self, todo):
        ok, failed, outputs = 0, 0, []
        paused = False
        t0 = time.monotonic()
        for i, entry in enumerate(todo, 1):
            if self._pause_req:
                paused = True
                break
            if self.upscaler is not None and self.upscaler._user_cancel:
                break
            job = entry["job"]
            self.upscaler = Upscaler(on_log=lambda m: self.events.put(("log", m)),
                                     on_progress=lambda p: self.events.put(("progress", p)))
            self.events.put(("current", entry, i, len(todo)))
            self.events.put(("log", f"──── [{i}/{len(todo)}] {os.path.basename(entry['path'])}"))
            if (job.data.get("state") or {}).get("src_done"):
                self.events.put(("log", f"↻ Reprise à l'image {job.data['state']['src_done']}."))
            self.jobs.update(job, status="running")
            try:
                out = self.upscaler.process(job.path, job.settings, job.output, job_dir=job.work,
                                            state=dict(job.data.get("state") or {}),
                                            on_state=lambda st, job=job: self.jobs.update(
                                                job, state=json.loads(json.dumps(st))))
                outputs.append(out)
                ok += 1
                self.jobs.delete(job)
                entry["job"] = None
                self.events.put(("file_done", entry, "Terminé ✔", out))
                self.events.put(("log", f"✔ Terminé : {out}"))
            except engine.Paused:
                paused = True
                self.jobs.update(job, status="paused")
                self.events.put(("file_done", entry, "En pause ⏸", None))
                self.events.put(("log", "⏸ En pause : vous pouvez fermer l'application ou éteindre "
                                        "le PC, la reprise se fera automatiquement."))
                break
            except Cancelled:
                self.jobs.delete(job)
                entry["job"] = None
                self.events.put(("file_done", entry, "Annulé", None))
                self.events.put(("log", "Traitement annulé."))
                break
            except Exception as e:  # noqa: BLE001
                failed += 1
                self.jobs.delete(job)
                entry["job"] = None
                self.events.put(("file_done", entry, "Erreur ✖", None))
                if isinstance(e, UpscaleError):
                    self.events.put(("log", f"✖ Erreur : {e}"))
                else:
                    self.events.put(("log", f"✖ Erreur inattendue : {e}\n{traceback.format_exc()}"))
        for entry in todo:
            job = entry.get("job")
            if job and job.status != "paused":
                if paused:
                    self.jobs.update(job, status="paused")
                    self.events.put(("file_done", entry, "En pause ⏸", None))
                elif self.upscaler is not None and self.upscaler._user_cancel:
                    self.jobs.delete(job)
                    entry["job"] = None
        self.events.put(("queue_done", ok, failed, outputs, time.monotonic() - t0, paused))

    def _pause(self):
        if self.running and not self._pause_req:
            self._pause_req = True
            self.lbl_status.configure(text="Mise en pause… (enregistrement de l'avancement)")
            if self.upscaler:
                self.upscaler.pause()

    def _cancel(self):
        if self.upscaler and self.running:
            self.lbl_status.configure(text="Annulation en cours…")
            self.upscaler.cancel()

    def _preview(self):
        sel = self.tree.selection()
        entry = next((f for f in self.files if f["iid"] in sel), None) or (self.files[0] if self.files else None)
        if entry is None:
            messagebox.showinfo(APP_TITLE, "Ajoutez une vidéo (et sélectionnez-la) pour générer un aperçu.")
            return
        settings = self._collect_settings()
        self._set_running(True)
        self.pb_file.configure(mode="indeterminate")
        self.pb_file.start(12)
        self.lbl_status.configure(text=f"Aperçu de {os.path.basename(entry['path'])}…")
        self.upscaler = Upscaler(on_log=lambda m: self.events.put(("log", m)))

        def work():
            try:
                cmp_, ai = self.upscaler.preview(entry["path"], settings)
                self.events.put(("preview_done", cmp_, None))
            except Cancelled:
                self.events.put(("preview_done", None, "Aperçu annulé."))
            except Exception as e:  # noqa: BLE001
                self.events.put(("preview_done", None, str(e)))

        threading.Thread(target=work, daemon=True).start()

    def _open_output(self):
        folder = self.last_output_dir or self.var_outdir.get().strip()
        if not folder and self.files:
            folder = os.path.dirname(self.files[0]["path"])
        if folder and os.path.isdir(folder):
            tools.open_path(folder)

    # ------------------------------------------------------------------ événements
    def _log(self, msg: str):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", time.strftime("[%H:%M:%S] ") + msg + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _poll_events(self):
        try:
            for _ in range(200):
                ev = self.events.get_nowait()
                self._handle(ev)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_events)

    def _handle(self, ev):
        kind = ev[0]
        if kind == "log":
            self._log(ev[1])
        elif kind == "probed":
            _, entry, info, err = ev
            if not self.tree.exists(entry["iid"]):
                return
            if info:
                entry["info"] = info
                self.tree.set(entry["iid"], "res", f"{info.width}x{info.height}")
                self.tree.set(entry["iid"], "dur", fmt_time(info.duration))
                self._refresh_outputs()
            else:
                self._set_status(entry, "Illisible ✖")
                self._log(f"Impossible de lire {os.path.basename(entry['path'])} : {err}")
        elif kind == "gpus":
            _, gpus, raw = ev
            if gpus:
                items = [("auto", "Automatique")] + [(str(i), f"{i} – {n}") for i, n in gpus]
                self.cb_gpu.set_items(items)
                self.cb_gpu.set(getattr(self, "_saved_gpu", "auto"))
                self._log("Carte(s) graphique(s) détectée(s) : " + ", ".join(n for _, n in gpus))
            else:
                self._log("⚠ Aucune carte graphique compatible Vulkan détectée. Mettez à jour vos "
                          "pilotes graphiques (NVIDIA / AMD / Intel), sinon l'IA ne pourra pas "
                          "fonctionner.\n" + raw[-600:])
        elif kind == "current":
            _, entry, i, n = ev
            self._set_status(entry, "En cours…")
            self.current_label = f"[{i}/{n}] {os.path.basename(entry['path'])}"
            self.lbl_status.configure(text=f"{self.current_label} – démarrage…")
            self.pb_file.configure(mode="determinate", value=0)
        elif kind == "progress":
            p: engine.Progress = ev[1]
            frac = p.done / p.total if p.total else 0
            self.pb_file.configure(value=int(frac * 1000))
            label = getattr(self, "current_label", "")
            self.lbl_status.configure(text=f"{label} – {frac * 100:.1f} %")
            self.lbl_total.configure(
                text=f"{p.stage} : image {p.done} / {p.total}   •   {p.speed:.2f} images/s   •   "
                     f"écoulé {fmt_time(p.elapsed)}   •   restant ≈ {fmt_time(p.eta)}")
            for f in self.files:
                if f["status"].startswith("En cours"):
                    self._set_status(f, f"En cours {frac * 100:.0f} %")
        elif kind == "file_done":
            _, entry, status, out = ev
            self._set_status(entry, status)
            if out:
                self.last_output_dir = os.path.dirname(out)
        elif kind == "queue_done":
            _, ok, failed, outputs, elapsed, paused = ev
            self._set_running(False)
            if self._closing:
                self.root.destroy()
                return
            if paused:
                msg = ("En pause : avancement enregistré. Vous pouvez fermer l'application ou éteindre "
                       "le PC, la reprise se fera automatiquement à la même image.")
                self.lbl_status.configure(text=msg)
                self.btn_start.configure(text="▶  Reprendre")
                return
            self.btn_start.configure(text="▶  Démarrer")
            self.pb_file.configure(value=1000 if ok and not failed else self.pb_file["value"])
            msg = f"{ok} vidéo(s) terminée(s)" + (f", {failed} en erreur" if failed else "") + \
                  f" en {fmt_time(elapsed)}."
            self.lbl_status.configure(text=msg)
            self._log(msg)
            if outputs:
                if messagebox.askyesno(APP_TITLE, msg + "\n\nOuvrir le dossier de sortie ?"):
                    self._open_output()
            elif failed:
                messagebox.showerror(APP_TITLE, msg + "\n\nConsultez le journal pour les détails.")
        elif kind == "preview_done":
            _, path, err = ev
            self.pb_file.stop()
            self.pb_file.configure(mode="determinate", value=0)
            self._set_running(False)
            if path:
                self.lbl_status.configure(text="Aperçu prêt (gauche = original, droite = IA).")
                self._log(f"Aperçu enregistré : {path}")
                tools.open_path(path)
            else:
                self.lbl_status.configure(text="Échec de l'aperçu.")
                self._log("✖ " + (err or ""))
                messagebox.showerror(APP_TITLE, (err or "Erreur")[:1500])

    def _on_close(self):
        if self.running:
            # mise en pause propre : la reprise se fera au prochain lancement
            self._save_settings()
            self._closing = True
            self._pause()
            self.lbl_status.configure(text="Mise en pause avant fermeture…")
            self.root.after(60000, self.root.destroy)
            return
        self._save_settings()
        tools.keep_awake(False)
        self.root.destroy()


def _enable_dpi_awareness():
    if tools.IS_WIN:
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass


def create_root() -> tk.Tk:
    _enable_dpi_awareness()
    if _DND:
        try:
            return TkinterDnD.Tk()
        except Exception:
            pass
    return tk.Tk()


def main(argv=None):
    root = create_root()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main(sys.argv[1:])
