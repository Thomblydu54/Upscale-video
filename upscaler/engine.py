"""Moteur d'upscaling vidéo : FFmpeg (décodage/encodage) + Real-ESRGAN ncnn-vulkan (IA).

Pipeline (streaming, espace disque borné) :

    ffmpeg (décodage -> PNG sur stdout)
        -> thread lecteur : écrit les images par lots dans un dossier temporaire
        -> boucle IA      : realesrgan-ncnn-vulkan traite chaque lot sur le GPU
        -> thread écrivain: envoie les images agrandies à ffmpeg (encodage) puis les supprime
    ffmpeg (encodage H.264/H.265 + piste audio/sous-titres d'origine)
"""
from __future__ import annotations

import collections
import json
import math
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field, asdict
from fractions import Fraction
from typing import Callable, Optional

from . import tools

# --------------------------------------------------------------------------------------
# Catalogues
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Model:
    key: str
    label: str
    scales: tuple
    description: str


MODELS: list[Model] = [
    Model("upscayl-lite-4x", "Réaliste – Rapide (recommandé pour les vidéos)", (4,),
          "Films, séries, vidéos de famille, YouTube… Bon compromis vitesse / qualité."),
    Model("upscayl-standard-4x", "Réaliste – Haute qualité (lent)", (4,),
          "Plus de détails que le mode rapide, mais 5 à 10x plus lent."),
    Model("realesrgan-x4plus", "Real-ESRGAN x4plus – Réaliste, fort débruitage (lent)", (4,),
          "Le modèle Real-ESRGAN classique : très propre, lisse un peu les textures."),
    Model("realesr-animevideov3", "Animation / Anime – Rapide", (2, 3, 4),
          "Dessins animés, anime, 2D. Très rapide, supporte x2 / x3 / x4 natifs."),
    Model("realesrgan-x4plus-anime", "Animation / Anime – Haute qualité (lent)", (4,),
          "Anime avec un rendu plus net et plus fidèle, mais plus lent."),
]
MODELS_BY_KEY = {m.key: m for m in MODELS}

# clé -> (libellé, description de la taille cible)
OUTPUT_SIZES: list[tuple[str, str]] = [
    ("x2", "x2 (double la résolution)"),
    ("x3", "x3"),
    ("x4", "x4 (quadruple la résolution)"),
    ("720p", "720p (HD)"),
    ("1080p", "1080p (Full HD)"),
    ("1440p", "1440p (2K)"),
    ("2160p", "2160p (4K UHD)"),
    ("4320p", "4320p (8K)"),
]


@dataclass(frozen=True)
class Encoder:
    key: str
    label: str
    codec: str
    family: str  # h264 | hevc
    kind: str    # cpu | nvenc | amf | qsv


ENCODERS: list[Encoder] = [
    Encoder("x264", "H.264 – CPU (compatible partout)", "libx264", "h264", "cpu"),
    Encoder("x265", "H.265/HEVC – CPU (fichiers plus légers)", "libx265", "hevc", "cpu"),
    Encoder("nvenc_h264", "H.264 – NVIDIA NVENC (très rapide)", "h264_nvenc", "h264", "nvenc"),
    Encoder("nvenc_hevc", "H.265 – NVIDIA NVENC (très rapide)", "hevc_nvenc", "hevc", "nvenc"),
    Encoder("amf_h264", "H.264 – AMD AMF", "h264_amf", "h264", "amf"),
    Encoder("amf_hevc", "H.265 – AMD AMF", "hevc_amf", "hevc", "amf"),
    Encoder("qsv_h264", "H.264 – Intel Quick Sync", "h264_qsv", "h264", "qsv"),
    Encoder("qsv_hevc", "H.265 – Intel Quick Sync", "hevc_qsv", "hevc", "qsv"),
]
ENCODERS_BY_KEY = {e.key: e for e in ENCODERS}

SPEEDS = [("fast", "Rapide"), ("medium", "Équilibré"), ("slow", "Lent (meilleure compression)")]

