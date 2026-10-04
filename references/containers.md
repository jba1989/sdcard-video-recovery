# Identifying the container and choosing an approach

This method depends on the container carrying a global index. Check which case you are in before
committing to it.

## Quick identification from raw bytes

Scan the first few MB of the data region and look for:

| Bytes found | Container | Approach |
|---|---|---|
| `ftyp` + `moov` + `mdat` | MP4 / QuickTime, non-fragmented | **This skill.** Full index available. |
| `ftyp` + `moof` + `mdat` (repeating) | Fragmented MP4 (fMP4 / CMAF) | Carving often works — see below |
| `0x47` every 188 bytes | MPEG-TS / AVCHD (.MTS, .M2TS) | Packet-level reassembly — see below |
| `RIFF` + `AVI ` | AVI | Index in `idx1` at end; similar idea, different tables |
| `FF D8 FF` + `Exif` | JPEG | Marker walk — see below |
| `MATROSKA` / EBML `1A 45 DF A3` | MKV / WebM | Cues at end; rarely from cameras |

## Non-fragmented MP4 — the main case

Everything in `mp4-structure.md` and `fragmentation.md` applies. Typical producers: GoPro, DJI,
Insta360, Sony, Canon, Nikon, Panasonic, iPhone/Android (`.MOV`/`.MP4`), most dashcams.

Vendor quirks worth knowing:

- **Dual-file recording** (Insta360 `.insv` + `.lrv`, DJI, some GoPro modes) — the proxy is written
  concurrently, which is what causes the interleaving. Both files are recoverable and pair up by
  identical `creation_time` and duration.
- **Chaptering** — long recordings split at 4 GB (FAT32) or a fixed duration. Each chapter is an
  independent file with its own `moov`; they rejoin by consecutive creation times.
- **`qt  ` brand** (Apple, some Sony) — same structures, occasionally `co64` instead of `stco`.
- **GoPro `GPMF`** telemetry lives in a separate `trak` with handler `meta`; harmless, but include
  it when computing the output length.

## Fragmented MP4 (fMP4 / CMAF)

Each fragment carries its own `moof` with a `trun` box listing sample sizes, so a fragment is
self-describing and a carver can recover runs of them without the global index. Signs: `moof`
boxes throughout, `mvex` inside `moov`, `mdat` immediately following each `moof`.

Approach: carve from `ftyp`/`styp` and walk `moof`+`mdat` pairs. The cluster-chasing machinery
still helps if fragments are split mid-`mdat`, but the fingerprint comes from `trun` sample sizes
rather than `stsz`.

## MPEG-TS / AVCHD

188-byte packets, each starting with sync byte `0x47`, carrying a 13-bit PID and a 4-bit
continuity counter that increments per PID.

This is *easier* to validate than MP4 — a candidate cluster is correct if its packets align to the
188-byte grid and continuity counters continue the sequence for the relevant PIDs. There is no
global index to recover and no `moov` to lose, so even badly fragmented streams can be reassembled
and remuxed with `ffmpeg -c copy`.

Camcorders writing AVCHD also keep `.CPI` index files in `BDMV/CLIPINF`; if those survive they give
the original clip boundaries.

## JPEG and raw stills

No global index — walk the marker structure from `FF D8`:

- Segments are `FF <marker> <2-byte length>`; skip by length.
- After `FF DA` (start of scan) the data is entropy-coded: `FF 00` is a stuffed byte, `FF D0`–`FF D7`
  are restart markers, and any other `FF xx` ends the scan.
- The file ends at `FF D9`.

Stills are small and usually survive in one or two clusters. If one is truncated, append `FF D9`
and the decoder will render everything up to the cut — JPEG decodes top-to-bottom, so a partial
file still yields a usable upper portion.

Camera RAW (CR2/CR3, NEF, ARW, DNG) is TIFF-based: `II*\0` or `MM\0*`, with IFD offsets that can be
walked much like MP4 boxes. Large RAW files fragment and need the same chasing treatment.

## When no container index survives

If `moov` was overwritten, the payload alone is not self-describing. The remaining option is to
extract the elementary stream and re-wrap it:

```bash
ffmpeg -f hevc -i raw.h265 -c copy out.mp4     # or -f h264
```

This needs Annex-B start codes, so length-prefixed MP4 payload must be converted first (replace
each length prefix with `00 00 00 01`, and recover SPS/PPS/VPS from another file of the same
camera and recording mode). Expect no audio sync and no timestamps — frame order survives, timing
does not. Treat it as a last resort.
