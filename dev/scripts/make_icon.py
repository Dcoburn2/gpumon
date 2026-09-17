"""Generate the gpumon application icon as a multi-resolution .ico.

No image library is available (and none should become a dependency), so the icon
is drawn analytically and the PNG/ICO containers are written by hand:

  * a tiny software rasteriser with coverage-based anti-aliasing (each pixel is
    sampled on a 3x3 sub-grid and the coverage drives alpha)
  * a minimal PNG encoder using `zlib` from the standard library
  * the ICO container, which stores PNG-compressed frames at 256x256 and
    uncompressed BGRA bitmaps below that (the format's traditional layout, and
    what Windows Explorer and the taskbar expect)

Design: a rounded dark tile holding a GPU package - a chip body with contact
pins on the left and right - with a monitor trace rising across it in the accent
blue, plus a warm peak where the trace tops out. It reads at 16 px because the
silhouette is two strong shapes and the trace, and it stays legible at 256 px.

Run:  python make_icon.py        (writes gpumon.ico and gpumon.png)
"""
from __future__ import annotations

import math
import os
import struct
import zlib
from typing import Sequence

# Palette. The monster is the application's own mascot - horns, two big eyes,
# a mouth full of a graphics card - drawn in the violet of the EVA-01 theme so
# the icon and the app look like the same thing.
TILE_TOP = (0x1A, 0x14, 0x33)
TILE_BOTTOM = (0x0C, 0x0A, 0x18)
HORN = (0xF2, 0xC9, 0x9B)
HORN_LIGHT = (0xFA, 0xE0, 0xBE)
BODY = (0x8B, 0x84, 0xF5)
BODY_LIGHT = (0x9C, 0x95, 0xFF)
LIMB = (0x75, 0x6D, 0xE0)
MOUTH = (0x3B, 0x35, 0x6B)
TOOTH = (0xFF, 0xFF, 0xFF)
EYE_WHITE = (0xFF, 0xFF, 0xFF)
EYE_IRIS = (0x22, 0x5C, 0x9E)
EYE_IRIS_DARK = (0x17, 0x3F, 0x72)
CARD = (0x12, 0x12, 0x16)
CARD_EDGE = (0x2A, 0x2A, 0x33)
SILVER = (0xB4, 0xBA, 0xC6)
FAN_RING = (0x6E, 0x6E, 0x80)
FAN_HUB = (0x9C, 0x9C, 0xB2)
GOLD = (0xE8, 0xBE, 0x4C)

SUPERSAMPLE = 3      # sub-samples per axis; 3x3 gives smooth edges cheaply


class Canvas:
    """Premultiplied-free RGBA canvas with coverage anti-aliasing."""

    def __init__(self, size: int) -> None:
        self.size = size
        self.pixels = [[(0.0, 0.0, 0.0, 0.0) for _ in range(size)]
                       for _ in range(size)]

    # -- compositing -----------------------------------------------------
    def blend(self, x: int, y: int, colour: tuple[int, int, int],
              alpha: float) -> None:
        if alpha <= 0.0 or not (0 <= x < self.size and 0 <= y < self.size):
            return
        alpha = min(1.0, alpha)
        r0, g0, b0, a0 = self.pixels[y][x]
        # Standard source-over on straight alpha.
        a1 = alpha + a0 * (1.0 - alpha)
        if a1 <= 0.0:
            self.pixels[y][x] = (0.0, 0.0, 0.0, 0.0)
            return
        r = (colour[0] * alpha + r0 * a0 * (1.0 - alpha)) / a1
        g = (colour[1] * alpha + g0 * a0 * (1.0 - alpha)) / a1
        b = (colour[2] * alpha + b0 * a0 * (1.0 - alpha)) / a1
        self.pixels[y][x] = (r, g, b, a1)

    def fill(self, predicate, colour: tuple[int, int, int],
             gradient: tuple[tuple[int, int, int], tuple[int, int, int]] | None = None) -> None:
        """Shade every pixel whose centre (with sub-sampling) matches."""
        step = 1.0 / SUPERSAMPLE
        offset = step / 2.0
        for y in range(self.size):
            for x in range(self.size):
                hits = 0
                for sy in range(SUPERSAMPLE):
                    for sx in range(SUPERSAMPLE):
                        px = x + offset + sx * step
                        py = y + offset + sy * step
                        if predicate(px / self.size, py / self.size):
                            hits += 1
                if not hits:
                    continue
                coverage = hits / (SUPERSAMPLE * SUPERSAMPLE)
                colour_here = colour
                if gradient is not None:
                    t = (y + 0.5) / self.size
                    top, bottom = gradient
                    colour_here = tuple(
                        int(round(top[i] + (bottom[i] - top[i]) * t))
                        for i in range(3))
                self.blend(x, y, colour_here, coverage)

    # -- export ----------------------------------------------------------
    def to_rgba_rows(self) -> list[bytes]:
        rows = []
        for row in self.pixels:
            line = bytearray()
            for r, g, b, a in row:
                line += bytes((int(round(r)), int(round(g)), int(round(b)),
                               int(round(a * 255))))
            rows.append(bytes(line))
        return rows