MP4_AUDIO_OK = {"aac", "mp3", "ac3", "eac3", "alac", "opus", "flac", "mp2"}
TEXT_SUBS = {"subrip", "ass", "ssa", "mov_text", "webvtt", "text"}


@dataclass
class Settings:
    model: str = "upscayl-lite-4x"
    output_size: str = "x2"
    encoder: str = "x264"
    quality: int = 18           # CRF (0 = sans perte ... 51 = très compressé)
    speed: str = "medium"
    deinterlace: str = "auto"   # auto | on | off
    gpu: str = "auto"           # "auto" ou index GPU
    tile: int = 0               # 0 = auto
    tta: bool = False
    container: str = "mp4"      # mp4 | mkv
    output_dir: str = ""        # vide = même dossier que la source
    temp_dir: str = ""          # vide = dossier temporaire système

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Settings":
        s = cls()
        for k, v in (data or {}).items():
            if hasattr(s, k):
                try:
                    setattr(s, k, type(getattr(s, k))(v))
                except (TypeError, ValueError):
                    pass
        if s.model not in MODELS_BY_KEY:
            s.model = cls.model
        if s.encoder not in ENCODERS_BY_KEY:
            s.encoder = cls.encoder
        if s.output_size not in dict(OUTPUT_SIZES):
            s.output_size = cls.output_size
        return s


# --------------------------------------------------------------------------------------
# Analyse de la vidéo
# --------------------------------------------------------------------------------------


@dataclass
class VideoInfo:
    path: str
    width: int
    height: int
    fps: str              # fraction, ex. "30000/1001"
    fps_float: float
    duration: float
    frames: int           # estimation
    sar: Optional[str]    # ex. "32:27" (None si pixels carrés)
    field_order: str
    color_space: str
    pix_fmt: str
    codec: str
    audio: list = field(default_factory=list)      # [(index, codec)]
    subtitles: list = field(default_factory=list)  # [(index, codec)]

    def summary(self) -> str:
        return (f"{self.width}x{self.height} @ {self.fps_float:.3f} i/s, "
                f"{fmt_time(self.duration)}, ~{self.frames} images, {self.codec}"
                + (f", {len(self.audio)} piste(s) audio" if self.audio else ", sans audio"))


class UpscaleError(RuntimeError):
    pass


class Cancelled(Exception):
    pass


class _Stopped(Exception):
    """Interne : un autre thread a demandé l'arrêt."""


def _parse_rate(value: str) -> Optional[Fraction]:
    try:
        fr = Fraction(value)
        return fr if fr > 0 else None
    except (ValueError, ZeroDivisionError, TypeError):
        return None


def probe(path: str) -> VideoInfo:
    ffprobe = tools.find_tool("ffprobe")
    proc = tools.run([ffprobe, "-v", "error", "-print_format", "json",
                      "-show_streams", "-show_format", path],
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise UpscaleError(f"Impossible de lire la vidéo :\n{proc.stderr.decode('utf-8', 'replace')[-800:]}")
    data = json.loads(proc.stdout.decode("utf-8", "replace") or "{}")
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not s.get("disposition", {}).get("attached_pic")), None)
    if video is None:
        raise UpscaleError("Aucune piste vidéo trouvée dans ce fichier.")

    width, height = int(video.get("width", 0)), int(video.get("height", 0))
    rotation = 0
    for sd in video.get("side_data_list", []) or []:
        if "rotation" in sd:
            try:
                rotation = int(float(sd["rotation"]))
            except (TypeError, ValueError):
                pass
    if not rotation:
        try:
            rotation = int(video.get("tags", {}).get("rotate", 0))
        except (TypeError, ValueError):
            rotation = 0
    if abs(rotation) % 180 == 90:
        width, height = height, width

    avg = _parse_rate(video.get("avg_frame_rate", ""))
    real = _parse_rate(video.get("r_frame_rate", ""))
    rate = avg if (avg and avg <= 240) else real
    if not rate or rate > 240:
        rate = Fraction(30, 1)
    rate = rate.limit_denominator(1001000)

    duration = 0.0
    for src in (video.get("duration"), data.get("format", {}).get("duration")):
        try:
            duration = float(src)
            if duration > 0:
                break
        except (TypeError, ValueError):
            continue
    frames = int(round(duration * float(rate))) if duration > 0 else 0
    if frames <= 0:
        try:
            frames = int(video.get("nb_frames", 0))
        except (TypeError, ValueError):
            frames = 0

    sar = video.get("sample_aspect_ratio")
    if not sar or sar in ("1:1", "0:1", "N/A"):
        sar = None

    audio, subs = [], []
    for s in streams:
        if s.get("codec_type") == "audio":
            audio.append((int(s["index"]), s.get("codec_name", "")))
        elif s.get("codec_type") == "subtitle":
            subs.append((int(s["index"]), s.get("codec_name", "")))

    return VideoInfo(path=path, width=width, height=height,
                     fps=f"{rate.numerator}/{rate.denominator}", fps_float=float(rate),
                     duration=duration, frames=frames, sar=sar,
                     field_order=video.get("field_order", "unknown") or "unknown",
                     color_space=video.get("color_space", "") or "",
                     pix_fmt=video.get("pix_fmt", "") or "",
                     codec=video.get("codec_name", "?"), audio=audio, subtitles=subs)


