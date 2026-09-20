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

# --------------------------------------------------------------------------
# Twenty keycap palettes: each one a mechanical keyboard set's colour story,
# dark or light as that set is, and none of them sharing a page colour with
# another.
# --------------------------------------------------------------------------
_add(_p(
    "olivia", "Olivia", "soft rose and cream on warm charcoal",
    bg="#1a1618", panel="#251e20", panel_alt="#211b1d", border="#625b5b",
    text="#f2e9e4", text_dim="#908886", text_bright="#f4ede9",
    accent="#e8a0b4", ok="#7ee787", warn="#ffc247", crit="#ff4d6d",
    idle="#564f4f", selection="#4c383e", plot_bg="#110e10",
    series=["#eb98af", "#ebab98", "#ebd498", "#bdeb98",
            "#98ebb8", "#98e6eb", "#9d98eb", "#e198eb",
            ]))

_add(_p(
    "botanical", "Botanical", "cream page with deep leaf green",
    bg="#ece7d9", panel="#f5f1e6", panel_alt="#f2eee1", border="#ced0bf",
    text="#22331f", text_dim="#878e7f", text_bright="#1c2a19",
    accent="#3f7d4e", ok="#00875f", warn="#b8860b", crit="#d70000",
    idle="#c2c3b6", selection="#d1dac8", plot_bg="#b8b4a9",
    series=["#3b6b47", "#3b6b5f", "#3b5f6b", "#3f3b6b",
            "#673b6b", "#6b3b4f", "#6b573b", "#586b3b",
            ]))

_add(_p(
    "mizu", "Mizu", "pale water blue with deep navy ink",
    bg="#dfe9f0", panel="#eef4f8", panel_alt="#e9f0f5", border="#c2cedc",
    text="#14304a", text_dim="#7d8e9e", text_bright="#10273d",
    accent="#2b6cb0", ok="#00875f", warn="#b8860b", crit="#d70000",
    idle="#bac5ce", selection="#c7d9ea", plot_bg="#aeb6bb",
    series=["#22466b", "#23226b", "#48226b", "#6b2252",
            "#6b2f22", "#6b6022", "#2e6b22", "#226b54",
            ]))

_add(_p(
    "bow", "BoW", "black on white, nothing else",
    bg="#f7f7f7", panel="#ffffff", panel_alt="#fcfcfc", border="#d8d8d8",
    text="#111111", text_dim="#838383", text_bright="#0e0e0e",
    accent="#4a4a4a", ok="#6f6f6f", warn="#8a8a8a", crit="#4a4a4a",
    idle="#c6c6c6", selection="#dbdbdb", plot_bg="#c1c1c1",
    series=["#2f2f2f", "#3d3d3d", "#4b4b4b", "#595959",
            "#676767", "#757575", "#838383", "#919191",
            ]))

_add(_p(
    "nautilus", "Nautilus", "deep cobalt with brass yellow",
    bg="#0a1424", panel="#12203a", panel_alt="#0f1c32", border="#555d69",
    text="#f2ead6", text_dim="#86898b", text_bright="#f4eedd",
    accent="#ffc94b", ok="#7ee787", warn="#ffc247", crit="#ff4d6d",
    idle="#48505f", selection="#41423d", plot_bg="#060d17",
    series=["#ebbe56", "#cdeb56", "#82eb56", "#56eba5",
            "#56b4eb", "#5b56eb", "#eb56e6", "#eb566a",
            ]))

_add(_p(
    "metropolis", "Metropolis", "dark teal with cream and rust",
    bg="#0c1a1c", panel="#14282b", panel_alt="#112326", border="#56615d",
    text="#f0e6d2", text_dim="#868b82", text_bright="#f3eada",
    accent="#ff8a5c", ok="#7ee787", warn="#ffc247", crit="#ff4d6d",
    idle="#495653", selection="#433c35", plot_bg="#081112",
    series=["#eb8a64", "#ebcd64", "#c4eb64", "#64eb74",
            "#64ebe3", "#6497eb", "#b764eb", "#eb64ae",
            ]))