# --------------------------------------------------------------------------
# Shape predicates, all expressed in normalised 0..1 coordinates
# --------------------------------------------------------------------------


def rounded_rect(x: float, y: float, left: float, top: float, right: float,
                 bottom: float, radius: float) -> bool:
    if not (left <= x <= right and top <= y <= bottom):
        return False
    # Corner tests, so shapes have soft corners rather than hard ones.
    if radius <= 0.0:
        return True
    for cx, cy in ((left + radius, top + radius),
                   (right - radius, top + radius),
                   (left + radius, bottom - radius),
                   (right - radius, bottom - radius)):
        inside_x = (x < left + radius) if cx == left + radius else (x > right - radius)
        inside_y = (y < top + radius) if cy == top + radius else (y > bottom - radius)
        if inside_x and inside_y:
            return math.hypot(x - cx, y - cy) <= radius
    return True


def circle(x: float, y: float, cx: float, cy: float, r: float) -> bool:
    return math.hypot(x - cx, y - cy) <= r


def between(value: float, lo: float, hi: float) -> bool:
    return lo <= value <= hi


# -- the monster -----------------------------------------------------------


def body(x: float, y: float) -> bool:
    return rounded_rect(x, y, 0.14, 0.165, 0.86, 0.90, 0.11)


def limb_left(x: float, y: float) -> bool:
    return rounded_rect(x, y, 0.035, 0.44, 0.185, 0.80, 0.075)


def limb_right(x: float, y: float) -> bool:
    return rounded_rect(x, y, 0.815, 0.44, 0.965, 0.80, 0.075)


def foot_left(x: float, y: float) -> bool:
    return rounded_rect(x, y, 0.26, 0.80, 0.43, 0.975, 0.075)


def foot_right(x: float, y: float) -> bool:
    return rounded_rect(x, y, 0.57, 0.80, 0.74, 0.975, 0.075)


def _taper(x: float, y: float, base: tuple[float, float],
           tip: tuple[float, float], w0: float, w1: float) -> bool:
    """A cone from `base` to `tip`, tapering from half-width w0 to w1.

    Distance to the centre line rather than a sampled curve: the rasteriser asks
    this for every sub-sample of every pixel, and a loop-per-pixel version takes
    minutes at 256 px.
    """
    vx, vy = tip[0] - base[0], tip[1] - base[1]
    span = vx * vx + vy * vy
    if span <= 0.0:
        return False
    u = ((x - base[0]) * vx + (y - base[1]) * vy) / span
    if u < 0.0:
        u = 0.0
    elif u > 1.0:
        u = 1.0
    nearest_x = base[0] + u * vx
    nearest_y = base[1] + u * vy
    return math.hypot(x - nearest_x, y - nearest_y) <= w0 + (w1 - w0) * u


def horn_left(x: float, y: float) -> bool:
    """A horn sweeping up and out from the top corner of the head."""
    return _taper(x, y, (0.300, 0.300), (0.110, 0.048), 0.085, 0.012)


def horn_right(x: float, y: float) -> bool:
    return _taper(x, y, (0.700, 0.300), (0.890, 0.048), 0.085, 0.012)


def horn_left_pale(x: float, y: float) -> bool:
    """The lit side of the horn, so it reads as a horn and not a blob."""
    return _taper(x, y, (0.282, 0.286), (0.124, 0.066), 0.040, 0.005)


def horn_right_pale(x: float, y: float) -> bool:
    return _taper(x, y, (0.718, 0.286), (0.876, 0.066), 0.040, 0.005)


# -- the face --------------------------------------------------------------


def eye_left(x: float, y: float) -> bool:
    return circle(x, y, 0.365, 0.395, 0.105)


def eye_right(x: float, y: float) -> bool:
    return circle(x, y, 0.635, 0.395, 0.105)


def iris_left(x: float, y: float) -> bool:
    return circle(x, y, 0.385, 0.415, 0.058)


def iris_right(x: float, y: float) -> bool:
    return circle(x, y, 0.615, 0.415, 0.058)


def pupil_left(x: float, y: float) -> bool:
    return circle(x, y, 0.395, 0.425, 0.026)


def pupil_right(x: float, y: float) -> bool:
    return circle(x, y, 0.605, 0.425, 0.026)