# --------------------------------------------------------------------------------------
# Plan de traitement
# --------------------------------------------------------------------------------------


@dataclass
class Plan:
    ai_scale: int
    ai_w: int
    ai_h: int
    out_w: int
    out_h: int
    decode_filter: str
    encode_filter: str
    batch_size: int
    notes: list = field(default_factory=list)


def _even(x: float) -> int:
    return max(2, int(round(x / 2.0)) * 2)


def target_factor(info: VideoInfo, output_size: str) -> float:
    if output_size.startswith("x"):
        return float(output_size[1:])
    target = int(output_size.rstrip("p"))
    short_side = min(info.width, info.height)
    # pixels non carrés (DVD) : on raisonne sur la hauteur réelle
    return target / float(short_side)


def make_plan(info: VideoInfo, settings: Settings) -> Plan:
    model = MODELS_BY_KEY.get(settings.model, MODELS[0])
    factor = target_factor(info, settings.output_size)
    notes = []
    if factor <= 1.0:
        raise UpscaleError(
            f"La vidéo fait déjà {info.width}x{info.height} : la résolution choisie "
            f"({settings.output_size}) n'est pas plus grande.")
    candidates = sorted(model.scales)
    ai_scale = next((s for s in candidates if s >= factor - 1e-6), candidates[-1])
    if factor > ai_scale + 1e-6:
        notes.append(f"Facteur x{factor:.2f} > x{ai_scale} de l'IA : complété par un "
                     f"agrandissement classique (Lanczos).")
    ai_w, ai_h = info.width * ai_scale, info.height * ai_scale
    out_w, out_h = _even(info.width * factor), _even(info.height * factor)

    # --- filtres de décodage (vers RGB pour l'IA)
    dec = []
    if settings.deinterlace == "on":
        dec.append("bwdif=mode=send_frame:parity=auto:deint=all")
    elif settings.deinterlace == "auto":
        dec.append("bwdif=mode=send_frame:parity=auto:deint=interlaced")
    known = {"bt709": "bt709", "bt470bg": "bt601", "smpte170m": "bt601",
             "smpte240m": "smpte240m", "fcc": "fcc", "bt2020nc": "bt2020", "bt2020c": "bt2020"}
    matrix = known.get(info.color_space)
    if matrix is None:  # non renseigné : on devine comme les lecteurs vidéo
        matrix = "bt709" if min(info.width, info.height) >= 720 or info.width >= 1280 else "bt601"
    dec.append(f"scale=in_color_matrix={matrix}:in_range=auto")
    dec.append("format=rgb24")

    # --- filtres d'encodage (RGB -> YUV BT.709, redimensionnement final)
    enc = [f"scale={out_w}:{out_h}:flags=lanczos:out_color_matrix=bt709:out_range=tv",
           "format=yuv420p"]
    if info.sar:
        enc.append("setsar=" + info.sar.replace(":", "/"))

    # --- taille des lots : ~1,5 Go d'images agrandies par lot
    est_png = ai_w * ai_h * 3 * 0.6
    batch = int(1.5e9 / max(est_png, 1))
    batch = max(16, min(400, batch))

    if info.color_space.startswith("bt2020"):
        notes.append("Vidéo HDR / BT.2020 détectée : la sortie sera en SDR BT.709.")
    return Plan(ai_scale, ai_w, ai_h, out_w, out_h, ",".join(dec), ",".join(enc), batch, notes)


