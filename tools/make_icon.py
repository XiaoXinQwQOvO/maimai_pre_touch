from PIL import Image, ImageDraw
S = 192
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle([0, 0, S - 1, S - 1], radius=42, fill=(10, 16, 26, 255))
c = S / 2
for i, r in enumerate([78, 60, 42, 24]):
    d.ellipse([c - r, c - r, c + r, c + r], outline=(79, 209, 255, 90 + i * 40), width=4)
for k in range(8):
    import math
    a = k * math.pi / 4
    d.line([c + 24 * math.sin(a), c - 24 * math.cos(a), c + 78 * math.sin(a), c - 78 * math.cos(a)],
           fill=(79, 209, 255, 60), width=2)
d.ellipse([c - 16, c - 16, c + 16, c + 16], fill=(79, 209, 255, 230))
d.ellipse([c - 7, c - 7, c + 7, c + 7], fill=(10, 16, 26, 255))
img.save(r"android\app\res\drawable\ic_launcher.png")
print("icon written")