_add(_p(
    "vaporwave", "Vaporwave", "pastel lavender with cyan and pink",
    bg="#eae4f5", panel="#f4eff9", panel_alt="#f0ebf8", border="#cdc6da",
    text="#2b2140", text_dim="#8b8499", text_bright="#231b34",
    accent="#7b5cff", ok="#00875f", warn="#b8860b", crit="#d70000",
    idle="#c4becd", selection="#dcd2fa", plot_bg="#b7b2bf",
    series=["#392d6b", "#582d6b", "#6b2d5f", "#6b2f2d",
            "#6b622d", "#4b6b2d", "#2d6b4e", "#2d556b",
            ]))

_add(_p(
    "serika", "Serika", "charcoal with a saturated lemon",
    bg="#16161a", panel="#202027", panel_alt="#1c1c22", border="#605f62",
    text="#f5f3ec", text_dim="#8f8e8d", text_bright="#f7f5ef",
    accent="#ffe000", ok="#7ee787", warn="#ffc247", crit="#ff4d6d",
    idle="#535356", selection="#4d461f", plot_bg="#0e0e11",
    series=["#ebd223", "#a0eb23", "#3beb23", "#23ebb2",
            "#237eeb", "#4e23eb", "#eb23c1", "#eb2c23",
            ]))

_add(_p(
    "samurai", "Samurai", "near-black with gold and deep blue",
    bg="#08090c", panel="#101720", panel_alt="#0d1219", border="#515455",
    text="#e8e2d0", text_dim="#80817c", text_bright="#ece7d8",
    accent="#d4a017", ok="#7ee787", warn="#ffc247", crit="#ff4d6d",
    idle="#44484a", selection="#37321e", plot_bg="#050608",
    series=["#ebb72e", "#c1eb2e", "#62eb2e", "#2eeb98",
            "#2ea1eb", "#3a2eeb", "#eb2ee0", "#eb2e43",
            ]))

_add(_p(
    "hyperfuse", "Hyperfuse", "mid grey with purple and cyan",
    bg="#202226", panel="#2a2d31", panel_alt="#26292d", border="#626568",
    text="#e6e8ea", text_dim="#8c8e91", text_bright="#eaecee",
    accent="#7d5bbe", ok="#7ee787", warn="#ffc247", crit="#ff4d6d",
    idle="#575a5d", selection="#3b364d", plot_bg="#151619",
    series=["#a27deb", "#d97deb", "#eb7dc5", "#eb907d",
            "#eaeb7d", "#a0eb7d", "#7debc7", "#7db3eb",
            ]))

_add(_p(
    "8008", "8008", "cool grey with pink and blue",
    bg="#dfe0e4", panel="#eceef1", panel_alt="#e7e9ec", border="#c3c5cc",
    text="#1c1f26", text_dim="#808287", text_bright="#17191f",
    accent="#c8588c", ok="#00875f", warn="#b8860b", crit="#d70000",
    idle="#babcc0", selection="#e5d0dd", plot_bg="#aeafb2",
    series=["#6b354e", "#6b3735", "#6b5235", "#576b35",
            "#356b40", "#356b64", "#353c6b", "#5b356b",
            ]))

_add(_p(
    "cafe", "Cafe", "cream page, espresso ink, caramel accent",
    bg="#efe6d8", panel="#f7f1e6", panel_alt="#f4ede1", border="#d3c8bd",
    text="#2e2318", text_dim="#8e867b", text_bright="#261d14",
    accent="#b06a2c", ok="#00875f", warn="#b8860b", crit="#d70000",
    idle="#c7c0b5", selection="#e9d6c1", plot_bg="#bab3a8",
    series=["#6b4523", "#6b6923", "#496b23", "#236b39",
            "#23616b", "#23316b", "#5d236b", "#6b233d",
            ]))

_add(_p(
    "camping", "Camping", "dark forest with tan and moss",
    bg="#0f1512", panel="#16201b", panel_alt="#141c18", border="#555b52",
    text="#e8e4d4", text_dim="#83867b", text_bright="#ece9dc",
    accent="#7fae5a", ok="#7ee787", warn="#ffc247", crit="#ff4d6d",
    idle="#484f47", selection="#2b3c28", plot_bg="#0a0e0c",
    series=["#b2eb85", "#85eb8b", "#85ebbe", "#85c2eb",
            "#9b85eb", "#e085eb", "#eb8590", "#ebcf85",
            ]))