def default_output_path(info: VideoInfo, settings: Settings, plan: Plan) -> str:
    folder = settings.output_dir or os.path.dirname(os.path.abspath(info.path))
    stem = os.path.splitext(os.path.basename(info.path))[0]
    ext = settings.container if settings.container in ("mp4", "mkv") else "mp4"
    base = os.path.join(folder, f"{stem}_upscaled_{plan.out_w}x{plan.out_h}")
    path, n = f"{base}.{ext}", 1
    while os.path.exists(path):
        path = f"{base} ({n}).{ext}"
        n += 1
    return path


def encoder_args(settings: Settings) -> list:
    enc = ENCODERS_BY_KEY.get(settings.encoder, ENCODERS[0])
    q = max(0, min(51, int(settings.quality)))
    speed = settings.speed if settings.speed in dict(SPEEDS) else "medium"
    args = ["-c:v", enc.codec]
    if enc.kind == "cpu":
        args += ["-preset", speed, "-crf", str(q)]
        if enc.codec == "libx265":
            args += ["-x265-params", "log-level=error"]
    elif enc.kind == "nvenc":
        preset = {"fast": "p3", "medium": "p5", "slow": "p7"}[speed]
        args += ["-preset", preset, "-rc", "vbr", "-cq", str(max(1, q)), "-b:v", "0"]
    elif enc.kind == "amf":
        quality = {"fast": "speed", "medium": "balanced", "slow": "quality"}[speed]
        args += ["-quality", quality, "-rc", "cqp", "-qp_i", str(q), "-qp_p", str(q)]
        if enc.family == "h264":
            args += ["-qp_b", str(q)]
    elif enc.kind == "qsv":
        preset = {"fast": "faster", "medium": "medium", "slow": "slower"}[speed]
        args += ["-preset", preset, "-global_quality", str(max(1, q))]
    if enc.family == "hevc" and settings.container == "mp4":
        args += ["-tag:v", "hvc1"]  # lisible sur Windows / Apple
    return args


def build_decoder_cmd(info: VideoInfo, plan: Plan) -> list:
    return [tools.find_tool("ffmpeg"), "-hide_banner", "-nostdin", "-loglevel", "error",
            "-i", info.path, "-map", "0:v:0", "-an", "-sn", "-dn",
            "-vf", plan.decode_filter, "-fps_mode", "cfr", "-r", info.fps,
            "-c:v", "png", "-compression_level", "1", "-f", "image2pipe", "-"]


def build_encoder_cmd(info: VideoInfo, plan: Plan, settings: Settings, output: str) -> list:
    cmd = [tools.find_tool("ffmpeg"), "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
           "-f", "image2pipe", "-framerate", info.fps, "-c:v", "png", "-i", "-",
           "-i", info.path,
           "-map", "0:v:0", "-vf", plan.encode_filter]
    cmd += encoder_args(settings)
    cmd += ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
            "-color_range", "tv"]

    # audio
    if info.audio:
        for idx, _ in info.audio:
            cmd += ["-map", f"1:{idx}"]
        codecs = {c for _, c in info.audio}
        if settings.container == "mkv" or codecs <= MP4_AUDIO_OK:
            cmd += ["-c:a", "copy"]
        else:
            cmd += ["-c:a", "aac", "-b:a", "256k"]
    # sous-titres
    if settings.container == "mkv":
        for idx, _ in info.subtitles:
            cmd += ["-map", f"1:{idx}"]
        if info.subtitles:
            cmd += ["-c:s", "copy"]
    else:
        text = [(i, c) for i, c in info.subtitles if c in TEXT_SUBS]
        for idx, _ in text:
            cmd += ["-map", f"1:{idx}"]
        if text:
            cmd += ["-c:s", "mov_text"]

    cmd += ["-map_metadata", "1", "-map_chapters", "1", "-max_muxing_queue_size", "9999"]
    if settings.container == "mp4":
        cmd += ["-movflags", "+faststart"]
    cmd += [output]
    return cmd


