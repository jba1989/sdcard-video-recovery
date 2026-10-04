# MP4 / QuickTime structure for recovery

Only the parts that matter for rebuilding a file from raw sectors.

## Contents

- [Box layout](#box-layout)
- [What a camera file looks like on disk](#what-a-camera-file-looks-like-on-disk)
- [Finding and validating headers](#finding-and-validating-headers)
- [Pairing ftyp with moov](#pairing-ftyp-with-moov)
- [The sample tables](#the-sample-tables)
- [NAL length prefixes](#nal-length-prefixes)
- [Timestamps and metadata](#timestamps-and-metadata)
- [Writing the rebuilt file](#writing-the-rebuilt-file)

## Box layout

Every box is `[4-byte big-endian size][4-byte type][payload]`. The size covers the header.

- `size == 1` → real size is a 64-bit value in the next 8 bytes (header is 16 bytes)
- `size == 0` → box runs to end of file (only legal for the last box)

Walking boxes is how you find the end of a file: step `offset += size` until the type is no longer
four printable ASCII characters.

## What a camera file looks like on disk

The common layout from action cameras and DSLRs:

```
offset 0     ftyp   size 28       brand, e.g. 'avc1' / 'qt  ' / 'mp42'
offset 28    wide   size 8        placeholder, allows mdat to be promoted to 64-bit
offset 36    mdat   size N        all the media payload
offset 36+N  moov   size M        the index — written last, when recording stops
```

Two consequences for recovery:

- `moov` is at the **end**. A truncated or partially-overwritten recording loses its index and
  becomes very hard to rebuild — the payload alone is not self-describing.
- The `mdat` size field is at **offset 36**, not 28. Offset 28 is the `wide` box's size field.
  Writing the corrected size to 28 inflates `wide` so that it swallows `mdat`. ffmpeg still plays
  such a file, because `stco` offsets are absolute and it locates `moov` independently — so this
  bug passes casual testing and fails elsewhere.

Some files use `free` instead of `wide`, or omit it. Parse the actual boxes rather than assuming
offsets.

## Finding and validating headers

Scan the raw device for the literal strings. Then validate, because a 4-byte signature matches by
chance about once per 4 GB:

| Candidate | Validation |
|---|---|
| `ftyp` | preceding 4-byte size is 8–128; next box is `wide`, `free` or `mdat` |
| `moov` | first child box is `mvhd` (bytes 12–16 from the box start) |
| JPEG | `FF D8 FF E1` **and** `Exif` at offset 6 (or `FF D8 FF E0` with `JFIF`) |

In the reference case this took 141 raw `moov` hits down to 112 real ones, and 28 JPEG hits down
to 3.

Scan in chunks with an overlap of at least the signature length, or you will miss matches that
straddle a chunk boundary.

## Pairing ftyp with moov

`stco` holds chunk offsets relative to the start of the file. The last chunk offset plus the last
sample's size equals the end of the media data, so:

```
mdat_size − last_stco_offset  ==  size of the final sample  (small, positive)
```

Pair each `ftyp` with the `moov` whose last `stco` entry satisfies `0 < mdat_size − last < 4096`.
This matched 112 of 112 files with no ambiguity in the reference case. It is far more reliable
than "nearest `moov` by distance", because fragmentation puts the index nowhere near the payload.

## The sample tables

Inside `moov` → `trak` → `mdia` → `minf` → `stbl`:

| Box | Contents |
|---|---|
| `stco` / `co64` | byte offset of each chunk, relative to file start (32- or 64-bit) |
| `stsz` | size of each sample; a non-zero `sample_size` field means all samples share one size |
| `stsc` | run-length map of samples-per-chunk |
| `stts` | sample durations |
| `stss` | sync (key) samples |

Expanding `stsc` to per-sample offsets:

```python
spc = []                                  # samples per chunk, expanded
for i, (first_chunk, per, _) in enumerate(entries):
    last = entries[i+1][0] - 1 if i+1 < len(entries) else chunk_count
    spc += [per] * (last - first_chunk + 1)

samples, si = [], 0
for ci in range(chunk_count):
    off = chunk_offsets[ci]
    for _ in range(spc[ci]):
        samples.append((off, sizes[si]))
        off += sizes[si]; si += 1
```

Identify the track type from `trak` → `mdia` → `hdlr`: `vide` or `soun` at payload offset 8.

Note that chunk offsets are **file-relative**. On a fragmented file they are not disk offsets —
reconstructing that mapping is the whole job (see `fragmentation.md`).

## NAL length prefixes

Inside MP4, H.264 and HEVC samples are a sequence of `[length][NAL payload]`. The length field
width comes from `avcC` / `hvcC` (`lengthSizeMinusOne + 1`), almost always 4 bytes.

This gives the verification primitive the whole method rests on:

```python
def walk(buf, off, sample_size, lensize=4):
    """True only if the NAL chain lands exactly on the sample boundary."""
    q, rem = off, sample_size
    while rem > 0:
        if q + lensize > len(buf): return False
        L = int.from_bytes(buf[q:q+lensize], 'big')
        if L <= 0 or L + lensize > rem: return False
        q += lensize + L
        rem -= lensize + L
    return rem == 0
```

Requiring exact arrival at the boundary is what makes this discriminating — random data passes a
single length check often enough to matter, but almost never produces a chain that sums exactly to
the declared sample size, and essentially never does so for several consecutive samples.

Annex-B start codes (`00 00 00 01`) appear in raw streams, not inside MP4. Do not expect them.

## Audio as a second validator

AAC samples carry no length prefix, but the first byte encodes the syntactic element type in its
top 3 bits. A stereo AAC-LC stream is a channel pair element with instance tag 0, so every sample
starts with `0x21`. Camera audio is uniform enough that this holds for an entire card.

Derive the expected byte from a region you already trust rather than hardcoding it, then use it to
validate clusters that contain no video NAL boundary — those are otherwise unverifiable, and
guessing "sequential" there is where audio corruption comes from.

## Timestamps and metadata

`mvhd` (and `tkhd`) hold creation and modification times as **seconds since 1904-01-01**, not the
Unix epoch:

```python
dt = datetime.datetime(1904, 1, 1) + datetime.timedelta(seconds=creation_time)
```

Version 0 uses 32-bit fields, version 1 uses 64-bit — the version is the first payload byte.
Duration is `duration / timescale`. Dimensions come from `tkhd`'s 16.16 fixed-point width/height.

This is how you restore shooting times after a format wipes the directory entries. JPEGs carry the
same information in EXIF, and some cameras (Insta360 among them) put the **original filename** in
`ImageDescription`.

## Writing the rebuilt file

```
[ftyp as found] [wide] [mdat with corrected size] [moov as found]
```

- Output length = max sample end across **all** tracks, capped at the declared `mdat` size. The
  audio track commonly extends past the last video sample.
- Patch the `mdat` size field at its real offset (36 in the common layout) to `body_len − 36`.
- A NAL walk reads only length prefixes and skips payload, so the walk can complete while the last
  NAL's bytes still lie beyond the resolved cluster map. Resolve one further cluster before
  writing, or the file ends a few KB short — which is exactly where the audio tail lives.