_add(_p(
    "copper", "Copper", "dark brown-grey with polished copper",
    bg="#17120f", panel="#221a16", panel_alt="#1e1714", border="#5f554e",
    text="#ecdfd2", text_dim="#8b8078", text_bright="#efe5da",
    accent="#c8763f", ok="#7ee787", warn="#ffc247", crit="#ff4d6d",
    idle="#524943", selection="#432c1e", plot_bg="#0f0c0a",
    series=["#eb945a", "#ebdc5a", "#b0eb5a", "#5aeb7c",
            "#5ae1eb", "#5a80eb", "#c45aeb", "#eb5a99",
            ]))

_add(_p(
    "mudbeam", "Mudbeam", "khaki and mud with an olive accent",
    bg="#2a2a22", panel="#34342a", panel_alt="#303027", border="#6a695d",
    text="#e9e5d4", text_dim="#929082", text_bright="#edeadc",
    accent="#8a9a5b", ok="#7ee787", warn="#ffc247", crit="#ff4d6d",
    idle="#5f5e53", selection="#454834", plot_bg="#1b1b16",
    series=["#d5eb94", "#aaeb94", "#94ebaa", "#94e3eb",
            "#949beb", "#c794eb", "#eb94b8", "#ebb894",
            ]))

_add(_p(
    "peach", "Peach Blossom", "peach and cream with terracotta",
    bg="#f3e3dc", panel="#faf0ea", panel_alt="#f8ebe5", border="#d8c8c2",
    text="#3a2620", text_dim="#968781", text_bright="#301f1a",
    accent="#ca6652", ok="#00875f", warn="#b8860b", crit="#d70000",
    idle="#ccc0ba", selection="#f0d4cc", plot_bg="#beb1ac",
    series=["#6b3b32", "#6b5832", "#616b32", "#326b32",
            "#326b61", "#324e6b", "#4e326b", "#6b3258",
            ]))

_add(_p(
    "burgundy", "Burgundy", "deep wine with cream and crimson",
    bg="#17090d", panel="#240f15", panel_alt="#1f0d12", border="#614e50",
    text="#f0e2d8", text_dim="#8e7d7a", text_bright="#f3e7df",
    accent="#b0304a", ok="#7ee787", warn="#ffc247", crit="#ff4d6d",
    idle="#554244", selection="#401620", plot_bg="#0f0608",
    series=["#eb5170", "#eb7e51", "#ebcc51", "#89eb51",
            "#51eb98", "#51d6eb", "#6551eb", "#e551eb",
            ]))

_add(_p(
    "nightrunner", "Night Runner", "midnight navy with neon mint",
    bg="#070b14", panel="#0e1520", panel_alt="#0c121c", border="#4f575d",
    text="#e6f0ea", text_dim="#7e8789", text_bright="#eaf3ee",
    accent="#37e6a0", ok="#7ee787", warn="#ffc247", crit="#ff4d6d",
    idle="#424a50", selection="#163f3a", plot_bg="#05070d",
    series=["#4aebaa", "#4adbeb", "#4a8aeb", "#904aeb",
            "#eb4ac0", "#eb4a54", "#ebe04a", "#70eb4a",
            ]))

_add(_p(
    "dolch", "Dolch", "warm mid grey with a teal accent",
    bg="#26241f", panel="#312e28", panel_alt="#2d2a25", border="#69655e",
    text="#ebe6dc", text_dim="#928e86", text_bright="#efeae2",
    accent="#3fa7a0", ok="#7ee787", warn="#ffc247", crit="#ff4d6d",
    idle="#5e5a53", selection="#344640", plot_bg="#191714",
    series=["#67ebe2", "#67b2eb", "#6770eb", "#cc67eb",
            "#eb679c", "#eb8a67", "#c8eb67", "#67eb74",
            ]))

_add(_p(
    "wob", "WoB", "white on black, nothing else",
    bg="#000000", panel="#0f0f0f", panel_alt="#0b0b0b", border="#5a5a5a",
    text="#ffffff", text_dim="#9a9a9a", text_bright="#ffffff",
    accent="#ffffff", ok="#cfcfcf", warn="#a8a8a8", crit="#efefef",
    idle="#3f3f3f", selection="#2a2a2a", plot_bg="#050505",
    series=["#383838", "#4a4a4a", "#5c5c5c", "#707070",
            "#8a8a8a", "#a3a3a3", "#c4c4c4", "#e6e6e6",
            ]))

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
