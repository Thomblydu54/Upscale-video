#!/usr/bin/env python3
"""Faux rife-ncnn-vulkan (même CLI, même numérotation que l'original) pour tester sans GPU.
Les images intermédiaires sont un simple fondu (Pillow)."""
import getopt, math, os, sys
from PIL import Image
import time
DELAY = float(os.environ.get('FAKE_DELAY', '0'))

opts, _ = getopt.getopt(sys.argv[1:], "0:1:i:o:n:s:m:g:j:f:xzuvh")
o = dict(opts)
src, dst = o["-i"], o["-o"]
if "rife" not in os.path.basename(o.get("-m", "rife-v2.3").rstrip("/\\")):
    print("unknown model dir type", file=sys.stderr); sys.exit(255)
files = sorted(os.listdir(src))
count = len(files)
numframe = int(o.get("-n", 0)) or count * 2
scale = count / numframe
cache = {}
def load(n):
    if n not in cache:
        cache[n] = Image.open(os.path.join(src, n)).convert("RGB")
    return cache[n]
for i in range(numframe):
    fx = i * scale
    sx = int(math.floor(fx)); fx -= sx
    if sx >= count - 1:
        sx, fx = count - 2, 1.0
    time.sleep(DELAY)
    a, b = load(files[sx]), load(files[sx + 1])
    im = a if fx == 0 else b if fx == 1 else Image.blend(a, b, fx)
    im.save(os.path.join(dst, "%08d.png" % (i + 1)), compress_level=1)
    print(f"{files[sx]} {files[sx+1]} {fx:f} -> {i+1:08d}.png done", file=sys.stderr)
