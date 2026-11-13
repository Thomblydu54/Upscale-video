"""Finitions automatiques : choix de la finition la mieux adaptée à CHAQUE image.

Principe
  1. Analyse rapide de toutes les images de la vidéo (miniatures 64x36) : luminosité, contraste,
     couleurs, teinte chaude / froide, hautes lumières, détails.
  2. Pour chaque image, chaque finition reçoit une note selon ce qu'elle apporte à CETTE image
     (ex. Doré pour un coucher de soleil, Glacé pour la neige ou un ciel bleu, Cinéma pour une
     scène sombre ou terne, Glow pour des lumières dans la nuit, Netteté seule pour le noir et
     blanc ou une image déjà très colorée…).
  3. Stabilisation pour éviter tout clignotement : notes lissées sur ±0,5 s à l'intérieur d'un
     même plan, finition gardée au moins 1 s, passage en fondu (0,6 s) d'une finition à l'autre ;
     changement net, sans fondu, à chaque changement de plan.

Le résultat (« runs » : début, finition, changement de plan ?) est enregistré dans l'état du
traitement : la pause / reprise est donc exacte. Les réglages de chaque image sont appliqués par
FFmpeg (sendcmd) pendant l'encodage, sans toucher aux autres modes de finitions."""
from __future__ import annotations

import bisect
from array import array
from typing import Callable, Optional

AW, AH = 64, 36                       # miniature d'analyse
FRAME_BYTES = AW * AH * 3
CUT_DIFF = 0.11                       # écart moyen des couleurs => changement de plan
KEYS = ("eclatant", "glow", "cinema", "dore", "glace", "net")
PARAM_KEYS = ("finish_sharp", "finish_color", "finish_contrast", "finish_glow", "finish_warmth")

SMOOTH_S = 0.5                        # lissage des notes (± secondes, dans un même plan)
MIN_RUN_S = 1.0                       # durée minimale d'une finition dans un plan
RAMP_S = 0.6                          # durée du fondu entre deux finitions d'un même plan


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if x < lo else hi if x > hi else x


