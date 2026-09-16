"""Work out how much VRAM a GGUF's context window costs, from the file itself.

`--vram-check` measures what a model actually uses; this explains where it went.
The number people guess wrong is the KV cache, so the geometry is read from the
GGUF metadata rather than from the model card, and the blocks that own a KV
cache are counted from the tensor names.

That distinction matters for hybrid models. Qwen3.5/3.8-style files interleave
three linear-attention blocks (a fixed-size recurrent state, no cache) with one
full-attention block (a cache that grows with the context). Assuming a cache on
every block overstates this model by about 4x.

    python ggufinfo.py MODEL.gguf
    python ggufinfo.py MODEL.gguf --ctx 131072
"""
from __future__ import annotations

import os
import struct
import sys

# GGUF metadata value types (the subset that appears in practice).
_FMT = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f", 7: "<?",
        10: "<Q", 11: "<q", 12: "<d"}
_SIZE = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}

GiB = 1024 ** 3
MiB = 1024 ** 2


def _read(f, kind: int):
    if kind == 8:                                   # string
        n = struct.unpack("<Q", f.read(8))[0]
        return f.read(n).decode("utf-8", "replace")
    if kind == 9:                                   # array
        element = struct.unpack("<I", f.read(4))[0]
        count = struct.unpack("<Q", f.read(8))[0]
        if element == 8:
            return [_read(f, 8) for _ in range(count)]
        return [struct.unpack(_FMT[element], f.read(_SIZE[element]))[0]
                for _ in range(count)]
    return struct.unpack(_FMT[kind], f.read(_SIZE[kind]))[0]


def read_gguf(path: str) -> tuple[dict, list[str]]:
    """Return (metadata, tensor names). Only the header is read."""
    with open(path, "rb") as f:
        magic, version, ntensors, nkv = struct.unpack("<IIQQ", f.read(24))
        if magic != 0x46554747:                     # 'GGUF'
            raise ValueError(f"{path} is not a GGUF file")
        if version not in (2, 3):
            raise ValueError(f"unsupported GGUF version {version}")
        meta: dict = {}
        for _ in range(nkv):
            key = _read(f, 8)
            meta[key] = _read(f, struct.unpack("<I", f.read(4))[0])
        names = []
        for _ in range(ntensors):
            name = _read(f, 8)
            ndims = struct.unpack("<I", f.read(4))[0]
            f.read(8 * ndims)                       # dimensions
            f.read(4)                               # quantisation type
            f.read(8)                               # data offset
            names.append(name)
    return meta, names


def _int(meta: dict, *keys, default=None):
    for key in keys:
        if key in meta:
            return int(meta[key])
    return default


def _str(meta: dict, *keys, default=""):
    for key in keys:
        if key in meta:
            return str(meta[key])
    return default


def report(path: str, ctx: int | None = None,
           cache_bytes: int = 2) -> int:
    meta, names = read_gguf(path)
    arch = _str(meta, "general.architecture")
    prefix = f"{arch}."
    blocks = _int(meta, prefix + "block_count", default=0)
    kv_heads = _int(meta, prefix + "attention.head_count_kv",
                    prefix + "attention.head_count", default=0)
    key_len = _int(meta, prefix + "attention.key_length", default=0)
    val_len = _int(meta, prefix + "attention.value_length", default=0)
    native_ctx = _int(meta, prefix + "context_length", default=0)

    # Which blocks have a KV cache, read from the tensors rather than assumed.
    cache_blocks = sorted({int(n.split(".")[1]) for n in names
                           if n.startswith("blk.") and ".attn_k." in n})
    state_blocks = sorted({int(n.split(".")[1]) for n in names
                           if n.startswith("blk.") and ".ssm_" in n})

    print(f"file        : {os.path.basename(path)}")
    print(f"architecture: {arch}")
    print(f"file size   : {os.path.getsize(path):,} bytes "
          f"({os.path.getsize(path) / GiB:.2f} GiB)")
    print(f"blocks      : {blocks} "
          f"({len(cache_blocks)} with a KV cache, "
          f"{len(state_blocks)} with a recurrent state)")
    print(f"KV geometry : {kv_heads} heads x {key_len} key / {val_len} value dim")
    if native_ctx:
        print(f"trained ctx : {native_ctx:,} tokens")
    if not cache_blocks:
        print("\nNo attn_k tensors found: cannot estimate a KV cache.")
        return 1

    per_token_layer = (kv_heads * key_len + kv_heads * val_len) * cache_bytes
    if not per_token_layer:
        print("\nHead counts or dimensions are missing from the metadata.")
        return 1
    per_layer = per_token_layer * native_ctx if native_ctx else 0

    print(f"\nper token per attention block: {per_token_layer} bytes")
    print(f"per attention block at {native_ctx:,} tokens: "
          f"{per_layer / GiB:.2f} GiB")
    print(f"KV cache at the full {native_ctx:,} tokens: "
          f"{per_layer * len(cache_blocks) / GiB:.2f} GiB "
          f"for all {len(cache_blocks)} blocks")

    print("\ncontext        KV cache      weights + KV")
    weights = os.path.getsize(path)
    for try_ctx in (32768, 65536, 131072, native_ctx or 262144):
        if try_ctx > (native_ctx or try_ctx):
            continue
        kv = per_token_layer * len(cache_blocks) * try_ctx
        print(f"{try_ctx:>8,}  {kv / GiB:>10.2f} GiB  "
              f"{(weights + kv) / GiB:>10.2f} GiB")

    if ctx:
        kv = per_token_layer * len(cache_blocks) * ctx
        print(f"\nat the requested {ctx:,} tokens: KV {kv / GiB:.2f} GiB, "
              f"model + KV {(weights + kv) / GiB:.2f} GiB "
              f"({(weights + kv) / 1e9:.2f} GB)")
    print("\nK and V counted at "
          f"{cache_bytes} bytes per value (f16). A quantised cache "
          "(q8_0, q4_0) multiplies the KV column by 0.5 or 0.25.\n"
          "Compute buffers, the vision tower and the driver add a few GiB on "
          "top; compare with what `python gpumon.py --vram-check` measures.")
    return 0


def main(argv: list[str]) -> int:
    args = list(argv)
    ctx = None
    if "--ctx" in args:
        i = args.index("--ctx")
        try:
            ctx = int(args[i + 1])
        except (IndexError, ValueError):
            print("--ctx needs a token count, e.g. --ctx 131072", file=sys.stderr)
            return 2
        del args[i:i + 2]
    if not args:
        print(__doc__.strip().splitlines()[0])
        print("\nusage: python ggufinfo.py MODEL.gguf [--ctx TOKENS]",
              file=sys.stderr)
        return 2
    try:
        return report(args[0], ctx)
    except (OSError, ValueError) as exc:
        print(f"ggufinfo: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
