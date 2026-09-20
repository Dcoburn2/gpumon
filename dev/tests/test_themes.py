"""Verify the theme catalogue: valid colours, readable contrast, distinct themes.

A palette is data, so most of what can go wrong is a typo or a colour that
cannot be read on the background it ships with. Both are checkable without a
display, which is what this does - the desktop, terminal and browser front ends
all render from these same values, so a bad entry is a bad entry everywhere.
"""

from __future__ import annotations
# GPUMON_TEST_BOOTSTRAP
# Run from anywhere, and from any working directory: the program modules and the
# packaging scripts are one level up, and the paths in here are relative to the
# repository root.
import os as _os
import sys as _sys

#: dev/, where this test lives: the packaging scripts, and the build output.
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
#: The repository root, one level above. The program is here, so this is where the
#: tests run from and the paths they use are relative to.
_REPO = _os.path.dirname(_ROOT)
#: Anything under dev/ is written as a path from the root - "dev/release/..." -
#: because that is where it is from here.
_DEV = "dev"
for _extra in (_REPO, _ROOT, _os.path.join(_ROOT, "scripts")):
    if _extra not in _sys.path:
        _sys.path.insert(0, _extra)
_os.chdir(_REPO)

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


def _windows_only_test() -> None:
    """Stop, with a note, on anything that is not Windows.

    Some of the suite is about Windows itself: the .cmd launcher, the registry
    lookup for the desktop folder, LibreHardwareMonitor's Windows backends, a
    signing stub written as a .cmd, ctypes.WinDLL. None of that can say anything
    about a Linux machine, and a failure there is noise rather than a finding.
    """
    if _os.name != "nt":
        print("  --  skipped: this test is about Windows")
        raise SystemExit(0)





import colorsys
import re

from ui import themes

problems: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if ok else 'FAIL'} {label}{'  ' + detail if detail else ''}")
    if not ok:
        problems.append(label)


HEX = re.compile(r"^#[0-9a-f]{6}$")
COLOUR_FIELDS = ("bg", "panel", "panel_alt", "border", "text", "text_dim",
                 "text_bright", "accent", "ok", "warn", "crit", "idle",
                 "selection", "plot_bg")


def rgb(value: str) -> tuple[int, int, int]:
    return int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16)


