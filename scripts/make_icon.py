"""Génère assets/icon.ico et assets/icon.png (nécessite Pillow)."""
import os
from PIL import Image, ImageDraw, ImageFont

S = 512
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
# dégradé violet -> cyan
grad = Image.new("RGBA", (S, S))
gd = ImageDraw.Draw(grad)
for y in range(S):
    t = y / (S - 1)
    c = (int(124 + (0 - 124) * t), int(58 + (200 - 58) * t), int(237 + (255 - 237) * t), 255)
    gd.line([(0, y), (S, y)], fill=c)
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle([16, 16, S - 16, S - 16], radius=110, fill=255)
img.paste(grad, (0, 0), mask)
d = ImageDraw.Draw(img)
# petit carré (basse résolution) -> grand carré (haute résolution)
d.rounded_rectangle([86, 300, 196, 410], radius=14, outline=(255, 255, 255, 230), width=14)
d.rounded_rectangle([150, 100, 420, 370], radius=26, outline=(255, 255, 255, 255), width=18)
d.line([(200, 312), (330, 182)], fill=(255, 255, 255, 255), width=20)
d.polygon([(350, 162), (350, 252), (260, 162)], fill=(255, 255, 255, 255))
try:
    font = ImageFont.truetype("DejaVuSans-Bold.ttf", 92)
except OSError:
    font = ImageFont.load_default()
d.text((296, 440), "AI", font=font, fill=(255, 255, 255, 255), anchor="mm")

here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets")
os.makedirs(here, exist_ok=True)
img.save(os.path.join(here, "icon.png"))
img.save(os.path.join(here, "icon.ico"), sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("icône générée")

# écran de démarrage (affiché pendant le chargement de l'exe)
W, H = 520, 300
splash = Image.new("RGB", (W, H), (24, 24, 32))
sd = ImageDraw.Draw(splash)
splash.paste(img.resize((140, 140), Image.LANCZOS), (30, 80), img.resize((140, 140), Image.LANCZOS))
try:
    big = ImageFont.truetype("DejaVuSans-Bold.ttf", 30)
    small = ImageFont.truetype("DejaVuSans.ttf", 16)
except OSError:
    big = small = ImageFont.load_default()
sd.text((195, 115), "Upscale Video AI", font=big, fill=(255, 255, 255))
sd.text((197, 162), "Agrandissement vidéo par IA", font=small, fill=(170, 200, 255))
sd.text((197, 250), "Chargement…", font=small, fill=(140, 140, 160))
splash.save(os.path.join(here, "splash.png"))
print("splash généré")
