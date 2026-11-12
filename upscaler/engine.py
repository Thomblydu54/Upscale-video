"""Moteur d'upscaling vidéo : FFmpeg (décodage/encodage) + Real-ESRGAN ncnn-vulkan (IA)
+ RIFE ncnn-vulkan (interpolation d'images, optionnelle).

Pipeline (streaming, espace disque borné) :

    ffmpeg (décodage -> PNG sur stdout)
        -> thread lecteur : écrit les images par lots dans un dossier temporaire
        -> boucle IA      : (RIFE crée les images intermédiaires) puis
                            realesrgan-ncnn-vulkan agrandit chaque lot sur le GPU
        -> thread écrivain: envoie les images à ffmpeg (encodage) puis les supprime
    ffmpeg (encodage H.264/H.265 + piste audio/sous-titres d'origine)

Les fichiers image (PNG, JPEG, WebP…) sont traités à part et donnent une image.
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
    ("x1", "Taille d'origine (sans agrandissement)"),
]

# Interpolation d'images (RIFE) : multiplie le nombre d'images par seconde
INTERPOLATIONS: list[tuple[str, str]] = [
    ("off", "Désactivée"),
    ("x2", "x2 – deux fois plus fluide (ex. 30 → 60 i/s)"),
    ("x3", "x3 (ex. 24 → 72 i/s)"),
    ("x4", "x4 – ralenti fluide (ex. 30 → 120 i/s)"),
]
RIFE_BIN = "rife-ncnn-vulkan"
RIFE_MODEL = "rife-v4.6"
SCENE_THRESHOLD = 0.32  # au-delà : changement de plan (pas d'interpolation entre deux plans)

# Formats de sortie pour les images
IMAGE_FORMATS: list[tuple[str, str]] = [
    ("auto", "Comme l'original"),
    ("png", "PNG (sans perte)"),
    ("jpg", "JPEG (léger)"),
    ("webp", "WebP"),
]
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".jfif", ".webp", ".bmp", ".tif", ".tiff", ".tga",
             ".jp2", ".avif", ".heic", ".heif")


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
    auto_update: bool = True    # recherche de mises à jour au démarrage
    interpolation: str = "off"  # off | x2 | x3 | x4 (RIFE)
    image_format: str = "auto"  # auto | png | jpg | webp (sortie des images)

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
        if s.interpolation not in dict(INTERPOLATIONS):
            s.interpolation = cls.interpolation
        if s.image_format not in dict(IMAGE_FORMATS):
            s.image_format = cls.image_format
        return s

    @property
    def interp_factor(self) -> int:
        return int(self.interpolation[1:]) if self.interpolation.startswith("x") else 1


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
    is_image: bool = False
    has_alpha: bool = False

    def summary(self) -> str:
        if self.is_image:
            return f"image {self.width}x{self.height} ({self.codec})" + (", transparence" if self.has_alpha else "")
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

    fmt_name = str(data.get("format", {}).get("format_name", ""))
    codec = video.get("codec_name", "") or ""
    is_image = (fmt_name == "image2" or fmt_name.endswith("_pipe")
                or (path.lower().endswith(IMAGE_EXT) and codec != "gif")) and frames <= 1
    if is_image:
        rotation = 0
        duration, frames, rate = 0.0, 1, Fraction(25, 1)
    pix_fmt = video.get("pix_fmt", "") or ""
    has_alpha = is_image and (pix_fmt.startswith(("rgba", "bgra", "argb", "abgr", "ya", "yuva", "gbrap"))
                              or pix_fmt in ("pal8", "rgb32", "bgr32") or "a64" in pix_fmt)

    audio, subs = [], []
    for s in streams:
        if s.get("codec_type") == "audio":
            audio.append((int(s["index"]), s.get("codec_name", "")))
        elif s.get("codec_type") == "subtitle":
            subs.append((int(s["index"]), s.get("codec_name", "")))

    if is_image:
        audio, subs = [], []
    return VideoInfo(path=path, width=width, height=height,
                     fps=f"{rate.numerator}/{rate.denominator}", fps_float=float(rate),
                     duration=duration, frames=frames, sar=sar,
                     field_order=video.get("field_order", "unknown") or "unknown",
                     color_space=video.get("color_space", "") or "",
                     pix_fmt=pix_fmt, codec=codec or "?", audio=audio, subtitles=subs,
                     is_image=is_image, has_alpha=has_alpha)


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
    upscale: bool = True        # False : taille d'origine (interpolation seule)
    interp: int = 1             # multiplicateur d'images par seconde (RIFE)
    out_fps: str = ""           # fréquence de sortie (fraction)


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
    if info.is_image:
        return make_image_plan(info, settings)
    model = MODELS_BY_KEY.get(settings.model, MODELS[0])
    factor = target_factor(info, settings.output_size)
    interp = settings.interp_factor
    notes = []
    if settings.output_size == "x1":
        if interp <= 1:
            raise UpscaleError(
                "« Taille d'origine » ne fait rien sans interpolation : choisissez une taille de "
                "sortie plus grande, ou activez l'interpolation d'images.")
        return _make_interp_only_plan(info, settings, interp)
    if factor <= 1.0:
        if interp > 1:  # vidéo déjà assez grande : on garde l'interpolation seule
            plan = _make_interp_only_plan(info, settings, interp)
            plan.notes.append(f"La vidéo fait déjà {info.width}x{info.height} : pas d'agrandissement, "
                              "interpolation seulement.")
            return plan
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

    dec = [_decode_filter(info, settings)]

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
    if interp > 1:
        batch = max(16, batch // interp)
    return Plan(ai_scale, ai_w, ai_h, out_w, out_h, ",".join(dec), ",".join(enc), batch, notes,
                upscale=True, interp=interp, out_fps=_mul_fps(info.fps, interp))


def _mul_fps(fps: str, k: int) -> str:
    fr = Fraction(fps) * k
    return f"{fr.numerator}/{fr.denominator}"


def _decode_filter(info: VideoInfo, settings: Settings) -> str:
    dec = []
    if settings.deinterlace == "on":
        dec.append("bwdif=mode=send_frame:parity=auto:deint=all")
    elif settings.deinterlace == "auto":
        dec.append("bwdif=mode=send_frame:parity=auto:deint=interlaced")
    known = {"bt709": "bt709", "bt470bg": "bt601", "smpte170m": "bt601",
             "smpte240m": "smpte240m", "fcc": "fcc", "bt2020nc": "bt2020", "bt2020c": "bt2020"}
    matrix = known.get(info.color_space)
    if matrix is None:
        matrix = "bt709" if min(info.width, info.height) >= 720 or info.width >= 1280 else "bt601"
    dec.append(f"scale=in_color_matrix={matrix}:in_range=auto")
    dec.append("format=rgb24")
    return ",".join(dec)


def _make_interp_only_plan(info: VideoInfo, settings: Settings, interp: int) -> Plan:
    w, h = _even(info.width), _even(info.height)
    enc = [f"scale={w}:{h}:flags=lanczos:out_color_matrix=bt709:out_range=tv", "format=yuv420p"]
    if info.sar:
        enc.append("setsar=" + info.sar.replace(":", "/"))
    batch = int(1.5e9 / max(info.width * info.height * 3 * 0.6 * interp, 1))
    batch = max(16, min(300, batch))
    notes = []
    if info.color_space.startswith("bt2020"):
        notes.append("Vidéo HDR / BT.2020 détectée : la sortie sera en SDR BT.709.")
    return Plan(1, info.width, info.height, w, h, _decode_filter(info, settings), ",".join(enc),
                batch, notes, upscale=False, interp=interp, out_fps=_mul_fps(info.fps, interp))


def make_image_plan(info: VideoInfo, settings: Settings) -> Plan:
    model = MODELS_BY_KEY.get(settings.model, MODELS[0])
    if settings.output_size == "x1":
        raise UpscaleError("« Taille d'origine » : rien à faire pour une image. "
                           "Choisissez x2, x4 ou une résolution (4K…).")
    factor = target_factor(info, settings.output_size)
    if factor <= 1.0:
        raise UpscaleError(
            f"L'image fait déjà {info.width}x{info.height} : la résolution choisie "
            f"({settings.output_size}) n'est pas plus grande.")
    candidates = sorted(model.scales)
    ai_scale = next((s for s in candidates if s >= factor - 1e-6), candidates[-1])
    notes = []
    if factor > ai_scale + 1e-6:
        notes.append(f"Facteur x{factor:.2f} > x{ai_scale} de l'IA : complété par un "
                     f"agrandissement classique (Lanczos).")
    out_w = max(1, int(round(info.width * factor)))
    out_h = max(1, int(round(info.height * factor)))
    fmt = "rgba" if info.has_alpha else "rgb24"
    return Plan(ai_scale, info.width * ai_scale, info.height * ai_scale, out_w, out_h,
                f"format={fmt}", "", 1, notes, upscale=True, interp=1, out_fps="")


def image_output_ext(info: VideoInfo, settings: Settings) -> str:
    fmt = settings.image_format
    if fmt == "auto":
        src = os.path.splitext(info.path)[1].lower()
        if src in (".jpg", ".jpeg", ".jfif"):
            return "jpg"
        if src == ".webp":
            return "webp"
        return "png"
    return fmt if fmt in ("png", "jpg", "webp") else "png"


def default_output_path(info: VideoInfo, settings: Settings, plan: Plan) -> str:
    folder = settings.output_dir or os.path.dirname(os.path.abspath(info.path))
    stem = os.path.splitext(os.path.basename(info.path))[0]
    if info.is_image:
        ext = image_output_ext(info, settings)
        tag = f"upscaled_{plan.out_w}x{plan.out_h}"
    else:
        ext = settings.container if settings.container in ("mp4", "mkv") else "mp4"
        parts = []
        if plan.upscale:
            parts.append(f"upscaled_{plan.out_w}x{plan.out_h}")
        if plan.interp > 1:
            parts.append(f"{float(Fraction(plan.out_fps)):.4g}fps")
        tag = "_".join(parts) or "upscaled"
    base = os.path.join(folder, f"{stem}_{tag}")
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
           "-f", "image2pipe", "-framerate", plan.out_fps or info.fps, "-c:v", "png", "-i", "-",
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


def rife_model_dir() -> str:
    return os.path.join(tools.bin_dir(), RIFE_MODEL)


def build_rife_cmd(settings: Settings, plan: Plan, info: VideoInfo, src: str, dst: str,
                   numframe: int) -> list:
    cmd = [tools.find_tool(RIFE_BIN), "-i", src, "-o", dst, "-n", str(numframe),
           "-m", rife_model_dir(), "-f", "%08d.png"]
    if str(settings.gpu).strip().lower() not in ("", "auto"):
        cmd += ["-g", str(settings.gpu)]
    if max(info.width, info.height) >= 2560:
        cmd += ["-u"]  # mode UHD : mouvements amples sur les très grandes images
    return cmd


def detect_scene_cuts(info: VideoInfo, plan: Plan, on_frame: Optional[Callable[[int], None]] = None,
                      register: Optional[Callable] = None) -> set:
    """Indices (0 = première image) des images qui commencent un nouveau plan."""
    deint = plan.decode_filter.split(",")[0] if plan.decode_filter.startswith("bwdif") else ""
    chain = ",".join(x for x in (deint, f"fps={info.fps}", "setpts=N", "scale=256:-2",
                                 f"select='gt(scene,{SCENE_THRESHOLD})'", "metadata=print") if x)
    cmd = [tools.find_tool("ffmpeg"), "-hide_banner", "-nostdin", "-nostats", "-loglevel", "info",
           "-i", info.path, "-map", "0:v:0", "-an", "-sn", "-dn", "-vf", chain,
           "-f", "null", "-", "-progress", "pipe:1"]
    log = tempfile.TemporaryFile()
    proc = tools.popen(cmd, stdout=subprocess.PIPE, stderr=log, stdin=subprocess.DEVNULL)
    if register:
        register(proc)
    for line in proc.stdout:
        if line.startswith(b"frame=") and on_frame:
            try:
                on_frame(int(line[6:].strip() or 0))
            except ValueError:
                pass
    proc.wait()
    log.seek(0)
    text = log.read().decode("utf-8", "replace")
    log.close()
    import re
    cuts = {int(m.group(1)) for m in re.finditer(r"\bframe:\d+\s+pts:(\d+)", text)}
    cuts.discard(0)
    return cuts


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


def fmt_time_ms(seconds: float) -> str:
    """hh:mm:ss.cc"""
    seconds = max(0.0, float(seconds or 0))
    whole = int(seconds)
    cs = int(round((seconds - whole) * 100))
    if cs == 100:
        whole, cs = whole + 1, 0
    return f"{fmt_time(whole)}.{cs:02d}"


def _seek(t: float) -> list:
    """Option de recherche ffmpeg (aucune à 0 : « -ss 0 » ne produit rien sur une image fixe)."""
    return ["-ss", f"{t:.4f}"] if t and t > 0 else []


def grab_frame_jpeg(path: str, t: float, width: int = 1280, quality: int = 3) -> bytes:
    """Extrait l'image affichée au temps `t` (recherche précise) en JPEG."""
    proc = tools.run([tools.find_tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin",
                      *_seek(t), "-i", path, "-frames:v", "1", "-an", "-sn",
                      "-vf", f"scale='min({width},iw)':-2:flags=bicubic", "-q:v", str(quality),
                      "-f", "image2pipe", "-c:v", "mjpeg", "-"],
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    if proc.returncode != 0 or not proc.stdout:
        raise UpscaleError("Impossible d'extraire l'image :\n"
                           + proc.stderr.decode("utf-8", "replace")[-400:])
    return proc.stdout


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
        self._interp = 1
        self._tail_dups = 0
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
    def preview(self, path: str, settings: Settings, position: float = 0.5,
                time_s: Optional[float] = None) -> tuple[str, str]:
        """Agrandit une seule image de la vidéo. Retourne (comparaison.png, image_ia.png).

        `time_s` (secondes) choisit précisément l'image ; sinon `position` (0..1 de la durée)."""
        self._reset()
        info = probe(path)
        plan = make_plan(info, settings)
        if not plan.upscale:
            raise UpscaleError("L'aperçu montre l'agrandissement par l'IA. Choisissez une taille de "
                               "sortie plus grande que l'original (x2, 4K…) pour le voir.")
        if info.is_image:
            time_s = 0.0
        folder = os.path.join(tempfile.gettempdir(), "UpscaleVideoAI_apercus")
        os.makedirs(folder, exist_ok=True)
        stem = os.path.splitext(os.path.basename(path))[0][:60]
        src = os.path.join(folder, f"{stem}_source.png")
        ai = os.path.join(folder, f"{stem}_ia_x{plan.ai_scale}.png")
        cmp_ = os.path.join(folder, f"{stem}_comparaison.png")
        if time_s is not None:
            t = max(0.0, float(time_s))
        else:
            t = max(0.0, info.duration * position) if info.duration else 0.0
        ffmpeg = tools.find_tool("ffmpeg")

        self.on_log(f"Aperçu : extraction de l'image à {fmt_time_ms(t)}…")
        proc = tools.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                          *_seek(t), "-i", path, "-frames:v", "1",
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
        if plan.upscale:
            self.on_log(f"Sortie : {plan.out_w}x{plan.out_h} (IA x{plan.ai_scale} → "
                        f"{plan.ai_w}x{plan.ai_h}) – modèle {settings.model}")
        else:
            self.on_log(f"Sortie : {plan.out_w}x{plan.out_h} (taille d'origine)")
        if plan.interp > 1:
            self.on_log(f"Interpolation RIFE x{plan.interp} : {info.fps_float:.3f} → "
                        f"{float(Fraction(plan.out_fps)):.3f} images/s")
        for n in plan.notes:
            self.on_log("Note : " + n)
        if info.is_image:
            return self._process_image(info, plan, settings, output)

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

    # ---- images -------------------------------------------------------------------
    def _process_image(self, info: VideoInfo, plan: Plan, settings: Settings, output: str) -> str:
        self._total, self._samples, self._last_report = 1, collections.deque(), 0.0
        self._t_start = time.monotonic()
        self._report(0, "IA", force=True)
        ffmpeg = tools.find_tool("ffmpeg")
        work = tempfile.mkdtemp(prefix="upv_img_", dir=settings.temp_dir or None)
        try:
            src = os.path.join(work, "source.png")
            ai = os.path.join(work, "ia.png")
            proc = tools.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                              "-i", info.path, "-frames:v", "1", "-vf", plan.decode_filter, src],
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            if proc.returncode != 0 or not os.path.isfile(src):
                raise UpscaleError("Lecture de l'image impossible :\n"
                                   + proc.stderr.decode("utf-8", "replace")[-800:])
            log = os.path.join(work, "realesrgan.log")
            self.on_log("Passage dans l'IA…")
            with open(log, "wb") as lf:
                p = self._register(tools.popen(build_ai_cmd(settings, plan, src, ai), stdout=lf,
                                               stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL))
                p.wait()
            if self._user_cancel:
                raise Cancelled()
            if not os.path.isfile(ai):
                raise UpscaleError(self._ai_error_message(log))
            self._report(1, "Enregistrement", force=True)
            ext = os.path.splitext(output)[1].lower().lstrip(".")
            vf = f"scale={plan.out_w}:{plan.out_h}:flags=lanczos"
            if ext == "jpg":
                codec = ["-vf", vf + ",format=yuvj444p", "-c:v", "mjpeg", "-q:v", "2"]
            elif ext == "webp":
                codec = ["-vf", vf, "-c:v", "libwebp", "-quality", "95", "-compression_level", "4"]
            else:
                codec = ["-vf", vf + (",format=rgba" if info.has_alpha else ",format=rgb24"),
                         "-c:v", "png"]
            tmp_out = os.path.join(work, "sortie." + ext)
            proc = tools.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                              "-i", ai, "-frames:v", "1", *codec, "-update", "1", tmp_out],
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            if proc.returncode != 0 or not os.path.isfile(tmp_out):
                raise UpscaleError("Enregistrement de l'image impossible :\n"
                                   + proc.stderr.decode("utf-8", "replace")[-800:])
            shutil.move(tmp_out, output)
            self._report(1, "Terminé", force=True)
            return output
        finally:
            shutil.rmtree(work, ignore_errors=True)

    # ---- vidéos -------------------------------------------------------------------
    def _pipeline(self, info: VideoInfo, plan: Plan, settings: Settings,
                  output: str, work: str) -> None:
        self._samples = collections.deque()
        self._t_start = time.monotonic()
        self._last_report = 0.0
        self._cuts: set = set()
        self._prev_last: Optional[str] = None   # dernière image source du lot précédent
        self._src_seen = 0                      # images source déjà interpolées
        self._tail_dups = max(0, plan.interp - 1)
        if plan.interp > 1:
            tools.find_tool(RIFE_BIN)  # erreur claire si absent
            if not os.path.isdir(rife_model_dir()):
                raise UpscaleError(f"Modèle d'interpolation introuvable : {rife_model_dir()}")
            self._total = max(1, info.frames)
            self.on_log("Détection des changements de plan (pour ne pas mélanger deux scènes)…")
            self._cuts = detect_scene_cuts(info, plan,
                                           on_frame=lambda n: self._report(n, "Analyse des plans"),
                                           register=self._register)
            if self._user_cancel:
                raise Cancelled()
            self.on_log(f"{len(self._cuts)} changement(s) de plan détecté(s).")
            self._samples = collections.deque()
            self._t_start = time.monotonic()
        self._total = max(1, info.frames * plan.interp)
        self._done_ai = 0

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
            self._interp = plan.interp

            reader = threading.Thread(target=self._reader_thread,
                                      args=(dec.stdout, work, plan.batch_size, ai_q), daemon=True)
            writer = threading.Thread(target=self._writer_thread,
                                      args=(enc.stdin, enc_q), daemon=True)
            reader.start()
            writer.start()
            self._report(0, "IA" if plan.upscale else "Interpolation", force=True)
            try:
                self._ai_loop(settings, plan, info, ai_q, enc_q, ai_log)
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
            self._total = max(index * self._interp, 1)
            self._put(ai_q, None)
        except _Stopped:
            pass
        except BaseException as e:  # noqa: BLE001
            if not self._stop.is_set():
                self._fail(e)

    def _ai_loop(self, settings: Settings, plan: Plan, info: VideoInfo, ai_q: queue.Queue,
                 enc_q: queue.Queue, log_path: str) -> None:
        while True:
            item = self._get(ai_q)
            if item is None:
                self._put(enc_q, None)
                return
            in_dir, count = item
            if plan.interp > 1:
                in_dir, count = self._interpolate(settings, plan, info, in_dir, count, log_path)
            if not plan.upscale:
                self._done_ai += count
                self._report(self._done_ai, "Interpolation", force=True)
                self._put(enc_q, (in_dir, count))
                continue
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

    def _interpolate(self, settings: Settings, plan: Plan, info: VideoInfo, in_dir: str,
                     count: int, log_path: str) -> tuple[str, int]:
        """Crée les images intermédiaires d'un lot avec RIFE.

        Le lot est précédé de la dernière image du lot précédent, pour interpoler aussi la
        transition entre deux lots. Les images d'origine sont recopiées telles quelles et, aux
        changements de plan, l'image intermédiaire est remplacée par l'image précédente."""
        k = plan.interp
        names = sorted(n for n in os.listdir(in_dir) if n.endswith(".png"))
        first_global = self._src_seen               # indice global de names[0]
        inputs = [os.path.join(in_dir, n) for n in names]
        has_prev = self._prev_last is not None
        if has_prev:
            prev_copy = os.path.join(in_dir, "00000000_prev.png")
            shutil.move(self._prev_last, prev_copy)
            inputs.insert(0, prev_copy)
        n_in = len(inputs)
        g0 = first_global - (1 if has_prev else 0)   # indice global de inputs[0]
        out_dir = in_dir[:-3] + "_rin"
        os.makedirs(out_dir, exist_ok=True)
        keep = range(1, k * (n_in - 1) + 1) if has_prev else range(0, k * (n_in - 1) + 1)

        rife_out = in_dir[:-3] + "_rife"
        if n_in >= 2:
            os.makedirs(rife_out, exist_ok=True)
            self._report(self._done_ai, "Interpolation", force=True)
            with open(log_path, "wb") as lf:
                proc = self._register(tools.popen(
                    build_rife_cmd(settings, plan, info, in_dir, rife_out, k * n_in),
                    stdout=lf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL))
                while proc.poll() is None:
                    if self._stop.is_set():
                        proc.kill()
                        proc.wait()
                        raise _Stopped()
                    time.sleep(0.3)
            with self._lock:
                self._procs.remove(proc)
            if self._stop.is_set():
                raise _Stopped()
            if proc.returncode != 0 or _count_png(rife_out) < k * n_in - (k - 1):
                tail = _tail(log_path)
                raise UpscaleError("L'interpolation d'images (RIFE) a échoué."
                                   + ("\n\nMémoire vidéo insuffisante." if "memory" in tail.lower() else "")
                                   + f"\n\nJournal :\n{tail[-1000:]}")

        out_index = 0
        for i in keep:
            sx, rem = divmod(i, k)
            dst = os.path.join(out_dir, f"{out_index:08d}.png")
            out_index += 1
            if rem == 0:                       # image d'origine : copie exacte
                shutil.copyfile(inputs[sx], dst)
            elif (g0 + sx + 1) in self._cuts:  # changement de plan : pas de fondu
                shutil.copyfile(inputs[sx], dst)
            else:
                os.replace(os.path.join(rife_out, f"{i + 1:08d}.png"), dst)
        # garde la dernière image source pour le lot suivant
        self._prev_last = os.path.join(os.path.dirname(in_dir), "prev_last.png")
        shutil.move(inputs[-1], self._prev_last)
        self._src_seen += len(names)
        shutil.rmtree(in_dir, ignore_errors=True)
        shutil.rmtree(rife_out, ignore_errors=True)
        return out_dir, out_index

    def _writer_thread(self, stdin, enc_q: queue.Queue) -> None:
        try:
            last = b""
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
                        last = f.read()
                    stdin.write(last)
                    os.remove(p)
                    self._encoded += 1
                shutil.rmtree(out_dir, ignore_errors=True)
            # interpolation : la dernière image est répétée pour garder la durée exacte
            for _ in range(getattr(self, "_tail_dups", 0) if last else 0):
                stdin.write(last)
                self._encoded += 1
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