def luminance(value: str) -> float:
    """WCAG relative luminance."""
    weights = (0.2126, 0.7152, 0.0722)
    out = 0.0
    for weight, channel in zip(weights, rgb(value)):
        c = channel / 255
        out += weight * (c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
    return out


def contrast(a: str, b: str) -> float:
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def hue(value: str) -> float:
    r, g, b = (c / 255 for c in rgb(value))
    return colorsys.rgb_to_hsv(r, g, b)[0] * 360


def is_light(palette) -> bool:
    """True when the page background is lighter than the text on it."""
    return luminance(palette.bg) > luminance(palette.text)


def rgb_distance(a: str, b: str) -> float:
    """Euclidean distance in RGB, which stays meaningful between dark colours."""
    return sum((x - y) ** 2 for x, y in zip(rgb(a), rgb(b))) ** 0.5


print("=" * 84)
print("THEME TEST")
print("=" * 84)

print("\n[1] the catalogue")
catalog = themes.catalog()
keys = [key for key, _label, _blurb in catalog]
print(f"    {len(keys)} themes: {', '.join(keys)}")
check("eleven themes are defined", len(keys) == 11, str(len(keys)))
check("the default is first", keys[0] == themes.DEFAULT_KEY, keys[0])
check("the default is Shinji",
      themes.DEFAULT_KEY == "shinji" and keys[0] == "shinji",
      f"default={themes.DEFAULT_KEY}")
CHARACTER_PALETTES = {"shinji", "rei", "asuka", "touji", "mari", "kaworu"}
check("every character palette is present",
      CHARACTER_PALETTES <= set(keys), str(sorted(CHARACTER_PALETTES - set(keys))))
# Shinji leads deliberately; the rest of the character palettes follow it, and the
# five that are not character palettes come last.
check("the character palettes lead, in order",
      [k for k in keys if k in CHARACTER_PALETTES] ==
      ["shinji", "rei", "asuka", "touji", "mari", "kaworu"],
      str([k for k in keys if k in CHARACTER_PALETTES]))
check("five palettes that are not character themes join them",
      len([k for k in keys if k not in CHARACTER_PALETTES]) == 5,
      str([k for k in keys if k not in CHARACTER_PALETTES]))
check("every theme has a label and a blurb",
      all(label and blurb for _k, label, blurb in catalog))
check("labels are unique", len({label for _k, label, _b in catalog}) == len(keys))
check("exactly one theme is light",
      len([k for k in keys if is_light(themes.get(k))]) == 1,
      str([k for k in keys if is_light(themes.get(k))]))

print("\n[2] every colour is a valid hex value")
for key in keys:
    palette = themes.get(key)
    bad = [name for name in COLOUR_FIELDS if not HEX.match(getattr(palette, name))]
    check(f"{key}: {len(COLOUR_FIELDS)} colours parse", not bad, str(bad))
    check(f"{key}: series entries parse",
          len(palette.series) >= 8 and all(HEX.match(c) for c in palette.series),
          f"{len(palette.series)} entries")
    check(f"{key}: series entries are distinguishable",
          len(set(palette.series)) == len(palette.series))
    check(f"{key}: series is immutable", isinstance(palette.series, tuple))

print("\n[3] text is readable on the background it ships with")
for key in keys:
    p = themes.get(key)
    body = contrast(p.text, p.panel)
    dim = contrast(p.text_dim, p.panel)
    bright = contrast(p.text_bright, p.panel)
    graph = contrast(p.accent, p.plot_bg)
    kind = "light" if is_light(p) else "dark"
    print(f"    {key:8} {kind:5} text {body:4.1f}:1   dim {dim:4.1f}:1   "
          f"bright {bright:5.1f}:1   accent-on-plot {graph:4.1f}:1")
    # 4.5:1 is WCAG AA for body text; the dim tier is deliberately secondary,
    # and the accent is a 1px trace rather than text, so it gets a lower bar.
    check(f"{key}: body text reaches AA", body >= 4.5, f"{body:.1f}:1")
    check(f"{key}: bright text reaches AA", bright >= 4.5, f"{bright:.1f}:1")
    check(f"{key}: dim text stays legible", dim >= 2.5, f"{dim:.1f}:1")
    check(f"{key}: accent is visible on the plot", graph >= 1.6, f"{graph:.1f}:1")
    check(f"{key}: plot inset differs from the panel",
          p.plot_bg != p.panel and p.plot_bg != p.bg)
    # The inset has to read as a recessed surface, not as the same flat page.
    # Compared as RGB distance, not luminance: relative luminance squashes every
    # dark colour toward zero, so #080c11 and #111823 measure as nearly equal
    # while being plainly different on screen.
    check(f"{key}: the plot inset is offset from the panel it sits in",
          rgb_distance(p.plot_bg, p.panel) >= 12,
          f"{rgb_distance(p.plot_bg, p.panel):.0f} of 441")
    # Grid lines and frame borders have to be visible on that inset.
    check(f"{key}: grid lines are visible on the plot",
          contrast(p.border, p.plot_bg) >= 1.15,
          f"{contrast(p.border, p.plot_bg):.2f}:1")
    # Series colours are drawn as 1px traces and fills on the inset.
    worst = min(contrast(c, p.plot_bg) for c in p.series)
    check(f"{key}: every series colour shows on the plot", worst >= 1.4,
          f"worst {worst:.2f}:1")

print("\n[4] alarm colours keep their meaning")
# The monochrome themes deliberately do not: they trade hue for a single
# phosphor, and the alarm level is still carried by the text and the banner.
MONOCHROME = {"amber"}
for key in keys:
    p = themes.get(key)
    check(f"{key}: ok/warn/crit are distinct",
          len({p.ok, p.warn, p.crit}) == 3)
    if key in MONOCHROME:
        continue
    ok_h, warn_h, crit_h = hue(p.ok), hue(p.warn), hue(p.crit)
    green = 80 <= ok_h <= 190
    warm = 20 <= warn_h <= 70
    red = crit_h <= 20 or crit_h >= 340
    check(f"{key}: green means ok, red means critical",
          green and warm and red,
          f"ok {ok_h:.0f}deg warn {warn_h:.0f}deg crit {crit_h:.0f}deg")

print("\n[5] the colour-blind pair is not used for alarm state")
# red/green is the pair that alarm levels must not rely on exclusively; the
# themes keep a separate amber warning tier so the two never collide.
for key in keys:
    p = themes.get(key)
    check(f"{key}: warning is not the critical colour", p.warn != p.crit)

print("\n[6] themes are actually different from each other")
signatures = {(themes.get(k).bg, themes.get(k).panel, themes.get(k).accent)
              for k in keys}
check("no two themes share background, panel and accent",
      len(signatures) == len(keys), f"{len(signatures)} distinct of {len(keys)}")
for pair in (("rei", "shinji"), ("shinji", "asuka"), ("asuka", "mari")):
    a, b = (themes.get(k) for k in pair)
    check(f"{pair[0]} and {pair[1]} differ in background and accent",
          a.bg != b.bg and a.accent != b.accent)

print("\n[7] lookup, aliases and fallbacks")
check("aliases resolve", themes.normalize("Shinji") == "shinji"
      and themes.normalize("toji") == "touji"
      and themes.normalize("01") == "shinji"
      and themes.normalize("gruvbox_dark") == "gruvbox"
      and themes.normalize("  Nord  ") == "nord",
      str([themes.normalize(n) for n in
           ("Shinji", "toji", "01", "gruvbox_dark", "  Nord  ")]))
# The unit designations are gone on purpose. They are somebody else's names, and
# a config or a habit that still uses them should fall back to the default rather
# than quietly resolving to anything.
check("the unit designations no longer resolve",
      themes.normalize("EVA-01") == "" and themes.normalize("unit-01") == "",
      f"{themes.normalize('EVA-01')!r} {themes.normalize('unit-01')!r}")
check("an unknown name normalises to nothing", themes.normalize("chartreuse") == "")
check("get() falls back to the default",
      themes.get("chartreuse").key == themes.DEFAULT_KEY)
before = themes.current_key()
themes.set_theme("no-such-theme")
check("set_theme refuses to leave the app colourless",
      themes.current_key() == themes.DEFAULT_KEY, themes.current_key())
themes.set_theme(before)
check("set_theme is restorable", themes.current_key() == before, before)
check("every key round-trips through normalize",
      all(themes.normalize(k) == k for k in keys))

print("\n" + "=" * 84)
if problems:
    print(f"THEME TEST FAILED ({len(problems)})")
    for p in problems:
        print(f"  - {p}")
else:
    print("THEME TEST PASSED")
print("=" * 84)
raise SystemExit(1 if problems else 0)