def features(buf: bytes) -> tuple:
    """Mesures d'une miniature RGB 64x36 (valeurs 0..1) + luminosités (pour les plans)."""
    r, g, b = buf[0::3], buf[1::3], buf[2::3]
    n = len(r)
    lum = [(0.2126 * x + 0.7152 * y + 0.0722 * z) / 255.0 for x, y, z in zip(r, g, b)]
    srt = sorted(lum)
    mean_l = sum(lum) / n
    lo, hi = srt[n // 10], srt[(n * 9) // 10]
    chroma = (sum(map(max, r, g, b)) - sum(map(min, r, g, b))) / (255.0 * n)
    warm = (sum(r) - sum(b)) / (255.0 * n)
    hl = sum(1 for v in map(max, r, g, b) if v > 217) / n   # points très lumineux (néons…)
    dk = sum(1 for v in lum if v < 0.18) / n
    gx = sum(abs(lum[i + 1] - lum[i]) for i in range(n - 1) if (i + 1) % AW)
    gy = sum(abs(lum[i + AW] - lum[i]) for i in range(n - AW))
    tex = (gx + gy) / (2 * n)
    return (mean_l, lo, hi, chroma, warm, hl, dk, tex), lum


def scores(f: tuple) -> tuple:
    """Note de chaque finition (ordre KEYS) pour une image."""
    mean_l, lo, hi, chroma, warm, hl, dk, tex = f
    spread = hi - lo
    colour = _clamp((chroma - 0.02) / 0.04)           # 0 = noir et blanc
    mono = 1.0 - colour
    punchy = _clamp((chroma - 0.33) / 0.12) * _clamp((spread - 0.55) / 0.2)
    # Doré : scène déjà chaude (coucher de soleil, intérieur, lumière dorée)
    dore = _clamp((warm - 0.06) / 0.12) * (0.55 + 0.45 * _clamp(chroma / 0.25)) * colour
    # Glacé : dominante froide (neige, ciel, mer, nuit bleue)
    glace = _clamp((-warm - 0.012) / 0.09) * (0.8 + 0.2 * _clamp(mean_l / 0.5)) * colour
    # Glow : lumières vives sur fond sombre (néons, lampes, ville la nuit, feu)
    lights = _clamp(hl / 0.07) * _clamp((0.58 - mean_l) / 0.25) * _clamp(dk / 0.25 + 0.3)
    # … ou image très colorée en aplats (dessin animé lumineux)
    flat = _clamp((chroma - 0.28) / 0.12) * _clamp((0.07 - tex) / 0.04) * _clamp((mean_l - 0.35) / 0.2)
    glow = max(lights, 0.8 * flat) * colour
    # Cinéma : scène sombre / feutrée, ou terne (peu de contraste)
    dark = _clamp((0.4 - mean_l) / 0.2) * (1.0 - 0.6 * _clamp(hl / 0.07))
    dull = _clamp((0.5 - spread) / 0.25)
    cinema = max(0.8 * dark, 0.7 * dull) * (0.4 + 0.6 * colour)
    # Éclatant : scène de jour aux couleurs normales -> plus vive (choix « par défaut »)
    day = _clamp(1.0 - abs(mean_l - 0.55) / 0.3)
    eclatant = (0.3 + 0.35 * day * _clamp((0.34 - chroma) / 0.15)) * colour * (1.0 - punchy)
    # Netteté seule : noir et blanc (pas de teinte ajoutée), ou image déjà très vive
    net = max(mono, 0.75 * punchy, 0.12)
    return (eclatant, glow, cinema, dore, glace, net)


def pick(f: tuple) -> str:
    s = scores(f)
    return KEYS[max(range(len(KEYS)), key=lambda i: s[i])]


def describe(f: tuple) -> str:
    mean_l, lo, hi, chroma, warm, hl, dk, tex = f
    return (f"lumière {mean_l:.2f}, contraste {hi - lo:.2f}, couleurs {chroma:.2f}, "
            f"teinte {'chaude' if warm > 0.03 else 'froide' if warm < -0.01 else 'neutre'} "
            f"({warm:+.2f}), hautes lumières {hl * 100:.0f} %")


class Analysis:
    """Accumule les notes image par image pendant la lecture de la vidéo."""

    def __init__(self):
        self.sc = [array("f") for _ in KEYS]
        self.cuts: list[int] = []
        self._prev: Optional[list] = None
        self.n = 0

    def add(self, buf: bytes) -> None:
        f, _ = features(buf)
        for arr, v in zip(self.sc, scores(f)):
            arr.append(v)
        if self._prev is not None:  # écart moyen des couleurs (R, V, B) avec l'image précédente
            diff = sum(abs(a - b) for a, b in zip(buf, self._prev)) / (255.0 * len(buf))
            if diff > CUT_DIFF:
                self.cuts.append(self.n)
        self._prev = buf
        self.n += 1

    def runs(self, fps: float) -> list:
        """Finition choisie par image, stabilisée -> [[début, clé, changement de plan], …]."""
        n = self.n
        if n == 0:
            return []
        half = max(1, int(round(SMOOTH_S * fps)))
        min_run = max(1, int(round(MIN_RUN_S * fps)))
        bounds = [0] + [c for c in self.cuts if 0 < c < n] + [n]
        labels = [0] * n
        for s0, s1 in zip(bounds, bounds[1:]):
            pre = []
            for arr in self.sc:  # sommes cumulées : moyenne glissante rapide
                acc, p = 0.0, [0.0]
                for i in range(s0, s1):
                    acc += arr[i]
                    p.append(acc)
                pre.append(p)
            for i in range(s0, s1):
                a, b = max(s0, i - half) - s0, min(s1, i + half + 1) - s0
                best, bv = 0, -1.0
                for k, p in enumerate(pre):
                    v = (p[b] - p[a]) / (b - a)
                    if v > bv + 1e-9:
                        best, bv = k, v
                labels[i] = best
            # finitions trop brèves dans ce plan : absorbées par la voisine la plus longue
            while True:
                segs, i = [], s0
                while i < s1:
                    j = i
                    while j < s1 and labels[j] == labels[i]:
                        j += 1
                    segs.append([i, j, labels[i]])
                    i = j
                if len(segs) <= 1:
                    break
                short = min(range(len(segs)), key=lambda t: segs[t][1] - segs[t][0])
                a, b, _ = segs[short]
                if b - a >= min_run:
                    break
                left = segs[short - 1] if short > 0 else None
                right = segs[short + 1] if short + 1 < len(segs) else None
                nb = max((x for x in (left, right) if x), key=lambda x: x[1] - x[0])
                for t in range(a, b):
                    labels[t] = nb[2]
        cutset = set(bounds[1:-1])
        out = []
        for i in range(n):
            if i == 0 or labels[i] != labels[i - 1] or i in cutset:
                if out and out[-1][1] == KEYS[labels[i]] and i not in cutset:
                    continue
                out.append([i, KEYS[labels[i]], 1 if (i == 0 or i in cutset) else 0])
        return out


def stats(runs: list, n: int) -> dict:
    """Part de chaque finition (en images)."""
    res: dict = {}
    for t, run in enumerate(runs):
        end = runs[t + 1][0] if t + 1 < len(runs) else n
        res[run[1]] = res.get(run[1], 0) + max(0, end - run[0])
    return res


class Curve:
    """Réglages (netteté, couleurs, contraste, halo, teinte ; 0..1 / -1..1) de chaque image
    source, avec fondu entre deux finitions d'un même plan."""

    def __init__(self, runs: list, presets: dict, fps: float, n: int):
        self.runs = runs or [[0, "eclatant", 1]]
        self.starts = [r[0] for r in self.runs]
        self.vals = [tuple(presets[r[1]][k] / 100.0 for k in PARAM_KEYS) for r in self.runs]
        self.ramp = max(2, int(round(RAMP_S * fps)))
        self.n = max(1, n)

    def at(self, i: int) -> tuple:
        i = max(0, min(self.n - 1, i))
        t = bisect.bisect_right(self.starts, i) - 1
        cur = self.vals[t]
        half = self.ramp / 2.0
        # fondu avec la finition précédente (début de cette finition, même plan)
        if t > 0 and not self.runs[t][2] and i < self.starts[t] + half:
            x = (i - (self.starts[t] - half)) / self.ramp
            return _mix(self.vals[t - 1], cur, x)
        # fondu avec la suivante (fin de cette finition, même plan)
        if t + 1 < len(self.runs) and not self.runs[t + 1][2] and i >= self.starts[t + 1] - half:
            x = (i - (self.starts[t + 1] - half)) / self.ramp
            return _mix(cur, self.vals[t + 1], x)
        return cur


def _mix(a: tuple, b: tuple, x: float) -> tuple:
    x = _clamp(x)
    x = x * x * (3 - 2 * x)  # fondu doux
    return tuple(p + (q - p) * x for p, q in zip(a, b))


# ---- application par FFmpeg ----------------------------------------------------------------

def _filter_values(v: tuple) -> dict:
    sharp, color, contrast, glow, warmth = v
    temp = 6500 - warmth * 2500 if warmth > 0 else 6500 - warmth * 5000
    mix = min(1.0, abs(warmth) * 10) * (0.35 + 0.35 * abs(warmth))  # continu autour de 0
    d = 0.075 * contrast
    return {
        "temperature": f"{temp:.0f}", "mix": f"{mix:.3f}",
        "master": f"0/0 0.25/{0.25 - d:.4f} 0.75/{0.75 + d:.4f} 1/1",
        "intensity": f"{0.9 * color:.3f}", "strength": f"{0.9 * sharp:.3f}",
        "opacity": f"{0.65 * glow:.3f}",
    }


def filter_graph(v0: tuple, height: int, cmd_file: str) -> str:
    """Chaîne FFmpeg (mêmes filtres que les finitions manuelles), réglable image par image."""
    f = _filter_values(v0)
    sigma = max(2.0, height * 0.012)
    return (f"format=gbrp,sendcmd=f={cmd_file},"
            f"colortemperature@fa=temperature={f['temperature']}:mix={f['mix']}:pl=1,"
            f"curves@fb=master='{f['master']}',"
            f"vibrance@fc=intensity={f['intensity']},"
            f"cas@fd=strength={f['strength']},"
            f"split[fin_a][fin_b];[fin_b]curves=all='0/0 0.6/0 1/1',"
            f"gblur=sigma={sigma:.1f}:steps=2[fin_g];"
            f"[fin_a][fin_g]blend@fe=all_mode=screen:all_opacity={f['opacity']}")


def command_lines(values: Callable[[int], tuple], count: int, fps: float) -> tuple:
    """Commandes sendcmd d'un morceau : (réglages de la 1re image, texte des changements)."""
    prev = _filter_values(values(0))
    first = values(0)
    lines = []
    for i in range(1, count):
        cur = _filter_values(values(i))
        if cur == prev:
            continue
        cmds = []
        if cur["temperature"] != prev["temperature"]:
            cmds.append(f"colortemperature@fa temperature {cur['temperature']}")
        if cur["mix"] != prev["mix"]:
            cmds.append(f"colortemperature@fa mix {cur['mix']}")
        if cur["master"] != prev["master"]:
            cmds.append(f"curves@fb master '{cur['master']}'")
        if cur["intensity"] != prev["intensity"]:
            cmds.append(f"vibrance@fc intensity {cur['intensity']}")
        if cur["strength"] != prev["strength"]:
            cmds.append(f"cas@fd strength {cur['strength']}")
        if cur["opacity"] != prev["opacity"]:
            cmds.append(f"blend@fe all_opacity {cur['opacity']}")
        # au milieu de deux images : déclenché exactement sur l'image i
        lines.append(f"{(i - 0.5) / fps:.6f} " + ", ".join(cmds) + ";")
        prev = cur
    if not lines:  # sendcmd refuse un fichier vide : commande neutre (même valeur)
        lines.append(f"0 cas@fd strength {_filter_values(first)['strength']};")
    return first, "\n".join(lines) + "\n"
