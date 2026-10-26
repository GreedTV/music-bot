"""Draws the /profile card as a picture, with an optional background image behind the stats."""
import io
import os
import re
import sys
from dataclasses import dataclass, field

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

W, H = 1100, 680
MARGIN = 48
MAX_BACKGROUND_BYTES = 10 * 1024 * 1024
MAX_BACKGROUND_PIXELS = 40_000_000

_WIN = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
# For each role, the first font file that exists is used (Windows first, then common Linux fonts).
_FONT_FILES = {
    "regular": [os.path.join(_WIN, "segoeui.ttf"), "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"],
    "semibold": [os.path.join(_WIN, "seguisb.ttf"), "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
    "bold": [os.path.join(_WIN, "segoeuib.ttf"), "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
    "emoji": [os.path.join(_WIN, "seguiemj.ttf"), "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf"],
    "symbol": [os.path.join(_WIN, "seguisym.ttf"), "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"],
    "cjk": [os.path.join(_WIN, "msyh.ttc"), "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"],
    "korean": [os.path.join(_WIN, "malgun.ttf"), "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"],
}
_font_cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def _font(role: str, size: int) -> ImageFont.FreeTypeFont:
    key = (role, size)
    if key not in _font_cache:
        path = next((p for p in _FONT_FILES[role] if os.path.exists(p)), None)
        try:
            _font_cache[key] = ImageFont.truetype(path, size) if path else ImageFont.load_default(size)
        except OSError:  # e.g. a bitmap-only emoji font that can't be scaled
            _font_cache[key] = ImageFont.load_default(size)
    return _font_cache[key]


def _script(ch: str) -> str | None:
    """Which fallback font a character needs, or None for the normal font."""
    cp = ord(ch)
    if cp in (0x200D, 0xFE0F) or 0x1F000 <= cp <= 0x1FAFF or 0x2600 <= cp <= 0x27BF or 0x2B00 <= cp <= 0x2BFF:
        return "emoji"
    if 0xAC00 <= cp <= 0xD7AF or 0x1100 <= cp <= 0x11FF or 0x3130 <= cp <= 0x318F:
        return "korean"
    if (0x3000 <= cp <= 0x30FF or 0x3400 <= cp <= 0x4DBF or 0x4E00 <= cp <= 0x9FFF
            or 0xF900 <= cp <= 0xFAFF or 0xFF00 <= cp <= 0xFFEF):
        return "cjk"
    if 0x2190 <= cp <= 0x23FF or 0x25A0 <= cp <= 0x25FF:
        return "symbol"
    return None


def _runs(text: str, role: str) -> list[tuple[str, str]]:
    """Split text into (piece, font role) runs so emoji and CJK characters use fonts that have them."""
    runs: list[tuple[str, str]] = []
    for ch in text:
        r = _script(ch) or role
        if runs and runs[-1][1] == r:
            runs[-1] = (runs[-1][0] + ch, r)
        else:
            runs.append((ch, r))
    return runs


def text_width(text: str, role: str, size: int) -> float:
    return sum(_font(r, size).getlength(piece) for piece, r in _runs(text, role))


def fit(text: str, role: str, size: int, max_width: float) -> str:
    """Shorten text with … so it fits in max_width."""
    if text_width(text, role, size) <= max_width:
        return text
    while text and text_width(text + "…", role, size) > max_width:
        text = text[:-1]
    return text.rstrip() + "…"


def draw_text(draw: ImageDraw.ImageDraw, x: float, baseline: float, text: str, role: str, size: int,
              fill, max_width: float | None = None) -> float:
    """Draw text on a baseline, switching fonts for emoji/CJK. Returns the x where the text ends."""
    if max_width is not None:
        text = fit(text, role, size, max_width)
    for piece, r in _runs(text, role):
        font = _font(r, size)
        draw.text((x, baseline), piece, font=font, fill=fill, anchor="ls", embedded_color=(r == "emoji"))
        x += font.getlength(piece)
    return x


def wrap(text: str, role: str, size: int, max_width: float, max_lines: int) -> list[str]:
    words, lines, line = text.split(), [], ""
    for word in words:
        candidate = f"{line} {word}".strip()
        if text_width(candidate, role, size) <= max_width:
            line = candidate
            continue
        if line:
            lines.append(line)
        line = word
        if len(lines) == max_lines:
            break
    if line and len(lines) < max_lines:
        lines.append(line)
    if len(lines) == max_lines and " ".join(lines) != " ".join(words):
        lines[-1] = fit(lines[-1] + " …", role, size, max_width)
    return [fit(l, role, size, max_width) for l in lines]


@dataclass
class CardData:
    name: str
    accent: int
    bio: str | None = None
    favorite: str | None = None
    avatar: bytes | None = None
    background: bytes | None = None  # already-validated image bytes
    listening: str = "0m"
    songs_heard: int = 0
    songs_requested: int = 0
    streak: int = 0
    best_streak: int = 0
    badges: list[str] = field(default_factory=list)
    top_songs: list[tuple[str, int]] = field(default_factory=list)
    top_artists: list[tuple[str, int]] = field(default_factory=list)
    listened_with: list[tuple[str, str]] = field(default_factory=list)
    top_servers: list[str] = field(default_factory=list)


def _rgb(color: int) -> tuple[int, int, int]:
    return (color >> 16) & 255, (color >> 8) & 255, color & 255


def _lighten(rgb, amount: float):
    return tuple(int(c + (255 - c) * amount) for c in rgb)


def _background(data: CardData) -> Image.Image:
    if data.background:
        try:
            img = Image.open(io.BytesIO(data.background))
            img = ImageOps.exif_transpose(img).convert("RGB")
            img = ImageOps.fit(img, (W, H), Image.Resampling.LANCZOS)
            # Darken so white text stays readable on any picture.
            return Image.blend(img, Image.new("RGB", (W, H), (10, 10, 14)), 0.45)
        except Exception:
            pass  # a broken image falls back to the color background
    accent = _rgb(data.accent)
    dark = (18, 19, 24)
    img = Image.new("RGB", (W, H), dark)
    # two soft glows of the accent color: a big one top-left and a smaller one bottom-right
    for cx, cy, radius, strength in ((0.15, 0.1, 1.3, 0.6), (0.95, 1.0, 0.8, 0.35)):
        size = int(W * radius)
        mask = ImageOps.invert(Image.radial_gradient("L")).resize((size, size)).point(lambda v, s=strength: int(v * s))
        img.paste(Image.new("RGB", (size, size), accent), (int(W * cx - size / 2), int(H * cy - size / 2)), mask)
    return img.filter(ImageFilter.GaussianBlur(30))


def _panel(img: Image.Image, box, radius=22, alpha=150):
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(overlay).rounded_rectangle(box, radius, fill=(12, 12, 16, alpha))
    img.alpha_composite(overlay)


def _avatar(data: CardData, size: int) -> Image.Image | None:
    if not data.avatar:
        return None
    try:
        av = ImageOps.fit(Image.open(io.BytesIO(data.avatar)).convert("RGBA"), (size, size), Image.Resampling.LANCZOS)
    except Exception:
        return None
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    av.putalpha(mask)
    return av


def render_profile(data: CardData) -> bytes:
    """Returns a JPEG of the profile card."""
    accent = _rgb(data.accent)
    soft = _lighten(accent, 0.55)
    white, grey = (255, 255, 255), (200, 202, 210)
    img = _background(data).convert("RGBA")
    draw = ImageDraw.Draw(img)

    # header: avatar, name, bio, favorite song
    av_size = 160
    draw.ellipse((MARGIN - 6, 40 - 6, MARGIN + av_size + 6, 40 + av_size + 6), fill=accent)
    av = _avatar(data, av_size)
    if av:
        img.alpha_composite(av, (MARGIN, 40))
    else:
        draw.ellipse((MARGIN, 40, MARGIN + av_size, 40 + av_size), fill=(40, 42, 50))
    tx = MARGIN + av_size + 32
    text_w = W - tx - MARGIN
    draw_text(draw, tx, 98, data.name, "bold", 50, white, text_w)
    y = 140
    if data.bio:
        for line in wrap(data.bio, "regular", 26, text_w, 2):
            draw_text(draw, tx, y, line, "regular", 26, (235, 236, 240))
            y += 34
    if data.favorite:
        end = draw_text(draw, tx, y + 4, "🎵 Favorite: ", "semibold", 23, soft)
        draw_text(draw, end, y + 4, data.favorite, "regular", 23, white, W - MARGIN - end)

    # badges
    bx, by = MARGIN, 236
    for badge in data.badges:
        w = text_width(badge, "semibold", 20) + 28
        if bx + w > W - MARGIN:
            break
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ImageDraw.Draw(overlay).rounded_rectangle((bx, by, bx + w, by + 38), 19, fill=(*accent, 210))
        img.alpha_composite(overlay)
        draw_text(draw, bx + 14, by + 27, badge, "semibold", 20, white)
        bx += w + 10

    # stat boxes
    stats = [("Listening time", data.listening), ("Songs heard", str(data.songs_heard)),
             ("Songs requested", str(data.songs_requested)),
             (f"Streak · best {data.best_streak}", f"{data.streak} day{'s' if data.streak != 1 else ''}")]
    gap = 16
    box_w = (W - 2 * MARGIN - gap * (len(stats) - 1)) / len(stats)
    sy = 292
    for i, (label, value) in enumerate(stats):
        x0 = MARGIN + i * (box_w + gap)
        _panel(img, (x0, sy, x0 + box_w, sy + 104))
        draw_text(draw, x0 + 20, sy + 36, label, "regular", 20, grey, box_w - 40)
        draw_text(draw, x0 + 20, sy + 86, value, "bold", 40, white, box_w - 40)

    # lists
    ly = 412
    _panel(img, (MARGIN, ly, W - MARGIN, ly + 212))
    columns = [
        (MARGIN + 24, 450, "Top songs", [f"{i}. {t}  ·  {n}×" for i, (t, n) in enumerate(data.top_songs, 1)]),
        (MARGIN + 500, 230, "Top artists", [f"{i}. {a}" for i, (a, _) in enumerate(data.top_artists, 1)]),
        (MARGIN + 754, 250, "Listened with", [f"{n}  ·  {t}" for n, t in data.listened_with]),
    ]
    for x, width, title, items in columns:
        draw_text(draw, x, ly + 38, title, "semibold", 22, soft)
        if not items:
            draw_text(draw, x, ly + 76, "Nothing yet", "regular", 21, grey)
        for row, item in enumerate(items[:5]):
            draw_text(draw, x, ly + 76 + row * 32, item, "regular", 21, white, width)

    # footer, on a soft dark fade so it's readable over any background
    fade = Image.linear_gradient("L").resize((W, 70)).point(lambda v: int(v * 0.75))
    img.paste((8, 8, 12, 255), (0, H - 70), fade)
    draw = ImageDraw.Draw(img)
    if data.top_servers:
        draw_text(draw, MARGIN, H - 22, "Top servers: " + "  ·  ".join(data.top_servers), "regular", 19, grey,
                  W - 2 * MARGIN - 140)
    brand = "Musica"
    draw_text(draw, W - MARGIN - text_width(brand, "bold", 20), H - 22, brand, "bold", 20, soft)

    out = io.BytesIO()
    img.convert("RGB").save(out, "JPEG", quality=90)
    return out.getvalue()


def prepare_background(raw: bytes) -> bytes | None:
    """Check an uploaded picture and shrink it to the card's size. Returns a small JPEG, or None if it's not
    a usable image. Only this copy is kept, so a background can't change after it's set."""
    try:
        img = Image.open(io.BytesIO(raw))
        if img.width * img.height > MAX_BACKGROUND_PIXELS:
            return None
        img.verify()  # catches truncated or fake files
        img = Image.open(io.BytesIO(raw))  # verify() leaves the image unusable, so open it again
        img.seek(0)  # animated GIF/WebP: use the first frame
        img = ImageOps.fit(ImageOps.exif_transpose(img).convert("RGB"), (W, H), Image.Resampling.LANCZOS)
    except Exception:
        return None
    out = io.BytesIO()
    img.save(out, "JPEG", quality=85)
    return out.getvalue()


if __name__ == "__main__" and len(sys.argv) > 1:  # python card.py out.jpg  -> renders a sample card
    sample = CardData("Bat-Erdene 🎧", 0xEB459E, bio="lofi all day 🌙 · Сайн байна уу", favorite="Get Lucky - Daft Punk",
                      listening="12h 30m", songs_heard=150, songs_requested=60, streak=8, best_streak=12,
                      badges=["🎶 10h listener", "🔥 7-day streak", "💯 100 songs"],
                      top_songs=[("Daft Punk - Get Lucky", 9), ("The Weeknd - Blinding Lights", 7)],
                      top_artists=[("Daft Punk", 9), ("The Weeknd", 7)], listened_with=[("Alice", "3h 2m")],
                      top_servers=["Chill Server · 12h"])
    with open(sys.argv[1], "wb") as f:
        f.write(render_profile(sample))