def glint_left(x: float, y: float) -> bool:
    return circle(x, y, 0.352, 0.378, 0.021)


def glint_right(x: float, y: float) -> bool:
    return circle(x, y, 0.602, 0.378, 0.021)


# -- the card in the mouth -------------------------------------------------


def mouth(x: float, y: float) -> bool:
    return rounded_rect(x, y, 0.185, 0.515, 0.815, 0.845, 0.060)


def tooth_left(x: float, y: float) -> bool:
    """A fang from the top lip, biting into the card.

    It starts at the lip rather than higher up: any nearer the eyes and the two
    shapes read as one, which made the first draft look like the eyes were
    dripping.
    """
    return _taper(x, y, (0.335, 0.548), (0.335, 0.700), 0.045, 0.010)


def tooth_right(x: float, y: float) -> bool:
    return _taper(x, y, (0.665, 0.548), (0.665, 0.700), 0.045, 0.010)


def card(x: float, y: float) -> bool:
    """The board: a long black card held across the mouth.

    Taller than the first attempt, which left a rim of the mouth visible above
    and below it - that rim is what made the card read as translucent.
    """
    return rounded_rect(x, y, 0.080, 0.596, 0.920, 0.792, 0.048)


def card_shroud(x: float, y: float) -> bool:
    """The cooler shroud: the top of the card, a shade lighter than the rest."""
    return card(x, y) and between(y, 0.596, 0.650)


def bracket(x: float, y: float) -> bool:
    """The PCI bracket at the left end, with its mounting tab.

    The most GPU-shaped detail there is: a silver plate at the end of the card,
    taller than the card itself, with the tab that screws to the case.
    """
    return (rounded_rect(x, y, 0.078, 0.564, 0.142, 0.824, 0.020)
            or rounded_rect(x, y, 0.078, 0.564, 0.116, 0.596, 0.006))


def bracket_slots(x: float, y: float) -> bool:
    """Two display outputs cut into the bracket."""
    return (between(x, 0.090, 0.130)
            and (between(y, 0.641, 0.669) or between(y, 0.696, 0.724)))


def power_plug(x: float, y: float) -> bool:
    """The eight-pin power connector sitting on the top edge."""
    return rounded_rect(x, y, 0.735, 0.548, 0.872, 0.606, 0.014)


def power_pins(x: float, y: float) -> bool:
    return power_plug(x, y) and not between(y, 0.588, 0.606) \
        and ((x - 0.743) % 0.032) <= 0.020


def fan_left(x: float, y: float) -> bool:
    return circle(x, y, 0.365, 0.706, 0.082)


def fan_right(x: float, y: float) -> bool:
    return circle(x, y, 0.655, 0.706, 0.082)


def fan_left_ring(x: float, y: float) -> bool:
    return circle(x, y, 0.365, 0.706, 0.082) and not circle(x, y, 0.365, 0.706, 0.050)


def fan_right_ring(x: float, y: float) -> bool:
    return circle(x, y, 0.655, 0.706, 0.082) and not circle(x, y, 0.655, 0.706, 0.050)


def fan_left_hub(x: float, y: float) -> bool:
    return circle(x, y, 0.365, 0.706, 0.026)


def fan_right_hub(x: float, y: float) -> bool:
    return circle(x, y, 0.655, 0.706, 0.026)


def fan_blades(x: float, y: float) -> bool:
    """Five blades per fan, drawn as radial spokes with a gap between them."""
    spoke = 2 * math.pi / 5
    for cx in (0.365, 0.655):
        dx, dy = x - cx, y - 0.706
        r = math.hypot(dx, dy)
        if not 0.026 < r < 0.080:
            continue
        angle = math.atan2(dy, dx) % spoke
        if angle < 0.34 or angle > spoke - 0.34:
            return True
    return False


def connector(x: float, y: float) -> bool:
    """The PCIe edge fingers along the bottom of the board."""
    if not (0.215 <= x <= 0.925 and 0.792 <= y <= 0.828):
        return False
    return (x - 0.215) % 0.045 <= 0.028


def card_notch(x: float, y: float) -> bool:
    """The key notch in the connector, which is what makes it read as PCIe."""
    return between(x, 0.452, 0.508) and between(y, 0.786, 0.834)


# --------------------------------------------------------------------------
# PNG and ICO
# --------------------------------------------------------------------------


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def encode_png(rows: Sequence[bytes], width: int, height: int) -> bytes:
    raw = bytearray()
    for row in rows:
        raw.append(0)          # filter type 0
        raw.extend(row)
    return (b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR",
                         struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + _png_chunk(b"IEND", b""))


