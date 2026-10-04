#!/usr/bin/env python3
"""Step 1 -- is the data still there?

Samples the device at evenly spaced points and reports how much real data is present
and whether it has the structure of compressed media. Read-only.

    sudo python3 probe.py /dev/rdiskN [-o OUTDIR] [--points 400] [--size 1]

The point of running this first: the most expensive mistake in recovery is spending
hours searching for data that no longer exists. Three outcomes matter --

  * a large contiguous band of structured data  -> quick format, proceed
  * almost everything reads 0x00 / 0xFF         -> erased or TRIM'd, stop
  * high entropy but no structure               -> random overwrite, stop
"""
from __future__ import annotations

import argparse
import collections
import itertools
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sdr_lib import aligned_read, device_size  # noqa: E402

SIGNATURES = [
    (b"\x00\x00\x00", "triple zero run", 3),
    (b"\x00\x00\x00\x01", "Annex-B start code", 4),
    (b"ftyp", "MP4 container header", 4),
    (b"moov", "MP4 index", 4),
    (b"mdat", "MP4 payload", 4),
    (b"\xff\xd8\xff", "JPEG start", 3),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("device", help="raw device or image, e.g. /dev/rdiskN")
    ap.add_argument("-o", "--outdir", default=".", help="where to write probe.json")
    ap.add_argument("--points", type=int, default=400, help="sample points (default 400)")
    ap.add_argument("--size", type=int, default=1, help="MB per sample point (default 1)")
    args = ap.parse_args()

    total = device_size(args.device)
    if not total:
        print(f"Could not determine size of {args.device}", file=sys.stderr)
        return 1

    chunk = args.size * 1024 * 1024
    step = total // args.points
    print(f"Device    {args.device}")
    print(f"Size      {total:,} bytes ({total / 1024**3:.1f} GB)")
    print(f"Sampling  {args.points} points x {args.size} MB "
          f"({args.points * chunk / 1024**2:.0f} MB total)\n")

    fd = os.open(args.device, os.O_RDONLY)
    try:
        parts = []
        for i in range(args.points):
            parts.append(aligned_read(fd, i * step, chunk))
            if i and i % 50 == 0:
                print(f"  ... {i}/{args.points}", flush=True)
    finally:
        os.close(fd)

    data = b"".join(parts)
    n = len(data)

    # --- how much is real data, and where
    flags = []
    for i in range(args.points):
        c = parts[i]
        blank = c.count(0) + c.count(0xFF)
        flags.append("#" if blank < len(c) * 0.5 else ".")
    density = "".join(flags)

    regions = []
    pos = 0
    for key, group in itertools.groupby(density):
        count = len(list(group))
        regions.append({
            "start_gb": round(pos * step / 1e9, 2),
            "end_gb": round((pos + count) * step / 1e9, 2),
            "kind": "data" if key == "#" else "blank",
            "fraction": round(count / args.points, 4),
        })
        pos += count

    data_fraction = density.count("#") / args.points
    estimated_bytes = int(total * data_fraction)

    # --- does it have the structure of compressed media
    #
    # Measure only the non-blank sample points. Mixing the blank regions in swamps
    # every statistic: a card that is 97% zeros yields a "triple zero" ratio in the
    # millions and an entropy median of zero, neither of which says anything about
    # whether the occupied region holds real media.
    dense = b"".join(parts[i] for i in range(args.points) if flags[i] == "#")
    sample = dense if dense else data
    m = len(sample)

    counts = collections.Counter(sample)
    zero_pct = counts.get(0, 0) * 100 / m
    distinct = len(counts)

    structure = {}
    for pattern, label, width in SIGNATURES:
        expected = m / (256 ** width)
        found = sample.count(pattern)
        structure[label] = {
            "found": found,
            "expected_if_random": round(expected, 4),
            "ratio": round(found / expected, 1) if expected else None,
        }

    entropies = []
    for i in range(0, m - 4096, 4096):
        block = sample[i:i + 4096]
        c = collections.Counter(block)
        entropies.append(-sum((v / 4096) * math.log2(v / 4096) for v in c.values()))
    entropies.sort()
    median_entropy = entropies[len(entropies) // 2] if entropies else 0.0

    # --- verdict
    #
    # Judge the occupied region, not the card's fill level. A lightly used card is
    # still fully recoverable, so an absolute floor on data volume matters more than
    # a percentage -- and finding container headers settles it outright.
    triple_zero = structure["triple zero run"]["ratio"] or 0
    # Only the 4-byte ASCII box types count as evidence. The 3-byte JPEG signature
    # hits by chance often enough inside video payload that treating it as proof of
    # a surviving container would make the verdict unreliable.
    has_headers = any(structure[k]["found"] > 0
                      for k in ("MP4 container header", "MP4 index", "MP4 payload"))
    # The two possible errors are not symmetric. Calling a recoverable card erased
    # makes someone abandon data that was still there; calling an erased card
    # recoverable costs one cheap scan. So the floor here is deliberately low, and
    # even the ERASED verdict recommends running scan.py before concluding anything.
    MIN_DATA = 1024 * 1024

    if has_headers:
        verdict = "RECOVERABLE"
        advice = "Container headers found in the occupied region. Run scan.py next."
    elif estimated_bytes < MIN_DATA:
        verdict = "ERASED"
        advice = ("Almost nothing but 0x00/0xFF -- consistent with a full format, "
                  "secure erase or TRIM. Probe sampling can miss a lightly used card, "
                  "so run scan.py anyway before telling anyone the data is gone: it is "
                  "cheap, and it looks at every sector rather than a sample.")
    elif triple_zero < 10 and median_entropy > 7.99:
        verdict = "OVERWRITTEN"
        advice = ("High entropy with no structure in the occupied region -- random or "
                  "encrypted fill, not media. Not recoverable.")
    else:
        verdict = "RECOVERABLE"
        advice = ("Structured media data present. Run scan.py next, then check that "
                  "the declared mdat total roughly matches the estimate below -- if "
                  "they agree, the data is on the card and any search that finds "
                  "nothing is a flaw in the search.")

    if dense and len(dense) < n:
        advice += (f"\n  (Structure measured on the {len(dense) / 1024**2:.0f} MB of "
                   f"occupied samples only, not the blank regions.)")

    print("\n=== Density map (each character = "
          f"{step / 1e9:.2f} GB; # = data, . = blank) ===")
    for row in range(0, args.points, 80):
        print(f"{row / args.points * 100:5.1f}%  {density[row:row + 80]}")

    print("\n=== Regions ===")
    for r in regions:
        print(f"  {r['start_gb']:7.2f} - {r['end_gb']:7.2f} GB  {r['kind']:<6} "
              f"({r['fraction'] * 100:.1f}%)")

    print("\n=== Structure ===")
    for label, s in structure.items():
        ratio = f"{s['ratio']:>10,.1f}x" if s["ratio"] else "         -"
        print(f"  {label:<22} found {s['found']:>8,}  vs random {ratio}")
    print(f"  {'4KB entropy median':<22} {median_entropy:.3f} bits (8.000 = random)")
    print(f"  {'zero byte share':<22} {zero_pct:.2f}%   distinct values {distinct}/256")

    print(f"\n=== Verdict: {verdict} ===")
    if estimated_bytes >= 1024**3:
        print(f"  Estimated real data on device: {estimated_bytes / 1024**3:.2f} GB")
    else:
        print(f"  Estimated real data on device: {estimated_bytes / 1024**2:.0f} MB")
    print(f"  {advice}")

    os.makedirs(args.outdir, exist_ok=True)
    out = os.path.join(args.outdir, "probe.json")
    with open(out, "w") as fh:
        json.dump({
            "device": args.device,
            "device_size": total,
            "sample_points": args.points,
            "sample_bytes": n,
            "data_fraction": round(data_fraction, 4),
            "estimated_data_bytes": estimated_bytes,
            "density_map": density,
            "regions": regions,
            "structure": structure,
            "median_entropy_4k": round(median_entropy, 4),
            "zero_percent": round(zero_pct, 3),
            "verdict": verdict,
        }, fh, indent=1)
    print(f"\nWrote {out}")
    return 0 if verdict == "RECOVERABLE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
