#!/usr/bin/env python3
"""Step 3 -- chase fragments cluster by cluster and write the rebuilt files.

    sudo python3 rebuild.py /dev/rdiskN -o OUTDIR [--radius 4096] [--only INDEX]

For each paired file, walks the sample structure and confirms the NAL chain lands
exactly on the boundaries stsz declares. When the walk runs past the resolved cluster
map, candidates for the next cluster are tried -- sequential first, then gap sizes
already seen in this file, then a widening ring -- and the one that validates wins.

Two rules this implementation exists to enforce, both learned the hard way:

  * The verification pattern must fit inside one cluster. If it spans more, and the
    file is fragmented at cluster granularity, nothing can ever match and the search
    returns zero hits everywhere -- indistinguishable from "the data is gone".

  * The output length is the maximum sample end across ALL tracks, not the video
    track's. A NAL walk reads length prefixes and skips payload, so it can finish
    while the last NAL's bytes are still unmapped; one more cluster is resolved
    before writing. Skipping this silently drops the tail of the audio track.

Resolved cluster maps are saved to rebuild.json. Clusters claimed by a finished file
cannot belong to another, and later files are the hard ones -- keeping the maps makes
that pruning available when it matters.
"""
from __future__ import annotations

import argparse
import json
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sdr_lib import (  # noqa: E402
    ClusterCache, aligned_read, track_samples, nal_length_size, walk_nals,
)


def make_reader(cache: ClusterCache, cmap: list[int]):
    """Read file-relative bytes through the resolved cluster map."""
    cl = cache.cl

    def read(off: int, n: int):
        out = bytearray()
        while n > 0:
            k, r = divmod(off, cl)
            if k >= len(cmap):
                return None
            block = cache.cluster(cmap[k])
            take = min(n, cl - r)
            out += block[r:r + take]
            off += take
            n -= take
        return bytes(out)
    return read


def advance(cache, cmap, samples, state, lensize, watch):
    """Continue the NAL walk. Returns (state, needed_cluster, checks_in_watch, ok).

    `checks_in_watch` counts length fields read inside the cluster under test. A
    candidate that the walk merely skips over would pass trivially, so acceptance
    requires at least one real check landing inside it.
    """
    read = make_reader(cache, cmap)
    si, q, rem = state
    checks = 0
    cl = cache.cl
    while si < len(samples):
        if rem == 0:
            q, rem = samples[si]
        while rem > 0:
            k = q // cl
            if k >= len(cmap):
                return (si, q, rem), k, checks, True
            raw = read(q, lensize)
            if raw is None:
                return (si, q, rem), q // cl, checks, True
            if k == watch:
                checks += 1
            length = int.from_bytes(raw, "big")
            if length <= 0 or length + lensize > rem:
                return (si, q, rem), None, checks, False
            q += lensize + length
            rem -= lensize + length
        si += 1
    return (si, q, rem), None, checks, True


def candidates(last: int, gaps: list[int], radius: int, lo: int, hi: int):
    """Sequential, then gaps already seen in this file, then a widening ring.

    Ordering is what makes this tractable: a full-device ring search costs on the
    order of 100 GB of reads per unresolved cluster.
    """
    seen = set()
    for c in [last + 1]:
        if lo <= c <= hi and c not in seen:
            seen.add(c)
            yield c
    for g in dict.fromkeys(reversed(gaps)):
        c = last + 1 + g
        if lo <= c <= hi and c not in seen:
            seen.add(c)
            yield c
    for r in range(1, radius + 1):
        for c in (last + 1 + r, last + 1 - r):
            if lo <= c <= hi and c not in seen:
                seen.add(c)
                yield c


