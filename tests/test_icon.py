"""Verify the generated icon is valid and actually draws something sensible.

Decodes the PNG frames and the ICO directory so the result is checked rather
than assumed: a blank or malformed icon would otherwise go unnoticed.
"""

from __future__ import annotations
# GPUMON_TEST_BOOTSTRAP
# Run from anywhere, and from any working directory: the program modules and the
# packaging scripts are one level up, and the paths in here are relative to the
# repository root.
import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
for _extra in (_ROOT, _os.path.join(_ROOT, "scripts")):
    if _extra not in _sys.path:
        _sys.path.insert(0, _extra)
_os.chdir(_ROOT)

def _skip_machine_specific_test() -> None:
    """Stop, with a note, when there is no hardware or desktop to test against.

    Set by continuous integration. A test that measures a window or asserts that
    a vendor's library is installed cannot say anything useful on a machine that
    has neither, and reporting a failure there trains everybody to ignore red.
    """
    if _os.environ.get("GPUMON_SKIP_MACHINE_TESTS"):
        print("  --  skipped: this test needs a GPU, a vendor driver, a desktop "
              "session or the sensor driver, and GPUMON_SKIP_MACHINE_TESTS is set")
        raise SystemExit(0)





import struct
import zlib

problems: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if ok else 'FAIL'} {label}{'  ' + detail if detail else ''}")
    if not ok:
        problems.append(label)


def decode_png(data: bytes) -> tuple[int, int, list[bytes]]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    pos, width, height, idat = 8, 0, 0, bytearray()
    while pos < len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        tag = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        if tag == b"IHDR":
            width, height, depth, ctype = struct.unpack(">IIBB", body[:10])
            assert depth == 8 and ctype == 6, f"expected RGBA8, got {depth}/{ctype}"
        elif tag == b"IDAT":
            idat.extend(body)
        pos += 12 + length
    raw = zlib.decompress(bytes(idat))
    stride = width * 4
    rows = []
    p = 0
    for _y in range(height):
        assert raw[p] == 0, "unexpected filter type"
        rows.append(raw[p + 1:p + 1 + stride])
        p += stride + 1
    return width, height, rows


def analyse(rows: list[bytes], size: int, label: str) -> None:
    colours: dict[tuple[int, int, int], int] = {}
    opaque = 0
    for row in rows:
        for x in range(size):
            r, g, b, a = row[x * 4:x * 4 + 4]
            if a > 128:
                opaque += 1
                colours[(r, g, b)] = colours.get((r, g, b), 0) + 1
    total = size * size
    print(f"\n  {label} {size}x{size}: {opaque}/{total} opaque pixels "
          f"({opaque / total * 100:.0f}%), {len(colours)} distinct colours")
    top = sorted(colours.items(), key=lambda kv: -kv[1])[:6]
    for (r, g, b), count in top:
        print(f"      #{r:02x}{g:02x}{b:02x}: {count}")

    def near(colour, target, tolerance=40) -> bool:
        return all(abs(colour[i] - target[i]) <= tolerance for i in range(3))

    # The mascot: a violet monster with tan horns, a dark tile behind it and a
    # black card - with its two fans and gold edge fingers - in its mouth.
    parts = {
        "violet body": (0x8B, 0x84, 0xF5, 45),
        "tan horns": (0xF2, 0xC9, 0x9B, 40),
        "dark tile": (0x11, 0x0C, 0x22, 30),
        "black card": (0x12, 0x12, 0x16, 18),
        "white eye/fang": (0xFF, 0xFF, 0xFF, 20),
        "gold card edge": (0xE8, 0xBE, 0x4C, 60),
    }
    found = {}
    for name, (r, g, b, tol) in parts.items():
        found[name] = any(near(c, (r, g, b), tol) for c in colours)
        print(f"      {name:16} present: {found[name]}")

    if opaque < total * 0.3:
        problems.append(f"{label}: icon looks empty ({opaque}/{total} opaque)")
    for name, present in found.items():
        if not present:
            problems.append(f"{label}: no {name} anywhere in the icon")
    # The eye whites and the card must be more than a rounding error, or the
    # face does not read at taskbar size.
    white = sum(count for c, count in colours.items() if sum(c) > 720)
    dark = sum(count for c, count in colours.items() if sum(c) < 90)
    print(f"      white pixels {white} ({white / total * 100:.1f}%), "
          f"near-black {dark} ({dark / total * 100:.1f}%)")
    if white < total * 0.01:
        problems.append(f"{label}: too little white for eyes and fangs")
    if dark < total * 0.02:
        problems.append(f"{label}: too little dark for the card and tile")


print("=" * 80)
print("ICON VERIFICATION")
print("=" * 80)

print("\n[1] PNG preview")
with open("gpumon.png", "rb") as fh:
    png = fh.read()
width, height, rows = decode_png(png)
check("PNG is 256x256", (width, height) == (256, 256), f"{width}x{height}")
analyse(rows, 256, "preview")

print("\n[2] ICO container")
with open("gpumon.ico", "rb") as fh:
    ico = fh.read()
reserved, kind, count = struct.unpack("<HHH", ico[:6])
check("ICO magic is 1 (icon)", kind == 1, str(kind))
check("reserved field is 0", reserved == 0)
check("seven frames present", count == 7, f"{count} frames")

entries = []
for i in range(count):
    off = 6 + i * 16
    w, h, colours, res, planes, bpp, length, offset = struct.unpack(
        "<BBBBHHII", ico[off:off + 16])
    entries.append((w or 256, h or 256, bpp, length, offset))
for w, h, bpp, length, offset in entries:
    print(f"      {w:>3}x{h:<3} {bpp}bpp  {length:>6} bytes at offset {offset}")
    if offset + length > len(ico):
        problems.append(f"frame {w}x{h} runs past the end of the file")
check("all frame offsets are in range",
      all(o + l <= len(ico) for _w, _h, _b, l, o in entries))
check("contains a 16x16 frame", any(w == 16 for w, _h, _b, _l, _o in entries))
check("contains a 256x256 frame", any(w == 256 for w, _h, _b, _l, _o in entries))

print("\n[3] the 128px PNG frame decodes")
frame = next(e for e in entries if e[0] == 128)
offset, length = frame[4], frame[3]
payload = ico[offset:offset + length]
w, h, frame_rows = decode_png(payload)
check("frame decodes as 128x128 PNG", (w, h) == (128, 128), f"{w}x{h}")
analyse(frame_rows, 128, "ico frame")

print("\n[4] the 32px bitmap frame has the right size")
frame32 = next(e for e in entries if e[0] == 32)
payload = ico[frame32[4]:frame32[4] + frame32[3]]
header_size, bw, bh, planes, bpp = struct.unpack("<IiiHH", payload[:16])
expected = 40 + 32 * 32 * 4 + ((32 + 31) // 32) * 4 * 32
check("BITMAPINFOHEADER size is 40", header_size == 40, str(header_size))
check("bitmap width is 32", bw == 32, str(bw))
check("bitmap height is doubled (XOR+AND)", bh == 64, str(bh))
check("bitmap is 32bpp", bpp == 32, str(bpp))
check("bitmap frame length is exact", len(payload) == expected,
      f"{len(payload)} vs {expected}")

print("\n" + "=" * 80)
if problems:
    print(f"ICON VERIFICATION FAILED ({len(problems)})")
    for p in problems:
        print(f"  - {p}")
else:
    print("ICON VERIFICATION PASSED")
print("=" * 80)
raise SystemExit(1 if problems else 0)
