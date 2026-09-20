"""Colour themes for every gpumon front end.

Colours live here rather than in `ui/widgets.py` so that all four front ends can
share one definition: the desktop window reads them through `ui.widgets`, the
terminal maps them onto ANSI, and the browser GUI emits them as CSS variables.
A theme is data, so adding one is a dict entry and nothing else.

`ui/widgets.py` used to hold these as module-level constants, which froze the
colours at import. It now resolves them on access (PEP 562 module `__getattr__`),
so switching a theme takes effect for every widget built afterwards without a
restart - which is why the desktop app rebuilds its window when the theme
changes.

Nothing here imports tkinter: the TUI on a headless machine must be able to read
a palette without a display.
"""
from __future__ import annotations

from dataclasses import dataclass, fields

#: Ordered; the first entry is the default and the order is the menu order.
#: Shinji is the default - violet with a fluorescent green trim - which is what
#: the program looks like out of the box. `nvtop`, the palette this started from,
#: is still there and still one click away.
DEFAULT_KEY = "shinji"


@dataclass(frozen=True)
class Palette:
    """One complete colour scheme.

    `ok`/`warn`/`crit` carry alarm meaning and stay green/amber/red in every
    theme, including the monochrome ones - a theme that recoloured them would
    cost more than it added. `plot_bg` is the inset background charts draw on,
    which is what gives the graphs their panel look.
    """

    key: str
    label: str
    blurb: str
    bg: str
    panel: str
    panel_alt: str
    border: str
    text: str
    text_dim: str
    text_bright: str
    accent: str
    ok: str
    warn: str
    crit: str
    idle: str
    selection: str
    plot_bg: str
    series: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


def _p(key: str, label: str, blurb: str, bg: str, panel: str, panel_alt: str,
       border: str, text: str, text_dim: str, text_bright: str, accent: str,
       ok: str, warn: str, crit: str, idle: str, selection: str, plot_bg: str,
       series: list[str]) -> Palette:
    return Palette(key=key, label=label, blurb=blurb, bg=bg, panel=panel,
                   panel_alt=panel_alt, border=border, text=text,
                   text_dim=text_dim, text_bright=text_bright, accent=accent,
                   ok=ok, warn=warn, crit=crit, idle=idle, selection=selection,
                   plot_bg=plot_bg, series=tuple(series))


THEMES: dict[str, Palette] = {}


def _add(palette: Palette) -> None:
    THEMES[palette.key] = palette


# --------------------------------------------------------------------------
# The default. The first palette registered is the one it starts on.
# --------------------------------------------------------------------------
_add(_p(
    "shinji", "Shinji", "violet with a fluorescent green trim",
    bg="#0a0710", panel="#17102a", panel_alt="#120c20", border="#2f2150",
    text="#ded3f5", text_dim="#8d7cb5", text_bright="#f4eeff",
    accent="#9a6bff", ok="#7ee787", warn="#ffc247", crit="#ff4d6d",
    idle="#3a2f5c", selection="#241a44", plot_bg="#070410",
    series=["#7ee787", "#9a6bff", "#4dd0e1", "#ffc247",
            "#f48fb1", "#b39ddb", "#69f0ae", "#82b1ff"]))

# --------------------------------------------------------------------------
# The original nvtop-inspired scheme, kept for anyone who prefers it
# --------------------------------------------------------------------------
_add(_p(
    "nvtop", "nvtop", "the original: cold blue on near-black",
    bg="#0b0f14", panel="#111823", panel_alt="#0e141d", border="#1e2a38",
    text="#c8d4e0", text_dim="#6b7d90", text_bright="#eaf2fa",
    accent="#3ea6ff", ok="#3ddc84", warn="#ffb020", crit="#ff4d4f",
    idle="#2a3a4a", selection="#16324a", plot_bg="#080c11",
    series=["#3ea6ff", "#b388ff", "#00d4c8", "#ffa657",
            "#7ee787", "#ff7eb6", "#d2a8ff", "#79c0ff"]))

