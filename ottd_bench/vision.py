"""Turn OpenTTD screenshots into images a model can ground tile coordinates in.

Two views are produced from the game's own renderer:

* view     - the real isometric game picture around a tile, with tile coordinates overlaid
* overview - the game's top-down minimap, enlarged, with ordinary x-right/y-down axes, a grid and place names

Both are cut from whole-map screenshots, so the tile -> pixel mapping is exact and does not
depend on any window or scroll state inside the game.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from .config import OPENTTD_DIR

TILE_SIZE = 16      # world units per tile
TILE_HEIGHT = 8     # world units (= pixels at 1x) per height level

# Fraction of the game's normal zoom, and the spacing of coordinate labels in tiles. The label
# spacing is chosen so labels are always about 128 px apart on screen.
ZOOMS = {"close": (1.0, 2), "medium": (0.5, 4), "far": (0.25, 8)}

_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    str(OPENTTD_DIR / "baseset" / "OpenTTD-Sans.ttf"),
]


@lru_cache(maxsize=None)
def _font(size: int) -> ImageFont.ImageFont:
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def _label(draw: ImageDraw.ImageDraw, xy: tuple[float, float], text: str, size: int = 11,
           fill=(255, 255, 255), anchor: str = "mm") -> None:
    draw.text(xy, text, font=_font(size), fill=fill, anchor=anchor, stroke_width=2, stroke_fill=(0, 0, 0))


@dataclass
class MapGeometry:
    """Tile -> pixel mapping for OpenTTD's whole-map ("giant") screenshot.

    Mirrors SetupScreenshotViewport()/RemapCoords() in OpenTTD's screenshot.cpp and landscape.h.
    """
    size_x: int
    size_y: int
    heights: list[str]          # heights[y][x] = chr(48 + height of the tile's north corner)
    freeform_edges: bool = True

    def corner(self, x: int, y: int) -> int:
        if 0 <= x < self.size_x and 0 <= y < self.size_y:
            return ord(self.heights[y][x]) - 48
        return 0

    def centre_height(self, x: int, y: int) -> float:
        return (self.corner(x, y) + self.corner(x + 1, y) + self.corner(x, y + 1) + self.corner(x + 1, y + 1)) / 4

    @property
    def _origin(self) -> tuple[int, int]:
        n = 1 if self.freeform_edges else 0
        left = (n - (self.size_x - 1)) * TILE_SIZE * 2
        top = 2 * n * TILE_SIZE - (self.corner(n, n) * TILE_HEIGHT + 150)
        return left, top

    def world_px(self, x: float, y: float) -> tuple[float, float]:
        """Pixel of the centre of tile (x, y) in the giant screenshot."""
        left, top = self._origin
        wx = x * TILE_SIZE + TILE_SIZE / 2
        wy = y * TILE_SIZE + TILE_SIZE / 2
        z = self.centre_height(int(x), int(y)) * TILE_HEIGHT
        return (wy - wx) * 2 - left, (wx + wy - z) - top

    def expected_size(self) -> tuple[int, int]:
        n = 1 if self.freeform_edges else 0
        left, top = self._origin
        width = (self.size_y - 1 - n) * TILE_SIZE * 2 - left + 1
        south = self.corner(self.size_x - 1, self.size_y - 1) * TILE_HEIGHT
        height = (self.size_x - 1 + self.size_y - 1) * TILE_SIZE - south - top + 1
        return width, height


def render_view(world: Image.Image, geo: MapGeometry, cx: int, cy: int, zoom: str = "medium",
                out_size: tuple[int, int] = (1024, 768), marks: list[tuple[int, int, str]] | None = None) -> Image.Image:
    """Isometric game view centred on tile (cx, cy) with a tile-coordinate overlay.

    `marks` are (x, y, text) tags for things the player owns, drawn in cyan.
    """
    scale, step = ZOOMS[zoom]
    out_w, out_h = out_size
    px, py = geo.world_px(cx, cy)
    src_w, src_h = out_w / scale, out_h / scale
    left, top = px - src_w / 2, py - src_h / 2

    box = (round(left), round(top), round(left + src_w), round(top + src_h))
    crop = world.crop(box).convert("RGB")   # areas outside the screenshot come back black
    if scale != 1.0:
        crop = crop.resize(out_size, Image.LANCZOS)
    draw = ImageDraw.Draw(crop)

    def to_out(x: int, y: int) -> tuple[float, float]:
        wx, wy = geo.world_px(x, y)
        return (wx - left) * scale, (wy - top) * scale

    half_w, half_h = 32 * scale, 16 * scale   # half extents of one tile's diamond
    for y in range(0, geo.size_y, step):
        for x in range(0, geo.size_x, step):
            ox, oy = to_out(x, y)
            if not (-20 <= ox <= out_w + 20 and 14 <= oy <= out_h + 10):
                continue
            if zoom == "close":
                draw.polygon([(ox, oy - half_h), (ox + half_w, oy), (ox, oy + half_h), (ox - half_w, oy)],
                             outline=(255, 255, 255))
            else:
                draw.ellipse((ox - 2, oy - 2, ox + 2, oy + 2), fill=(255, 255, 255), outline=(0, 0, 0))
            _label(draw, (ox, oy if zoom == "close" else oy - 9), f"{x},{y}")

    for mx, my, text in marks or []:
        ox, oy = to_out(mx, my)
        if 0 <= ox <= out_w and 22 <= oy <= out_h:
            draw.polygon([(ox, oy - half_h), (ox + half_w, oy), (ox, oy + half_h), (ox - half_w, oy)], outline=(0, 255, 255), width=2)
            _label(draw, (ox, oy + half_h + 7), text, fill=(0, 255, 255))

    # The requested centre tile, marked distinctly.
    ox, oy = to_out(cx, cy)
    draw.polygon([(ox, oy - half_h), (ox + half_w, oy), (ox, oy + half_h), (ox - half_w, oy)], outline=(255, 235, 0), width=2)
    if cx % step or cy % step:
        _label(draw, (ox, oy - half_h - 8), f"{cx},{cy}", fill=(255, 235, 0))

    draw.rectangle((0, 0, out_w, 20), fill=(0, 0, 0))
    draw.text((6, 3), f"Game view centred on tile {cx},{cy} (yellow)  |  zoom: {zoom}  |  labels are tile x,y"
                      "  |  x grows down-left, y grows down-right  |  cyan = yours", font=_font(12), fill=(255, 255, 255))
    return crop


# Colours of OpenTTD's owner minimap (OpenGFX palette), for the legend only.
_LEGEND = [((60, 89, 166), "water"), ((67, 110, 20), "land"), ((128, 0, 0), "town buildings and town roads"),
           ((104, 104, 104), "industry")]


def render_overview(minimap: Image.Image, geo: MapGeometry, towns: list[dict], industries: list[dict],
                    stations: list[dict], target: int = 768) -> Image.Image:
    """Top-down map of the whole world with a tile grid, axis labels and place names."""
    size = max(geo.size_x, geo.size_y)
    s = max(2, target // size)                         # pixels per tile
    grid = {64: 8, 128: 16, 256: 32, 512: 64}.get(size, 32)
    margin, footer = 38, 46
    map_w, map_h = geo.size_x * s, geo.size_y * s
    img = Image.new("RGB", (margin + map_w + 8, margin + map_h + footer), (20, 20, 20))
    # OpenTTD writes the minimap with x running right to left (column 0 is tile x = size_x - 1).
    # It is mirrored here so both axes read the ordinary way: models misread a reversed x axis.
    upright = ImageOps.mirror(minimap.convert("RGB"))
    img.paste(upright.resize((map_w, map_h), Image.NEAREST), (margin, margin))
    draw = ImageDraw.Draw(img)

    def to_px(x: float, y: float) -> tuple[float, float]:
        return margin + (x + 0.5) * s, margin + (y + 0.5) * s

    for x in range(0, geo.size_x, grid):
        gx = margin + x * s
        draw.line((gx, margin, gx, margin + map_h), fill=(255, 255, 255, 90), width=1)
        _label(draw, (gx, margin - 10), str(x), 12)
    for y in range(0, geo.size_y, grid):
        gy = margin + y * s
        draw.line((margin, gy, margin + map_w, gy), fill=(255, 255, 255, 90), width=1)
        _label(draw, (margin - 6, gy), str(y), 12, anchor="rm")
    draw.text((margin, 2), "x grows to the right", font=_font(11), fill=(200, 200, 200))
    draw.text((2, margin + map_h + 2), "y grows downward", font=_font(11), fill=(200, 200, 200))

    for ind in industries:
        ix, iy = to_px(ind["x"], ind["y"])
        _label(draw, (ix, iy), ind["type"], 10, fill=(255, 220, 90))
    for town in towns:
        tx, ty = to_px(town["x"], town["y"])
        _label(draw, (tx, ty - 11), town["name"], 13)
    for st in stations:
        sx, sy = to_px(st["x"], st["y"])
        draw.rectangle((sx - 4, sy - 4, sx + 4, sy + 4), outline=(0, 255, 255), width=2)
        _label(draw, (sx + 7, sy), f"#{st['id']}", 10, fill=(0, 255, 255), anchor="lm")

    lx, ly = margin, margin + map_h + 22
    for colour, name in _LEGEND:
        draw.rectangle((lx, ly, lx + 12, ly + 12), fill=colour, outline=(255, 255, 255))
        draw.text((lx + 17, ly - 1), name, font=_font(11), fill=(230, 230, 230))
        lx += 30 + int(draw.textlength(name, font=_font(11)))
    draw.rectangle((lx, ly, lx + 12, ly + 12), outline=(0, 255, 255), width=2)
    draw.text((lx + 17, ly - 1), "your stations (#id)", font=_font(11), fill=(230, 230, 230))
    lx += 30 + int(draw.textlength("your stations (#id)", font=_font(11)))
    draw.rectangle((lx, ly, lx + 12, ly + 12), fill=(252, 244, 60), outline=(255, 255, 255))
    draw.text((lx + 17, ly - 1), "your roads", font=_font(11), fill=(230, 230, 230))
    return img


def save_for_model(img: Image.Image, path: Path, max_side: int = 1024) -> Path:
    """Write the image the way it is sent to the model (and kept as a run artifact)."""
    if max(img.size) > max_side:
        img = img.copy()
        img.thumbnail((max_side, max_side), Image.LANCZOS)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "JPEG", quality=88)
    return path
