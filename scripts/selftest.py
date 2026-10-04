#!/usr/bin/env python3
"""Self-test -- validate the toolchain without a damaged card.

    python3 selftest.py [--keep]

Builds a synthetic MP4 with ffmpeg, scatters it across non-contiguous clusters inside
a fake card image surrounded by blank space, then runs the full pipeline and checks
that the rebuilt file is byte-for-byte identical to the original.

Worth running after any change to the parsing or chasing code. The byte comparison is
the point: a rebuilt file can play correctly in ffmpeg while still being structurally
wrong, because stco offsets are absolute and ffmpeg locates moov independently. Only
an exact comparison catches that class of defect.

Requires ffmpeg on PATH.
"""
from __future__ import annotations

import argparse
import os
import random
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
CL = 128 * 1024
IMAGE_MB = 600


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)


def build_source(work: str) -> str:
    """A short clip with both video and audio, moov at the end as cameras write it."""
    staged = os.path.join(work, "staged.mp4")
    src = os.path.join(work, "src.mp4")
    run(["ffmpeg", "-v", "error",
         "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=12",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=12",
         "-c:v", "libx264", "-preset", "ultrafast", "-b:v", "6M",
         "-c:a", "aac", "-b:a", "128k", "-y", staged])
    run(["ffmpeg", "-v", "error", "-i", staged, "-c", "copy", "-y", src])
    return src


def moov_offset(path: str) -> int:
    size = os.path.getsize(path)
    off = 0
    with open(path, "rb") as fh:
        while off < size:
            fh.seek(off)
            head = fh.read(16)
            if len(head) < 8:
                break
            box = int.from_bytes(head[0:4], "big")
            if box == 1:
                box = int.from_bytes(head[8:16], "big")
            if head[4:8] == b"moov":
                return off
            if box < 8:
                break
            off += box
    raise SystemExit("test clip has no moov -- ffmpeg produced an unexpected layout")


def scatter(src: str, image: str, seed: int = 7) -> int:
    """Place the file body across non-contiguous clusters, moov far away."""
    data = open(src, "rb").read()
    split = moov_offset(src)
    body, moov = data[:split], data[split:]
    n_clusters = (len(body) + CL - 1) // CL

    rng = random.Random(seed)
    total = IMAGE_MB * 1024 * 1024
    img = bytearray(total)

    placement, cur, i = [], rng.randrange(40, 80), 0
    while i < n_clusters:
        run_len = rng.choice([4, 8, 16, 16, 32])
        for _ in range(min(run_len, n_clusters - i)):
            placement.append(cur)
            cur += 1
            i += 1
        cur += rng.choice([2, 3, 5, 11, 17])          # gap given to "another file"

    for k, c in enumerate(placement):
        chunk = body[k * CL:(k + 1) * CL]
        img[c * CL:c * CL + len(chunk)] = chunk

    moov_cluster = cur + 200
    img[moov_cluster * CL:moov_cluster * CL + len(moov)] = moov

    # A little leftover noise, so the blank regions are not uniformly zero.
    for _ in range(60):
        c = rng.randrange(total // CL)
        if any(img[c * CL:(c + 1) * CL]):
            continue
        img[c * CL:(c + 1) * CL] = os.urandom(CL)

    open(image, "wb").write(img)
    return len(placement)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--keep", action="store_true", help="keep the working directory")
    args = ap.parse_args()

    if not shutil.which("ffmpeg"):
        print("ffmpeg not found on PATH", file=sys.stderr)
        return 1

    work = tempfile.mkdtemp(prefix="sdr-selftest-")
    try:
        print(f"Working in {work}")
        src = build_source(work)
        image = os.path.join(work, "card.img")
        n = scatter(src, image)
        print(f"Source {os.path.getsize(src):,} bytes, scattered across {n} "
              f"non-contiguous clusters in a {IMAGE_MB} MB image\n")

        out = os.path.join(work, "out")
        os.makedirs(out, exist_ok=True)
        for step in (
            [sys.executable, os.path.join(HERE, "probe.py"), image, "-o", work,
             "--points", "200"],
            [sys.executable, os.path.join(HERE, "scan.py"), image, "-o", work],
            [sys.executable, os.path.join(HERE, "rebuild.py"), image, "-o", out,
             "--scan", os.path.join(work, "scan.json")],
            [sys.executable, os.path.join(HERE, "verify.py"), out],
        ):
            name = os.path.basename(step[1])
            proc = subprocess.run(step, capture_output=True, text=True)
            status = "ok" if proc.returncode in (0, 2) else f"exit {proc.returncode}"
            print(f"  {name:<12} {status}")
            if proc.returncode not in (0, 2):
                print(proc.stdout[-2000:])
                print(proc.stderr[-2000:], file=sys.stderr)
                return 1

        rebuilt = [f for f in os.listdir(out) if f.endswith(".mp4")]
        if not rebuilt:
            print("\nFAIL: nothing was rebuilt")
            return 1
        got = open(os.path.join(out, rebuilt[0]), "rb").read()
        want = open(src, "rb").read()

        print()
        if got == want:
            print(f"PASS  rebuilt {len(got):,} bytes, byte-for-byte identical")
            return 0

        if len(got) != len(want):
            print(f"FAIL  size {len(got):,} vs expected {len(want):,}")
        else:
            diff = [i for i in range(len(want)) if want[i] != got[i]]
            print(f"FAIL  {len(diff)} bytes differ, first at offset {diff[0]}")
            print("      Offsets just past a box header usually mean a size field was"
                  " written to the wrong box.")
        return 1
    finally:
        if args.keep:
            print(f"\nKept {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