_add(_p(
    "rei", "Rei", "light grey with orange trim",
    # The one light theme: white-grey is the page, and the orange accent,
    # blue-grey shading and dark text do the rest. Every colour a light
    # background needs - text, grid, series - is darker here, which is why it
    # is not simply the dark palette inverted.
    bg="#dfe4e8", panel="#eef1f4", panel_alt="#e7ebef", border="#94a5b3",
    text="#1f2b34", text_dim="#5d6e7a", text_bright="#0b141a",
    accent="#d9480f", ok="#00875f", warn="#b8860b", crit="#d70000",
    idle="#c3ced7", selection="#bcd4e6", plot_bg="#cbd4db",
    series=["#1864ab", "#c2255c", "#2b8a3e", "#d9480f",
            "#6741d9", "#0b7285", "#a61e4e", "#5f3dc4"]))

_add(_p(
    "asuka", "Asuka", "red, black and orange",
    bg="#100507", panel="#200c10", panel_alt="#190809", border="#3d1a1f",
    text="#f2d8d5", text_dim="#ab767c", text_bright="#fff0ee",
    accent="#ff6b35", ok="#4ecb71", warn="#ffd23f", crit="#ff1744",
    idle="#43222a", selection="#33151a", plot_bg="#0b0304",
    series=["#ff6b35", "#ffd23f", "#4ecb71", "#ff9e9e",
            "#ffb4a2", "#e0e0e0", "#ff8a65", "#f4a261"]))

_add(_p(
    "touji", "Touji", "charcoal and silver with a red core",
    bg="#08090b", panel="#14171b", panel_alt="#101317", border="#2a2f36",
    text="#cfd6dd", text_dim="#7b8794", text_bright="#eef2f6",
    # Silver is the accent rather than the red: red already means "critical"
    # everywhere else in the app, and this palette has plenty of grey to carry a
    # neutral highlight.
    accent="#c0c8d0", ok="#4fb477", warn="#e0a458", crit="#ff5252",
    idle="#333a42", selection="#1e242b", plot_bg="#050608",
    series=["#c0c8d0", "#e5484d", "#6ea8fe", "#e0a458",
            "#4fb477", "#b197fc", "#ff9f68", "#63e6be"]))

_add(_p(
    "mari", "Mari", "magenta and pink with white trim",
    bg="#0d0611", panel="#1d0f21", panel_alt="#160a19", border="#3c2141",
    text="#f1d9ef", text_dim="#ab7cab", text_bright="#fdeffb",
    accent="#ff5fb0", ok="#6ee7b7", warn="#ffc857", crit="#ff2d6f",
    idle="#412441", selection="#301830", plot_bg="#0a040d",
    series=["#ff5fb0", "#6ee7b7", "#c084fc", "#ffc857",
            "#f9a8d4", "#a5b4fc", "#fda4af", "#5eead4"]))

_add(_p(
    "kaworu", "Kaworu", "black and violet with dual green cores",
    bg="#06070c", panel="#121420", panel_alt="#0d0f18", border="#262a3d",
    text="#d3d7e6", text_dim="#7e85a3", text_bright="#f0f2fa",
    accent="#b06bff", ok="#35d07f", warn="#ffc043", crit="#ff4d6d",
    idle="#2b3049", selection="#1b1f33", plot_bg="#04050a",
    series=["#b06bff", "#35d07f", "#4dd0e1", "#ff6ec7",
            "#ffc043", "#8da2ff", "#ff8a65", "#7cf5c4"]))

# --------------------------------------------------------------------------
# Five that are not the character palettes
# --------------------------------------------------------------------------
_add(_p(
    "nord", "Nord", "cool arctic blue-grey",
    bg="#2e3440", panel="#3b4252", panel_alt="#343b48", border="#4c566a",
    text="#d8dee9", text_dim="#8b98b1", text_bright="#eceff4",
    accent="#88c0d0", ok="#a3be8c", warn="#ebcb8b", crit="#bf616a",
    idle="#4c566a", selection="#3b5266", plot_bg="#272c36",
    series=["#88c0d0", "#81a1c1", "#a3be8c", "#ebcb8b",
            "#b48ead", "#d08770", "#5e81ac", "#8fbcbb"]))