def build_ai_cmd(settings: Settings, plan: Plan, src: str, dst: str) -> list:
    cmd = [tools.find_tool(tools.UPSCALER_BIN), "-i", src, "-o", dst,
           "-n", settings.model, "-s", str(plan.ai_scale), "-m", tools.models_dir(), "-f", "png"]
    if str(settings.gpu).strip().lower() not in ("", "auto"):
        cmd += ["-g", str(settings.gpu)]
    if settings.tile and int(settings.tile) >= 32:
        cmd += ["-t", str(int(settings.tile))]
    if settings.tta:
        cmd += ["-x"]
    return cmd


# --------------------------------------------------------------------------------------
# Utilitaires
# --------------------------------------------------------------------------------------

PNG_SIG = b"\x89PNG\r\n\x1a\n"


def iter_png_stream(stream):
    """Découpe un flux image2pipe (PNG concaténés) en images individuelles."""
    while True:
        sig = stream.read(8)
        if not sig:
            return
        if len(sig) < 8 or sig != PNG_SIG:
            raise UpscaleError("Flux d'images corrompu reçu de ffmpeg.")
        parts = [sig]
        while True:
            header = stream.read(8)
            if len(header) < 8:
                raise UpscaleError("Flux d'images tronqué reçu de ffmpeg.")
            length = int.from_bytes(header[:4], "big")
            body = stream.read(length + 4)
            if len(body) < length + 4:
                raise UpscaleError("Flux d'images tronqué reçu de ffmpeg.")
            parts.append(header)
            parts.append(body)
            if header[4:8] == b"IEND":
                break
        yield b"".join(parts)


def fmt_time(seconds: float) -> str:
    if seconds is None or seconds != seconds or seconds == math.inf:
        return "--:--:--"
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def _tail(path: str, n: int = 1500) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - n))
            return f.read().decode("utf-8", "replace").strip()
    except OSError:
        return ""


def _count_png(folder: str) -> int:
    try:
        with os.scandir(folder) as it:
            return sum(1 for e in it if e.name.endswith(".png"))
    except OSError:
        return 0


@dataclass
class Progress:
    done: int
    total: int
    speed: float      # images / seconde
    eta: float        # secondes
    elapsed: float
    stage: str = ""


# --------------------------------------------------------------------------------------
# Moteur
# --------------------------------------------------------------------------------------


