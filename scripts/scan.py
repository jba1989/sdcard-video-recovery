#!/usr/bin/env python3
"""Step 2 -- find the container headers and pair them with their indexes.

    sudo python3 scan.py /dev/rdiskN [-o OUTDIR] [--limit-gb 0]

Sweeps the device for ftyp / moov / JPEG signatures, validates each candidate, pairs
every file start with its index, and writes a catalogue of what was on the card --
creation time, duration, resolution, codec -- before any rebuilding happens.

Validation carries most of the weight here. A four-byte signature matches by chance
roughly once per 4 GB, so on a 50 GB card a dozen false 'moov' hits are expected.
Requiring the first child box to be 'mvhd' removes them; requiring 'Exif' at offset 6
does the same for JPEG.
"""
from __future__ import annotations

import argparse
import json
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sdr_lib import (  # noqa: E402
    aligned_read, device_size, boxes, find_path, parse_mvhd, track_samples,
    detect_cluster_size, valid_box_type,
)

CHUNK = 256 * 1024 * 1024
OVERLAP = 64
PATTERNS = [b"ftyp", b"moov", b"\xff\xd8\xff\xe1", b"\xff\xd8\xff\xe0"]


def sweep(fd: int, end: int) -> dict[bytes, list[int]]:
    """Collect raw signature offsets. Chunks overlap so matches on a boundary survive."""
    hits: dict[bytes, list[int]] = {p: [] for p in PATTERNS}
    pos, tail = 0, b""
    while pos < end:
        os.lseek(fd, pos, os.SEEK_SET)
        buf = bytearray()
        while len(buf) < CHUNK:
            part = os.read(fd, min(64 << 20, CHUNK - len(buf)))
            if not part:
                break
            buf += part
        if not buf:
            break
        blob = tail + bytes(buf)
        base = pos - len(tail)
        for pattern in PATTERNS:
            i = blob.find(pattern)
            while i != -1:
                hits[pattern].append(base + i)
                i = blob.find(pattern, i + 1)
        tail = blob[-OVERLAP:]
        pos += len(buf)
        if pos % (8 << 30) < CHUNK:
            print(f"  ... {pos / 1024**3:.0f} GB scanned", flush=True)
    return hits


def validate_ftyp(fd: int, off: int):
    """A real ftyp has a plausible size and is followed by wide/free/mdat."""
    start = off - 4
    if start < 0:
        return None
    head = aligned_read(fd, start, 64)
    size = int.from_bytes(head[0:4], "big")
    if head[4:8] != b"ftyp" or not (8 <= size <= 128):
        return None
    nxt = head[size:size + 8] if size + 8 <= 64 else aligned_read(fd, start + size, 8)
    ntype, nsize = nxt[4:8], int.from_bytes(nxt[0:4], "big")
    if ntype == b"mdat":
        mdat_off, mdat_size = start + size, nsize
    elif ntype in (b"wide", b"free") and 8 <= nsize <= 64:
        probe = aligned_read(fd, start + size + nsize, 16)
        if probe[4:8] != b"mdat":
            return None
        mdat_size = int.from_bytes(probe[0:4], "big")
        if mdat_size == 1:
            mdat_size = int.from_bytes(probe[8:16], "big")
        mdat_off = start + size + nsize
    else:
        return None
    if mdat_size < 1024:
        return None
    return {
        "start": start,
        "brand": head[8:12].decode("latin1", "replace"),
        "ftyp_size": size,
        "next_box": ntype.decode("latin1", "replace"),
        "mdat_off": mdat_off,
        "mdat_size": mdat_size,
        "header_bytes": mdat_off - start,
    }


def validate_moov(fd: int, off: int):
    """A real moov's first child box is mvhd."""
    start = off - 4
    if start < 0:
        return None
    head = aligned_read(fd, start, 32)
    size = int.from_bytes(head[0:4], "big")
    if head[4:8] != b"moov" or head[12:16] != b"mvhd":
        return None
    if not (100 < size < 64 * 1024 * 1024):
        return None
    return {"start": start, "size": size}