def bmp_frame(rows: Sequence[bytes], size: int) -> bytes:
    """An ICO bitmap frame: BITMAPINFOHEADER, BGRA bottom-up, then an AND mask."""
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0,
                         size * size * 4, 0, 0, 0, 0)
    body = bytearray()
    for row in reversed(rows):                 # bottom-up
        for x in range(size):
            r, g, b, a = row[x * 4:x * 4 + 4]
            body += bytes((b, g, r, a))
    # The AND mask is 1 bit per pixel, rows padded to 4 bytes. Zero means
    # "use the alpha channel", which is what we want everywhere.
    stride = ((size + 31) // 32) * 4
    body += bytes(stride * size)
    return header + bytes(body)


def write_ico(path: str, frames: list[tuple[int, bytes]]) -> None:
    count = len(frames)
    header = struct.pack("<HHH", 0, 1, count)
    directory = bytearray()
    offset = 6 + 16 * count
    for size, payload in frames:
        directory += struct.pack("<BBBBHHII",
                                 0 if size >= 256 else size,
                                 0 if size >= 256 else size,
                                 0, 0, 1, 32, len(payload), offset)
        offset += len(payload)
    with open(path, "wb") as fh:
        fh.write(header)
        fh.write(bytes(directory))
        for _size, payload in frames:
            fh.write(payload)


def draw(size: int) -> Canvas:
    """The mascot: a horned monster eating a graphics card.

    Painted back to front, and deliberately built from circles and rounded
    rectangles only, so the silhouette survives being shrunk to 16 px.
    """
    canvas = Canvas(size)
    # Tile: a dark violet plate, so the icon reads on a light or dark taskbar.
    canvas.fill(lambda x, y: rounded_rect(x, y, 0.0, 0.0, 1.0, 1.0, 0.19),
                TILE_TOP, gradient=(TILE_TOP, TILE_BOTTOM))
    # Horns, then the body on top of their lower half.
    canvas.fill(horn_left, HORN)
    canvas.fill(horn_right, HORN)
    canvas.fill(horn_left_pale, HORN_LIGHT)
    canvas.fill(horn_right_pale, HORN_LIGHT)
    # Limbs behind the body so only the parts that stick out are visible.
    for shape in (limb_left, limb_right, foot_left, foot_right):
        canvas.fill(shape, LIMB)
    canvas.fill(body, BODY, gradient=(BODY_LIGHT, BODY))
    # Face.
    canvas.fill(eye_left, EYE_WHITE)
    canvas.fill(eye_right, EYE_WHITE)
    canvas.fill(iris_left, EYE_IRIS)
    canvas.fill(iris_right, EYE_IRIS)
    canvas.fill(pupil_left, EYE_IRIS_DARK)
    canvas.fill(pupil_right, EYE_IRIS_DARK)
    canvas.fill(glint_left, EYE_WHITE)
    canvas.fill(glint_right, EYE_WHITE)
    # The mouth, the fangs hanging into it, then the card held across it.
    canvas.fill(mouth, MOUTH)
    canvas.fill(tooth_left, TOOTH)
    canvas.fill(tooth_right, TOOTH)
    canvas.fill(power_plug, CARD_EDGE)
    canvas.fill(power_pins, CARD)
    canvas.fill(card_shroud, CARD_EDGE)
    canvas.fill(card, CARD)
    canvas.fill(bracket, SILVER)
    canvas.fill(bracket_slots, CARD)
    canvas.fill(fan_left_ring, FAN_RING)
    canvas.fill(fan_right_ring, FAN_RING)
    canvas.fill(fan_blades, FAN_RING)
    canvas.fill(fan_left_hub, FAN_HUB)
    canvas.fill(fan_right_hub, FAN_HUB)
    canvas.fill(connector, GOLD)
    canvas.fill(card_notch, CARD)
    return canvas


def main() -> int:
    sizes = (16, 24, 32, 48, 64, 128, 256)
    frames: list[tuple[int, bytes]] = []
    master = None
    for size in sizes:
        canvas = draw(size)
        rows = canvas.to_rgba_rows()
        if size == 256:
            master = rows
        # Above 64 px the ICO spec expects PNG-compressed frames.
        if size >= 128:
            frames.append((size, encode_png(rows, size, size)))
        else:
            frames.append((size, bmp_frame(rows, size)))
    here = os.path.dirname(os.path.abspath(__file__))
    ico_path = os.path.join(here, "gpumon.ico")
    write_ico(ico_path, frames)
    if master is not None:
        with open(os.path.join(here, "gpumon.png"), "wb") as fh:
            fh.write(encode_png(master, 256, 256))
    print(f"wrote {ico_path} with frames {[s for s, _ in frames]}")
    print(f"wrote {os.path.join(here, 'gpumon.png')} (256x256 preview)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