class Upscaler:
    def __init__(self, on_log: Callable[[str], None] = print,
                 on_progress: Optional[Callable[[Progress], None]] = None):
        self.on_log = on_log
        self.on_progress = on_progress or (lambda p: None)
        self._stop = threading.Event()
        self._user_cancel = False
        self._procs: list[subprocess.Popen] = []
        self._lock = threading.Lock()
        self._errors: list[BaseException] = []

    # ---- contrôle ---------------------------------------------------------------
    def cancel(self) -> None:
        self._user_cancel = True
        self._stop.set()
        self._kill_all()

    def _kill_all(self) -> None:
        with self._lock:
            procs = list(self._procs)
        for p in procs:
            try:
                if p.poll() is None:
                    p.kill()
            except Exception:
                pass

    def _register(self, proc: subprocess.Popen) -> subprocess.Popen:
        with self._lock:
            self._procs.append(proc)
        return proc

    def _fail(self, exc: BaseException) -> None:
        with self._lock:
            self._errors.append(exc)
        self._stop.set()
        self._kill_all()

    def _put(self, q: queue.Queue, item) -> None:
        while True:
            if self._stop.is_set():
                raise _Stopped()
            try:
                q.put(item, timeout=0.2)
                return
            except queue.Full:
                continue

    def _get(self, q: queue.Queue):
        while True:
            if self._stop.is_set():
                raise _Stopped()
            try:
                return q.get(timeout=0.2)
            except queue.Empty:
                continue

    def _reset(self) -> None:
        self._stop.clear()
        self._user_cancel = False
        self._errors = []
        with self._lock:
            self._procs = []

    # ---- progression ------------------------------------------------------------
    def _report(self, done: int, stage: str = "", force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_report < 0.4:
            return
        self._last_report = now
        self._samples.append((now, done))
        while len(self._samples) > 2 and now - self._samples[0][0] > 30:
            self._samples.popleft()
        t0, d0 = self._samples[0]
        speed = (done - d0) / (now - t0) if now - t0 > 0.5 else 0.0
        total = max(self._total, done)
        eta = (total - done) / speed if speed > 0 else float("inf")
        self.on_progress(Progress(done, total, speed, eta, now - self._t_start, stage))

    # ---- aperçu -----------------------------------------------------------------
    def preview(self, path: str, settings: Settings, position: float = 0.5) -> tuple[str, str]:
        """Agrandit une seule image de la vidéo. Retourne (comparaison.png, image_ia.png)."""
        self._reset()
        info = probe(path)
        plan = make_plan(info, settings)
        folder = os.path.join(tempfile.gettempdir(), "UpscaleVideoAI_apercus")
        os.makedirs(folder, exist_ok=True)
        stem = os.path.splitext(os.path.basename(path))[0][:60]
        src = os.path.join(folder, f"{stem}_source.png")
        ai = os.path.join(folder, f"{stem}_ia_x{plan.ai_scale}.png")
        cmp_ = os.path.join(folder, f"{stem}_comparaison.png")
        t = max(0.0, info.duration * position) if info.duration else 0.0
        ffmpeg = tools.find_tool("ffmpeg")

        self.on_log(f"Aperçu : extraction de l'image à {fmt_time(t)}…")
        proc = tools.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                          "-ss", f"{t:.3f}", "-i", path, "-frames:v", "1",
                          "-vf", plan.decode_filter, src],
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if proc.returncode != 0 or not os.path.isfile(src):
            raise UpscaleError("Extraction de l'image impossible :\n"
                               + proc.stderr.decode("utf-8", "replace")[-800:])

        self.on_log("Aperçu : passage dans l'IA…")
        if os.path.exists(ai):
            os.remove(ai)
        log = os.path.join(folder, "realesrgan.log")
        with open(log, "wb") as lf:
            p = self._register(tools.popen(build_ai_cmd(settings, plan, src, ai),
                                           stdout=lf, stderr=subprocess.STDOUT,
                                           stdin=subprocess.DEVNULL))
            p.wait()
        if self._user_cancel:
            raise Cancelled()
        if not os.path.isfile(ai):
            raise UpscaleError(self._ai_error_message(log))

        self.on_log("Aperçu : création de la comparaison (gauche = original, droite = IA)…")
        w, h = plan.out_w, plan.out_h
        proc = tools.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                          "-i", src, "-i", ai, "-filter_complex",
                          f"[0:v]scale={w}:{h}:flags=bicubic[a];"
                          f"[1:v]scale={w}:{h}:flags=lanczos[b];[a][b]hstack=inputs=2",
                          "-frames:v", "1", cmp_],
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if proc.returncode != 0:
            raise UpscaleError("Création de la comparaison impossible :\n"
                               + proc.stderr.decode("utf-8", "replace")[-800:])
        return cmp_, ai

    @staticmethod
    def _ai_error_message(log_path: str) -> str:
        tail = _tail(log_path)
        hint = ""
        low = tail.lower()
        if "vkcreateinstance" in low or "invalid gpu" in low or "no gpu" in low or not tail:
            hint = ("\n\nAucun GPU compatible Vulkan n'a été trouvé. Mettez à jour les pilotes "
                    "de votre carte graphique (NVIDIA / AMD / Intel).")
        elif "out of memory" in low or "vkallocatememory" in low or "vk_error" in low:
            hint = ("\n\nMémoire vidéo insuffisante : choisissez une taille de tuile plus petite "
                    "(ex. 128 ou 64) dans les réglages avancés.")
        return f"L'IA (Real-ESRGAN) a échoué.{hint}\n\nJournal :\n{tail[-1000:]}"

    # ---- traitement complet -----------------------------------------------------
    def process(self, path: str, settings: Settings, output: Optional[str] = None) -> str:
        self._reset()
        info = probe(path)
        plan = make_plan(info, settings)
        output = output or default_output_path(info, settings, plan)
        os.makedirs(os.path.dirname(os.path.abspath(output)), exist_ok=True)

        self.on_log(f"Source : {info.summary()}")
        self.on_log(f"Sortie : {plan.out_w}x{plan.out_h} (IA x{plan.ai_scale} → "
                    f"{plan.ai_w}x{plan.ai_h}) – modèle {settings.model}")
        for n in plan.notes:
            self.on_log("Note : " + n)

        work = tempfile.mkdtemp(prefix="upv_", dir=settings.temp_dir or None)
        try:
            free = shutil.disk_usage(work).free
            need = plan.batch_size * plan.ai_w * plan.ai_h * 3 * 0.6 * 3
            if free < need:
                self.on_log(f"Attention : peu d'espace disque libre dans le dossier temporaire "
                            f"({free / 1e9:.1f} Go, ~{need / 1e9:.1f} Go recommandés).")
            self._pipeline(info, plan, settings, output, work)
        except BaseException:
            try:
                if os.path.exists(output):
                    os.remove(output)
            except OSError:
                pass
            raise
        finally:
            shutil.rmtree(work, ignore_errors=True)
        return output

    def _pipeline(self, info: VideoInfo, plan: Plan, settings: Settings,
                  output: str, work: str) -> None:
        self._total = max(1, info.frames)
        self._done_ai = 0
        self._samples = collections.deque()
        self._t_start = time.monotonic()
        self._last_report = 0.0

        dec_log = os.path.join(work, "decoder.log")
        enc_log = os.path.join(work, "encoder.log")
        ai_log = os.path.join(work, "realesrgan.log")

        dec_cmd = build_decoder_cmd(info, plan)
        enc_cmd = build_encoder_cmd(info, plan, settings, output)

        with open(dec_log, "wb") as dlf, open(enc_log, "wb") as elf:
            dec = self._register(tools.popen(dec_cmd, stdout=subprocess.PIPE, stderr=dlf,
                                             stdin=subprocess.DEVNULL, bufsize=1 << 20))
            enc = self._register(tools.popen(enc_cmd, stdin=subprocess.PIPE, stderr=elf,
                                             stdout=subprocess.DEVNULL, bufsize=1 << 20))
            ai_q: queue.Queue = queue.Queue(maxsize=1)
            enc_q: queue.Queue = queue.Queue(maxsize=1)
            self._encoded = 0

            reader = threading.Thread(target=self._reader_thread,
                                      args=(dec.stdout, work, plan.batch_size, ai_q), daemon=True)
            writer = threading.Thread(target=self._writer_thread,
                                      args=(enc.stdin, enc_q), daemon=True)
            reader.start()
            writer.start()
            self._report(0, "IA", force=True)
            try:
                self._ai_loop(settings, plan, ai_q, enc_q, ai_log)
            except _Stopped:
                pass
            except BaseException as e:  # noqa: BLE001
                self._fail(e)

            reader.join()
            writer.join()

            if self._user_cancel:
                self._kill_all()
                raise Cancelled()
            if self._errors:
                self._kill_all()
                err = self._errors[0]
                if isinstance(err, BrokenPipeError) or (isinstance(err, OSError)
                                                        and not isinstance(err, UpscaleError)):
                    enc.wait()
                    raise UpscaleError("L'encodeur vidéo s'est arrêté :\n" + _tail(enc_log))
                raise err

            self._report(self._done_ai, "Finalisation", force=True)
            self.on_log("Finalisation de l'encodage…")
            dec_rc = dec.wait()
            enc_rc = enc.wait()
            if dec_rc != 0:
                raise UpscaleError("Le décodage de la vidéo a échoué :\n" + _tail(dec_log))
            if enc_rc != 0:
                raise UpscaleError("L'encodage de la vidéo a échoué :\n" + _tail(enc_log))
            if self._encoded == 0:
                raise UpscaleError("Aucune image n'a été produite.\n" + _tail(dec_log))
            self._total = self._encoded
            self._report(self._encoded, "Terminé", force=True)

    def _reader_thread(self, stream, work: str, batch_size: int, ai_q: queue.Queue) -> None:
        try:
            index, batch, count, folder = 0, 0, 0, None
            for png in iter_png_stream(stream):
                if self._stop.is_set():
                    return
                if folder is None:
                    folder = os.path.join(work, f"b{batch:05d}_in")
                    os.makedirs(folder, exist_ok=True)
                index += 1
                with open(os.path.join(folder, f"{index:08d}.png"), "wb") as f:
                    f.write(png)
                count += 1
                if count >= batch_size:
                    self._put(ai_q, (folder, count))
                    batch, count, folder = batch + 1, 0, None
            if folder is not None and count:
                self._put(ai_q, (folder, count))
            self._total = max(index, 1)
            self._put(ai_q, None)
        except _Stopped:
            pass
        except BaseException as e:  # noqa: BLE001
            if not self._stop.is_set():
                self._fail(e)

    def _ai_loop(self, settings: Settings, plan: Plan, ai_q: queue.Queue,
                 enc_q: queue.Queue, log_path: str) -> None:
        while True:
            item = self._get(ai_q)
            if item is None:
                self._put(enc_q, None)
                return
            in_dir, count = item
            out_dir = in_dir[:-3] + "_out"
            os.makedirs(out_dir, exist_ok=True)
            base = self._done_ai
            with open(log_path, "wb") as lf:
                proc = self._register(tools.popen(build_ai_cmd(settings, plan, in_dir, out_dir),
                                                  stdout=lf, stderr=subprocess.STDOUT,
                                                  stdin=subprocess.DEVNULL))
                while proc.poll() is None:
                    if self._stop.is_set():
                        proc.kill()
                        proc.wait()
                        raise _Stopped()
                    time.sleep(0.3)
                    self._report(base + _count_png(out_dir), "IA")
            with self._lock:
                self._procs.remove(proc)
            produced = _count_png(out_dir)
            if self._stop.is_set():
                raise _Stopped()
            if proc.returncode != 0 or produced < count:
                raise UpscaleError(self._ai_error_message(log_path))
            self._done_ai = base + count
            self._report(self._done_ai, "IA", force=True)
            shutil.rmtree(in_dir, ignore_errors=True)
            self._put(enc_q, (out_dir, count))

    def _writer_thread(self, stdin, enc_q: queue.Queue) -> None:
        try:
            while True:
                item = self._get(enc_q)
                if item is None:
                    break
                out_dir, _ = item
                for name in sorted(n for n in os.listdir(out_dir) if n.endswith(".png")):
                    if self._stop.is_set():
                        raise _Stopped()
                    p = os.path.join(out_dir, name)
                    with open(p, "rb") as f:
                        stdin.write(f.read())
                    os.remove(p)
                    self._encoded += 1
                shutil.rmtree(out_dir, ignore_errors=True)
            stdin.close()
        except _Stopped:
            pass
        except BaseException as e:  # noqa: BLE001
            if not self._stop.is_set():
                self._fail(e)
        finally:
            try:
                stdin.close()
            except Exception:
                pass
