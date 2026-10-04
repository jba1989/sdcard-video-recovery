#!/usr/bin/env python3
"""Step 4 -- QA the rebuilt files.

    python3 verify.py OUTDIR [--quick]

Checks three things that are easy to get wrong and easy to miss:

  * Box structure walks cleanly from offset 0 to EOF. A wrong mdat size field still
    plays in ffmpeg -- stco offsets are absolute and moov is found independently --
    so this defect survives casual testing and surfaces in other players.

  * Video and audio decode separately. Checking only the video track hides a whole
    class of defect; the usual symptom is a clean video track over an audio track
    missing its last few seconds.

  * Declared duration matches what the container actually reports.

No source device needed -- this runs against the output directory alone.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys


def walk_boxes(path: str):
    """Return (chain, ends_exactly_at_eof)."""
    size = os.path.getsize(path)
    chain, off = [], 0
    with open(path, "rb") as fh:
        while off < size:
            fh.seek(off)
            head = fh.read(16)
            if len(head) < 8:
                break
            box = int.from_bytes(head[0:4], "big")
            btype = head[4:8].decode("latin1", "replace")
            if box == 1:
                box = int.from_bytes(head[8:16], "big")
            chain.append(btype)
            if box < 8:
                break
            off += box
    return chain, off == size


def ffprobe(path: str, entries: str):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", entries, "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=120)
        return out.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""


def decode_errors(path: str, stream: str) -> int:
    try:
        out = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", path, "-map", stream, "-f", "null", "-"],
            capture_output=True, text=True, timeout=3600)
        return len(out.stderr.strip().splitlines()) if out.stderr.strip() else 0
    except FileNotFoundError:
        return -1
    except subprocess.TimeoutExpired:
        return -2


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("outdir")
    ap.add_argument("--quick", action="store_true",
                    help="structure and metadata only; skip full decode")
    args = ap.parse_args()

    if not os.path.isdir(args.outdir):
        print(f"{args.outdir} is not a directory", file=sys.stderr)
        return 1

    report_path = os.path.join(args.outdir, "rebuild.json")
    declared = {}
    if os.path.exists(report_path):
        for r in json.load(open(report_path)):
            declared[r["file"]] = r

    files = sorted(f for f in os.listdir(args.outdir) if f.lower().endswith((".mp4", ".mov")))
    if not files:
        print("No .mp4/.mov files found.")
        return 1

    print(f"{'file':<42}{'duration':>11}{'structure':>12}{'video':>8}{'audio':>8}")
    rows = []
    for name in files:
        path = os.path.join(args.outdir, name)
        chain, exact = walk_boxes(path)
        structure = "+".join(chain) if exact else "BROKEN"
        duration = ffprobe(path, "format=duration") or "-"
        if args.quick:
            v = a = None
        else:
            v = decode_errors(path, "0:v")
            a = decode_errors(path, "0:a")
        print(f"{name[:40]:<42}{duration:>11}"
              f"{('ok' if exact else 'BROKEN'):>12}"
              f"{(v if v is not None else '-'):>8}{(a if a is not None else '-'):>8}",
              flush=True)
        rows.append({"file": name, "duration": duration, "structure_ok": exact,
                     "box_chain": chain, "video_errors": v, "audio_errors": a,
                     "declared": declared.get(name, {})})

    print("\n=== Summary ===")
    broken = [r for r in rows if not r["structure_ok"]]
    print(f"  files                 {len(rows)}")
    print(f"  structure broken      {len(broken)}")
    if broken:
        print("    -> check the mdat size field offset; with ftyp+wide+mdat it is at 36")
    if not args.quick:
        clean = [r for r in rows if r["video_errors"] == 0 and r["audio_errors"] == 0]
        vclean = [r for r in rows if r["video_errors"] == 0]
        print(f"  video decode clean    {len(vclean)}")
        print(f"  video + audio clean   {len(clean)}")
        audio_only = [r for r in rows if r["video_errors"] == 0 and (r["audio_errors"] or 0) > 0]
        if audio_only:
            print(f"  video clean, audio glitchy: {len(audio_only)}")
            print("    -> usually the output was truncated at the last video sample, or")
            print("       clusters with no video NAL boundary were resolved by assumption")

        total_dur = sum(float(r["duration"]) for r in rows
                        if r["duration"] not in ("-", ""))
        print(f"  total duration        {total_dur / 60:.1f} min")

    if declared:
        mismatched = []
        for r in rows:
            d = r["declared"].get("declared_duration_s")
            if d and r["duration"] not in ("-", ""):
                if abs(float(r["duration"]) - d) > 0.5:
                    mismatched.append((r["file"], d, float(r["duration"])))
        if mismatched:
            print(f"\n  duration mismatch vs index: {len(mismatched)}")
            for name, want, got in mismatched[:10]:
                print(f"    {name:<40} index {want:.1f}s  actual {got:.1f}s")

    out = os.path.join(args.outdir, "verify.json")
    with open(out, "w") as fh:
        json.dump(rows, fh, indent=1)
    print(f"\nWrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
