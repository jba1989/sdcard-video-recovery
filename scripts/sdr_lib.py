#!/usr/bin/env python3
"""Shared primitives for SD card video recovery.

Everything here is read-only with respect to the source device. Raw character devices
(/dev/rdiskN on macOS) reject unaligned reads, so all access goes through the aligned
helpers rather than bare os.read.
"""
from __future__ import annotations

import datetime
import os
import struct

SECTOR = 512
DEFAULT_CLUSTER = 128 * 1024

# Box types are four printable ASCII characters; this set is the practical alphabet.
_VALID_TYPE = set(
    b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 -_"
)


# ---------------------------------------------------------------- raw device I/O

def aligned_read(fd: int, off: int, n: int) -> bytes:
    """Read n bytes at an arbitrary offset from a device requiring sector alignment."""
    base = off - (off % SECTOR)
    pad = off - base
    span = ((pad + n + SECTOR - 1) // SECTOR) * SECTOR
    os.lseek(fd, base, os.SEEK_SET)
    buf = bytearray()
    while len(buf) < span:
        chunk = os.read(fd, min(64 << 20, span - len(buf)))
        if not chunk:
            break
        buf += chunk
    return bytes(buf[pad:pad + n])


def device_size(path: str) -> int:
    """Byte size of a block/character device or a regular file image."""
    fd = os.open(path, os.O_RDONLY)
    try:
        try:
            return os.lseek(fd, 0, os.SEEK_END)
        except OSError:
            return os.fstat(fd).st_size
    finally:
        os.close(fd)


class ClusterCache:
    """Read-through cache of fixed-size clusters. The ring search revisits the same
    neighbourhood repeatedly, so caching cuts device reads by roughly an order of
    magnitude."""

    def __init__(self, fd: int, cluster_size: int = DEFAULT_CLUSTER, limit: int = 6000):
        self.fd = fd
        self.cl = cluster_size
        self.limit = limit
        self._cache: dict[int, bytes] = {}
        self.reads = 0

    def cluster(self, index: int) -> bytes:
        hit = self._cache.get(index)
        if hit is not None:
            return hit
        os.lseek(self.fd, index * self.cl, os.SEEK_SET)
        buf = bytearray()
        while len(buf) < self.cl:
            chunk = os.read(self.fd, self.cl - len(buf))
            if not chunk:
                break
            buf += chunk
        data = bytes(buf)
        self.reads += 1
        if len(self._cache) >= self.limit:
            self._cache.clear()
        self._cache[index] = data
        return data


# ------------------------------------------------------------------ MP4 parsing

def boxes(buf: bytes, start: int = 0, end: int | None = None):
    """Yield (type, payload_start, box_end) for each box in buf[start:end]."""
    end = len(buf) if end is None else end
    off = start
    while off + 8 <= end:
        size = int.from_bytes(buf[off:off + 4], "big")
        btype = buf[off + 4:off + 8]
        header = 8
        if size == 1:
            size = int.from_bytes(buf[off + 8:off + 16], "big")
            header = 16
        elif size == 0:
            size = end - off
        if size < header or off + size > end:
            return
        yield btype, off + header, off + size
        off += size


def find_path(buf: bytes, start: int, end: int, path: list[bytes]):
    """All (payload_start, payload_end) ranges matching a nested box path."""
    if not path:
        return [(start, end)]
    out = []
    for btype, s, e in boxes(buf, start, end):
        if btype == path[0]:
            out += find_path(buf, s, e, path[1:])
    return out


def valid_box_type(four: bytes) -> bool:
    return len(four) == 4 and all(c in _VALID_TYPE for c in four)


def mp4_time(seconds: int) -> datetime.datetime:
    """MP4 timestamps count from 1904-01-01, not the Unix epoch."""
    return datetime.datetime(1904, 1, 1) + datetime.timedelta(seconds=seconds)


def parse_mvhd(moov: bytes):
    """Return (creation_datetime, duration_seconds) or (None, 0.0)."""
    for s, _e in find_path(moov, 0, len(moov), [b"moov", b"mvhd"]):
        version = moov[s]
        if version == 0:
            created, _mod, timescale, duration = struct.unpack(">IIII", moov[s + 4:s + 20])
        else:
            created, _mod = struct.unpack(">QQ", moov[s + 4:s + 20])
            timescale, duration = struct.unpack(">IQ", moov[s + 20:s + 32])
        secs = duration / timescale if timescale else 0.0
        return mp4_time(created), secs
    return None, 0.0


def track_samples(moov: bytes, handler: bytes):
    """Expand stco/stsz/stsc into [(file_offset, size)] for the first matching track.

    Chunk offsets are file-relative. On a fragmented file they are not disk offsets --
    reconstructing that mapping is what the chaser does.
    """
    for ts, te in find_path(moov, 0, len(moov), [b"moov", b"trak"]):
        hdlr = find_path(moov, ts, te, [b"mdia", b"hdlr"])
        if not hdlr or moov[hdlr[0][0] + 8:hdlr[0][0] + 12] != handler:
            continue

        stco = find_path(moov, ts, te, [b"mdia", b"minf", b"stbl", b"stco"])
        co64 = find_path(moov, ts, te, [b"mdia", b"minf", b"stbl", b"co64"])
        stsz = find_path(moov, ts, te, [b"mdia", b"minf", b"stbl", b"stsz"])
        stsc = find_path(moov, ts, te, [b"mdia", b"minf", b"stbl", b"stsc"])
        if not stsz or not stsc or not (stco or co64):
            continue

        if stco:
            base = stco[0][0]
            count = int.from_bytes(moov[base + 4:base + 8], "big")
            offsets = struct.unpack(f">{count}I", moov[base + 8:base + 8 + 4 * count])
        else:
            base = co64[0][0]
            count = int.from_bytes(moov[base + 4:base + 8], "big")
            offsets = struct.unpack(f">{count}Q", moov[base + 8:base + 8 + 8 * count])

        zbase = stsz[0][0]
        uniform = int.from_bytes(moov[zbase + 4:zbase + 8], "big")
        n_samples = int.from_bytes(moov[zbase + 8:zbase + 12], "big")
        if uniform:
            sizes = [uniform] * n_samples
        else:
            sizes = list(struct.unpack(
                f">{n_samples}I", moov[zbase + 12:zbase + 12 + 4 * n_samples]))

        cbase = stsc[0][0]
        n_entries = int.from_bytes(moov[cbase + 4:cbase + 8], "big")
        entries = [struct.unpack(">III", moov[cbase + 8 + 12 * i:cbase + 20 + 12 * i])
                   for i in range(n_entries)]

        per_chunk = []
        for i, (first_chunk, per, _desc) in enumerate(entries):
            last = entries[i + 1][0] - 1 if i + 1 < n_entries else count
            per_chunk += [per] * max(0, last - first_chunk + 1)

        samples, si = [], 0
        for ci in range(min(count, len(per_chunk))):
            off = offsets[ci]
            for _ in range(per_chunk[ci]):
                if si >= len(sizes):
                    break
                samples.append((off, sizes[si]))
                off += sizes[si]
                si += 1
        return samples
    return []


def nal_length_size(moov: bytes) -> int:
    """lengthSizeMinusOne + 1 from avcC/hvcC; defaults to 4."""
    for tag in (b"avcC", b"hvcC"):
        idx = moov.find(tag)
        if idx == -1:
            continue
        payload = moov[idx + 4:]
        # avcC: ...[profile][compat][level][0xFC|lengthSizeMinusOne]
        # hvcC: lengthSizeMinusOne sits at byte 21
        pos = 4 if tag == b"avcC" else 21
        if len(payload) > pos:
            return (payload[pos] & 0x03) + 1
    return 4


# -------------------------------------------------------------- verification

def walk_nals(buf: bytes, off: int, sample_size: int, lensize: int = 4) -> bool:
    """True only if the NAL length chain lands exactly on the sample boundary.

    Exactness is what makes this discriminating: random bytes pass a single length
    check often enough to matter, but almost never sum precisely to the declared
    sample size, and essentially never do so for several consecutive samples.
    """
    q, rem = off, sample_size
    while rem > 0:
        if q + lensize > len(buf):
            return False
        length = int.from_bytes(buf[q:q + lensize], "big")
        if length <= 0 or length + lensize > rem:
            return False
        q += lensize + length
        rem -= lensize + length
    return rem == 0


def jpeg_length(buf: bytes) -> tuple[int, bool]:
    """Walk JPEG markers. Returns (length, complete).

    Three details trip up naive scanners: FF 00 inside entropy-coded data is a stuffed
    byte, FF D0-FF D7 are restart markers, and runs of FF are legal marker padding.
    """
    if buf[0:2] != b"\xff\xd8":
        return 0, False
    i, n = 2, len(buf)
    while i < n - 1:
        if buf[i] != 0xFF:
            return i, False
        while i < n and buf[i] == 0xFF:
            i += 1
        if i >= n:
            return i, False
        marker = buf[i]
        i += 1
        if marker == 0xD9:
            return i, True
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            continue
        if i + 2 > n:
            return i, False
        seg = int.from_bytes(buf[i:i + 2], "big")
        if seg < 2:
            return i - 2, False
        if marker == 0xDA:
            i += seg
            while i < n - 1:
                if buf[i] == 0xFF:
                    nxt = buf[i + 1]
                    if nxt == 0x00 or 0xD0 <= nxt <= 0xD7:
                        i += 2
                        continue
                    if nxt == 0xFF:
                        i += 1
                        continue
                    if nxt == 0xD9:
                        return i + 2, True
                    break
                i += 1
            else:
                return i, False
            continue
        i += seg
    return i, False


# ------------------------------------------------------------- cluster size

def detect_cluster_size(fd: int, ftyp_offsets: list[int], dev_size: int) -> tuple[int, str]:
    """Return (cluster_size, how_it_was_determined).

    Getting this wrong breaks everything downstream, so the alignment check runs even
    when the boot sector parses -- they should agree.
    """
    from_boot = None
    for part_start in (1024 * 1024, 32 * 1024, 0):       # common partition offsets
        try:
            bs = aligned_read(fd, part_start, 512)
        except OSError:
            continue
        if bs[3:11] == b"EXFAT   ":
            bytes_shift, sectors_shift = bs[108], bs[109]
            if 9 <= bytes_shift <= 12 and sectors_shift <= 25:
                from_boot = 1 << (bytes_shift + sectors_shift)
                break

    from_align = None
    # One or two offsets carry no information -- the GCD is just the offset itself,
    # which yields an absurd "cluster size". Require enough samples for the GCD to
    # actually converge.
    if len(ftyp_offsets) >= 4:
        from math import gcd
        from functools import reduce
        g = reduce(gcd, ftyp_offsets)
        # Keep the largest power of two dividing the GCD, bounded to sane sizes.
        p = 1
        while p * 2 <= g and p * 2 <= (32 << 20):
            p *= 2
        if p >= 4096:
            from_align = p

    if from_boot and from_align:
        if from_boot == from_align:
            return from_boot, "exFAT boot sector (confirmed by ftyp alignment)"
        return from_align, (f"ftyp alignment ({from_align}); boot sector said "
                            f"{from_boot} -- alignment wins")
    if from_align:
        return from_align, f"ftyp alignment ({from_align})"
    if from_boot:
        return from_boot, "exFAT boot sector"
    return DEFAULT_CLUSTER, "default (could not determine)"