_add(_p(
    "gruvbox", "Gruvbox", "warm retro terminal",
    bg="#1d2021", panel="#282828", panel_alt="#232323", border="#3c3836",
    text="#ebdbb2", text_dim="#a89984", text_bright="#fbf1c7",
    accent="#d79921", ok="#8ec07c", warn="#fabd2f", crit="#fb4934",
    idle="#504945", selection="#4a3f2a", plot_bg="#17191a",
    series=["#83a598", "#d79921", "#8ec07c", "#fb4934",
            "#d3869b", "#b8bb26", "#fe8019", "#fabd2f"]))

_add(_p(
    "amber", "Amber CRT", "monochrome amber phosphor",
    bg="#0d0800", panel="#1a1206", panel_alt="#140e05", border="#3a2a0c",
    text="#ffc46b", text_dim="#a07536", text_bright="#ffe0a8",
    accent="#ffb000", ok="#ffd166", warn="#ff9f1c", crit="#ff6b35",
    idle="#4a3410", selection="#3a2a0c", plot_bg="#080500",
    series=["#ffb000", "#ffd166", "#ff8c42", "#ffe0a8",
            "#e08e0b", "#ffa94d", "#fff3bf", "#cc7a00"]))

_add(_p(
    "matrix", "Matrix", "green phosphor on black",
    bg="#000700", panel="#04120a", panel_alt="#030d07", border="#0d3b1f",
    text="#b6ffc9", text_dim="#4e9c68", text_bright="#d8ffe4",
    accent="#00ff41", ok="#00ff41", warn="#ffd400", crit="#ff3b3b",
    idle="#0a2a16", selection="#0b3a1e", plot_bg="#000400",
    series=["#00ff41", "#7cffb2", "#00d4a0", "#b6ff00",
            "#39ff88", "#00b34a", "#a8ff60", "#00e5ff"]))

#: Relaxed spellings people actually type, mapped onto a real key. A theme's
#: own key does not need an entry: `normalize` checks those first.
ALIASES = {
    "default": DEFAULT_KEY, "original": DEFAULT_KEY, "nvtop-style": DEFAULT_KEY,
    "01": "shinji", "1": "shinji", "violet": "shinji",
    "00": "rei", "0": "rei", "light": "rei", "light-grey": "rei",
    "02": "asuka", "2": "asuka",
    "toji": "touji", "03": "touji", "3": "touji",
    "08": "mari", "8": "mari",
    "kaoru": "kaworu", "13": "kaworu",
    "gruvbox-dark": "gruvbox", "nord-dark": "nord",
    "amber-crt": "amber", "crt": "amber", "phosphor": "amber",
    "matrix-green": "matrix",
}

_active: str = DEFAULT_KEY


def keys() -> list[str]:
    """Theme keys in menu order."""
    return list(THEMES)


def label(key: str) -> str:
    return get(key).label


def normalize(name: str) -> str:
    """Map a user-supplied name onto a theme key ('' when unknown)."""
    if not name:
        return ""
    slug = str(name).strip().lower().replace("_", "-").replace(" ", "-")
    if slug in THEMES:
        return slug
    if slug in ALIASES:
        return ALIASES[slug]
    collapsed = slug.replace("-", "")
    if collapsed in THEMES:
        return collapsed
    return ALIASES.get(collapsed, "")


def get(key: str) -> Palette:
    """The palette for `key`, falling back to the default."""
    return THEMES.get(normalize(key) or DEFAULT_KEY, THEMES[DEFAULT_KEY])


def current() -> Palette:
    return THEMES[_active]


def current_key() -> str:
    return _active


def set_theme(key: str) -> Palette:
    """Switch the active palette. Returns it, so callers can rebuild with it.

    An unknown name is not an error here: it is a config file someone edited by
    hand, or a `--theme` typo, and silently keeping the previous theme beats
    refusing to start. `normalize()` is public for callers that want to report
    the typo instead.
    """
    global _active
    resolved = normalize(key)
    _active = resolved or DEFAULT_KEY
    return current()


def catalog() -> list[tuple[str, str, str]]:
    """(key, label, blurb) for menus and `--list-themes`."""
    return [(p.key, p.label, p.blurb) for p in THEMES.values()]