def describe(moov: bytes):
    """Pull the user-facing catalogue fields out of an index."""
    created, duration = parse_mvhd(moov)
    width = height = 0
    codecs = []
    for ts, te in find_path(moov, 0, len(moov), [b"moov", b"trak"]):
        for s, _e in find_path(moov, ts, te, [b"tkhd"]):
            # tkhd payload: version+flags(4), times/id/duration(20 v0 | 32 v1),
            # reserved(8), layer+group(4), volume+reserved(4), matrix(36), then
            # width and height as 16.16 fixed point.
            base = s + (88 if moov[s] else 76)
            w = struct.unpack(">I", moov[base:base + 4])[0] >> 16
            h = struct.unpack(">I", moov[base + 4:base + 8])[0] >> 16
            if w > width:
                width, height = w, h
        for s, e in find_path(moov, ts, te, [b"mdia", b"minf", b"stbl", b"stsd"]):
            if e - s > 16 and valid_box_type(moov[s + 12:s + 16]):
                codecs.append(moov[s + 12:s + 16].decode("latin1", "replace"))
    return {
        "created": created.isoformat(sep=" ") if created else None,
        "duration_s": round(duration, 3),
        "width": width,
        "height": height,
        "codecs": codecs,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("device")
    ap.add_argument("-o", "--outdir", default=".")
    ap.add_argument("--limit-gb", type=float, default=0,
                    help="stop after N GB (0 = whole device; use probe.json regions)")
    args = ap.parse_args()

    total = device_size(args.device)
    end = int(args.limit_gb * 1024**3) if args.limit_gb else total
    fd = os.open(args.device, os.O_RDONLY)
    try:
        print(f"Sweeping {end / 1024**3:.1f} GB for container signatures...")
        raw = sweep(fd, end)
        labels = []
        for pattern, offsets in raw.items():
            name = "JPEG" if pattern[0] == 0xFF else pattern.decode("latin1")
            labels.append(f"{name}={len(offsets)}")
        print("\nRaw hits: " + ", ".join(labels))

        print("\nValidating...")
        ftyps = [r for r in (validate_ftyp(fd, o) for o in sorted(raw[b"ftyp"])) if r]
        moovs = [r for r in (validate_moov(fd, o) for o in sorted(raw[b"moov"])) if r]
        jpegs = sorted(set(raw[b"\xff\xd8\xff\xe1"]) | set(raw[b"\xff\xd8\xff\xe0"]))
        real_jpegs = []
        for off in jpegs:
            head = aligned_read(fd, off, 16)
            if head[6:10] == b"Exif" or head[6:11] == b"JFIF\x00":
                real_jpegs.append(off)

        print(f"  ftyp  {len(raw[b'ftyp']):>5} raw -> {len(ftyps):>4} valid")
        print(f"  moov  {len(raw[b'moov']):>5} raw -> {len(moovs):>4} valid "
              f"(first child must be mvhd)")
        print(f"  JPEG  {len(jpegs):>5} raw -> {len(real_jpegs):>4} valid "
              f"(Exif/JFIF at offset 6)")

        cluster, how = detect_cluster_size(fd, [f["start"] for f in ftyps], total)
        print(f"\nCluster size {cluster:,} bytes -- {how}")

        # Pair on mdat_size - last_stco_offset == final sample size (small, positive).
        print("\nPairing indexes with file starts...")
        catalogue = []
        used = set()
        for m in moovs:
            moov = aligned_read(fd, m["start"], m["size"])
            vid = track_samples(moov, b"vide")
            aud = track_samples(moov, b"soun")
            last = max([o + s for o, s in vid + aud], default=0)
            last_off = max([o for o, _ in vid + aud], default=0)
            match = None
            for f in ftyps:
                if f["start"] in used:
                    continue
                delta = f["mdat_size"] - last_off
                if 0 < delta < 4096:
                    match = f
                    break
            if match:
                used.add(match["start"])
            info = describe(moov)
            catalogue.append({
                "moov_off": m["start"],
                "moov_size": m["size"],
                "file_start": match["start"] if match else None,
                "mdat_off": match["mdat_off"] if match else None,
                "mdat_size": match["mdat_size"] if match else None,
                "header_bytes": match["header_bytes"] if match else None,
                "brand": match["brand"] if match else None,
                "video_samples": len(vid),
                "audio_samples": len(aud),
                "max_sample_end": last,
                **info,
            })
    finally:
        os.close(fd)

    paired = [c for c in catalogue if c["file_start"] is not None]
    declared = sum(c["mdat_size"] for c in paired)
    print(f"  paired {len(paired)} / {len(catalogue)}")
    print(f"\nDeclared mdat total: {declared / 1024**3:.2f} GB")
    probe_path = os.path.join(args.outdir, "probe.json")
    if os.path.exists(probe_path):
        est = json.load(open(probe_path)).get("estimated_data_bytes", 0)
        ratio = declared / est if est else 0
        print(f"Probe estimated:     {est / 1024**3:.2f} GB   (ratio {ratio:.2f})")
        if 0.7 <= ratio <= 1.4:
            print("  -> Totals reconcile. The data is on the card; a search that finds\n"
                  "     nothing is a flaw in the search, not evidence of loss.")
        elif ratio > 1.4:
            print("  -> Declared exceeds measured: some files were partly overwritten.\n"
                  "     Expect partial recovery.")

    catalogue.sort(key=lambda c: (c["created"] or "", -(c["mdat_size"] or 0)))
    print("\n=== Catalogue ===")
    print(f"{'created':<20}{'dur':>9}{'size':>11}  {'res':>11}  codecs")
    for c in catalogue:
        res = f"{c['width']}x{c['height']}" if c["width"] else "-"
        size = f"{c['mdat_size'] / 1024**2:.0f} MB" if c["mdat_size"] else "-"
        print(f"{(c['created'] or '-'):<20}{c['duration_s']:>8.1f}s{size:>11}  "
              f"{res:>11}  {','.join(c['codecs'])}")

    os.makedirs(args.outdir, exist_ok=True)
    out = os.path.join(args.outdir, "scan.json")
    with open(out, "w") as fh:
        json.dump({
            "device": args.device,
            "cluster_size": cluster,
            "cluster_source": how,
            "declared_mdat_total": declared,
            "files": catalogue,
            "jpeg_offsets": real_jpegs,
        }, fh, indent=1)
    print(f"\nWrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
