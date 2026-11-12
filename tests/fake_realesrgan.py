#!/usr/bin/env python3
"""Faux realesrgan-ncnn-vulkan (même CLI) pour tester le pipeline sans GPU.
Agrandit les images avec Pillow (bicubique)."""
import os, sys, getopt
from PIL import Image

opts, _ = getopt.getopt(sys.argv[1:], "i:o:s:t:g:m:n:f:j:xv")
o = dict(opts)
scale = int(o.get("-s", 4))
src, dst = o["-i"], o["-o"]
print("[0 Fake GPU]  queueC=2[8]  queueG=0[16]  queueT=1[2]", file=sys.stderr)
if os.environ.get("FAKE_AI_FAIL"):
    print("vkAllocateMemory failed", file=sys.stderr); sys.exit(255)
pairs = []
if os.path.isdir(src):
    for n in sorted(os.listdir(src)):
        pairs.append((os.path.join(src, n), os.path.join(dst, os.path.splitext(n)[0] + ".png")))
else:
    pairs.append((src, dst))
for a, b in pairs:
    im = Image.open(a).convert("RGB")
    im.resize((im.width * scale, im.height * scale), Image.BICUBIC).save(b, compress_level=1)