def chase(cache, file_start, moov, radius, lo, hi, claimed=None):
    """Resolve the file's cluster map. Returns a dict with cmap, state and status."""
    cl = cache.cl
    samples = track_samples(moov, b"vide")
    if not samples:
        return None
    lensize = nal_length_size(moov)
    cmap = [file_start // cl]
    state = (0, 0, 0)
    gaps: list[int] = []
    unverified = 0

    while True:
        state2, need, _checks, ok = advance(cache, cmap, samples, state, lensize, -1)
        if not ok:
            return dict(cmap=cmap, samples=samples, state=state2, status="corrupt",
                        unverified=unverified, lensize=lensize)
        if need is None:
            return dict(cmap=cmap, samples=samples, state=state2, status="complete",
                        unverified=unverified, lensize=lensize)
        state = state2
        k = len(cmap)
        last = cmap[-1]
        picked = None
        for cand in candidates(last, gaps, radius, lo, hi):
            if claimed and cand in claimed:
                continue
            cmap.append(cand)
            _s, _n, checks, ok2 = advance(cache, cmap, samples, state, lensize, k)
            if ok2 and checks > 0:
                picked = cand
                break
            if ok2 and checks == 0:
                # Nothing in this cluster can be checked by the video walk. Take the
                # sequential guess and record it -- video stays correct either way,
                # but audio living here may not.
                picked = cand
                unverified += 1
                break
            cmap.pop()
        if picked is None:
            return dict(cmap=cmap, samples=samples, state=state, status="lost",
                        unverified=unverified, lensize=lensize)
        gap = picked - last - 1
        if gap:
            gaps.append(gap)


def output_length(moov: bytes, entry: dict) -> int:
    """Max sample end across all tracks, capped at the declared mdat size."""
    ends = [0]
    for handler in (b"vide", b"soun", b"meta", b"hint"):
        for off, size in track_samples(moov, handler):
            ends.append(off + size)
    declared = entry["header_bytes"] + entry["mdat_size"]
    return min(max(ends), declared)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("device")
    ap.add_argument("-o", "--outdir", required=True)
    ap.add_argument("--scan", default=None, help="scan.json (default: OUTDIR/../scan.json)")
    ap.add_argument("--radius", type=int, default=4096,
                    help="ring search radius in clusters (default 4096 ~ +/-512MB)")
    ap.add_argument("--only", type=int, default=None, help="rebuild a single index")
    ap.add_argument("--min-free-gb", type=float, default=5.0)
    args = ap.parse_args()

    scan_path = args.scan or os.path.join(os.path.dirname(args.outdir.rstrip("/")) or ".",
                                          "scan.json")
    if not os.path.exists(scan_path):
        scan_path = "scan.json"
    scan = json.load(open(scan_path))
    cl = scan["cluster_size"]
    files = [f for f in scan["files"] if f["file_start"] is not None]
    files.sort(key=lambda f: f["file_start"])          # disk order -> better cache hits
    if args.only is not None:
        files = [files[args.only]]

    os.makedirs(args.outdir, exist_ok=True)
    lo, hi = 0, (os.path.getsize(args.device) if os.path.isfile(args.device)
                 else 1 << 62) // cl
    report_path = os.path.join(args.outdir, "rebuild.json")
    report = json.load(open(report_path)) if os.path.exists(report_path) else []
    done = {r["file_start"] for r in report if r.get("status") == "complete"}

    fd = os.open(args.device, os.O_RDONLY)
    cache = ClusterCache(fd, cl)
    claimed: set[int] = set()
    for r in report:
        claimed.update(r.get("cmap", []))

    t0 = time.time()
    try:
        for i, entry in enumerate(files, 1):
            if entry["file_start"] in done:
                print(f"[{i:>3}/{len(files)}] already done, skipping")
                continue
            free = os.statvfs(args.outdir)
            if free.f_bavail * free.f_frsize < args.min_free_gb * 1024**3:
                print("\nStopping: free space below the safety floor.")
                break

            moov = aligned_read(fd, entry["moov_off"], entry["moov_size"])
            label = f"{entry['created'] or '?'}  {entry['duration_s']:.1f}s"
            print(f"[{i:>3}/{len(files)}] {label}  "
                  f"declared {entry['mdat_size'] / 1024**2:.1f} MB "
                  f"({(time.time() - t0) / 60:.0f} min elapsed)", flush=True)

            result = chase(cache, entry["file_start"], moov, args.radius, lo, hi,
                           claimed=claimed)
            if result is None:
                print("        no video track, skipped")
                continue

            si = result["state"][0]
            samples = result["samples"]
            total = output_length(moov, entry)
            # The walk skips payload, so the final NAL may extend past the resolved
            # map. Resolve forward (sequentially) until the output length is covered.
            while len(result["cmap"]) * cl < total:
                result["cmap"].append(result["cmap"][-1] + 1)

            read = make_reader(cache, result["cmap"])
            body = bytearray(read(0, total) or b"")
            if len(body) < total:
                print(f"        short read ({len(body):,} of {total:,}), skipping")
                continue
            # header_bytes is the offset of the mdat box itself, so its size field
            # starts right there. Writing it 8 bytes earlier lands on the preceding
            # wide/free box and inflates that box until it swallows mdat -- a defect
            # ffmpeg does not notice, because stco offsets are absolute and it finds
            # moov independently, so it survives casual testing.
            struct.pack_into(">I", body, entry["header_bytes"],
                             len(body) - entry["header_bytes"])

            stamp = (entry["created"] or "unknown").replace("-", "").replace(":", "").replace(" ", "_")
            kind = "main" if entry["mdat_size"] > 50 * 1024**2 else "proxy"
            name = f"{stamp}_{kind}_{i:03d}.mp4"
            path = os.path.join(args.outdir, name)
            with open(path, "wb") as fh:
                fh.write(bytes(body))
                fh.write(moov)

            claimed.update(result["cmap"])
            pct = si * 100 / len(samples) if samples else 0
            mark = "OK  " if result["status"] == "complete" else "PART"
            print(f"        {mark} {result['status']:<9} samples {si}/{len(samples)} "
                  f"({pct:.0f}%)  {total / 1024**2:.1f} MB  "
                  f"unverified clusters {result['unverified']}", flush=True)

            report = [r for r in report if r["file_start"] != entry["file_start"]]
            report.append({
                "file": name,
                "file_start": entry["file_start"],
                "created": entry["created"],
                "declared_duration_s": entry["duration_s"],
                "status": result["status"],
                "samples_validated": si,
                "samples_total": len(samples),
                "percent": round(pct, 1),
                "bytes": total,
                "unverified_clusters": result["unverified"],
                "cmap": result["cmap"],
            })
            with open(report_path, "w") as fh:
                json.dump(report, fh, indent=1)
    finally:
        os.close(fd)

    complete = [r for r in report if r["status"] == "complete"]
    print(f"\n=== {len(complete)} complete of {len(report)} attempted "
          f"in {(time.time() - t0) / 60:.0f} min ===")
    partial = [r for r in report if r["status"] != "complete"]
    if partial:
        print(f"{len(partial)} incomplete -- retry them with a larger radius:")
        print(f"  sudo python3 rebuild.py {args.device} -o {args.outdir} --radius 16384")
    print(f"Cluster maps saved in {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
