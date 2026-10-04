# sdcard-video-recovery

A [Claude Code](https://claude.com/claude-code) skill for recovering video from a formatted or
corrupted memory card — by rebuilding files from the container's own index instead of carving by
signature.

On the card this was developed against, PhotoRec recovered **zero** videos. This method recovered
**216.5 of 219.0 minutes (98.8%)**, with 107 of 112 files decoding cleanly on both video and
audio tracks.

## Why signature carvers fail here

Carvers like PhotoRec, Recuva and Disk Drill assume a file's bytes are contiguous. Camera footage
usually is not: an action camera writes a full-resolution file and a low-res proxy at the same
time, so the filesystem interleaves them, and every long recording ends up split across many
clusters. When a format wipes the allocation table, the carver loses the chain. The typical result
is either nothing at all, or a file that plays for two seconds and stops.

The same card that produced 98.8% here produced this from PhotoRec 7.2:

```
0 videos
924,531 false positives  (~70 GB of all-zero files)
```

## The approach

An MP4 `moov` atom records the exact byte offset and size of every frame. That turns recovery
from guessing into verification — for any candidate cluster you can check whether the bytes there
continue the frame chain *exactly*, landing on the sample boundaries the index declares. The
constraint is strong enough that false positives effectively vanish.

1. **Probe** — sample the card and decide whether the data survived at all, before spending hours
   on it.
2. **Scan** — find every `ftyp` and `moov`, validate them, and pair each file start with its
   index. Produces a catalogue of what was on the card: times, durations, resolutions, codecs.
3. **Rebuild** — walk each file cluster by cluster, verifying the NAL length chain against
   `stsz`, searching for the continuation when an extent ends.
4. **Verify** — check structure, video and audio separately.

## Install

```bash
git clone https://github.com/jba1989/sdcard-video-recovery.git \
  ~/.claude/skills/sdcard-video-recovery
```

Then ask Claude Code about a formatted card and the skill triggers on its own. The scripts also
run standalone — see `SKILL.md`.

Requires Python 3.9+, and ffmpeg for verification.

## Check it before trusting it

```bash
python3 scripts/selftest.py
```

Builds a clip with ffmpeg, scatters it across non-contiguous clusters inside a synthetic card
image, rebuilds it, and asserts the result is byte-for-byte identical to the original. No damaged
card required.

```
Source 9,800,211 bytes, scattered across 75 non-contiguous clusters in a 600 MB image
  probe.py     ok
  scan.py      ok
  rebuild.py   ok
  verify.py    ok
PASS  rebuilt 9,800,211 bytes, byte-for-byte identical
```

The byte comparison is the point. A rebuilt file can play perfectly in ffmpeg while still being
structurally wrong, because `stco` offsets are absolute and ffmpeg locates `moov` independently —
so that class of defect survives casual testing. Writing this self-test caught six real bugs,
including one the documentation already warned about.

## What's here

| Path | |
|---|---|
| `SKILL.md` | Workflow, the single-cluster rule, scope and limits |
| `references/runbook.md` | Phase-by-phase checklist with a decision gate at each step |
| `references/diagnosis.md` | Did the data survive? Quick format vs TRIM vs overwrite |
| `references/mp4-structure.md` | Box parsing, `stco`/`stsz`/`stsc`, NAL prefixes, timestamps |
| `references/fragmentation.md` | Cluster chasing, fingerprint derivation, failure modes |
| `references/containers.md` | MP4 / fMP4 / MPEG-TS / JPEG / RAW — which approach applies |
| `references/case-insta360.md` | A fully measured run, including the wrong turns and their cost |
| `scripts/` | The four pipeline stages, a shared library, and the self-test |

## Scope

Works on non-fragmented MP4/QuickTime from cameras that write a global index — GoPro, DJI,
Insta360, Sony, Canon, Nikon, phones, most dashcams.

Does not apply to fragmented MP4 (`moof`-based, where plain carving usually works), MPEG-TS /
AVCHD (different validation), MJPEG or RAW burst (no global index), or cards that were genuinely
TRIM'd, overwritten or physically damaged. `references/containers.md` covers how to tell.

## The mistake worth copying

Three searches for the fragment continuation returned zero hits, including a full 63 GB sweep.
The conclusion drawn — the data had been destroyed — was written into a report before being
overturned.

Every one of those searches used a fingerprint larger than the 128 KB cluster size. With the file
fragmented at cluster granularity, no location on the device could ever match, so zero hits was
guaranteed regardless of whether the data existed. Meanwhile the arithmetic had already settled
it: 49.4 GB of real data measured on the card against 45.3 GB declared by the indexes. Those
numbers reconciled, which meant the data was present and the search was broken.

Rebuilt as a single-cluster fingerprint, the first search hit immediately.

**When a measurement and a search disagree, suspect the search.** That is the part of this
project most likely to be useful somewhere else, and it is why the runbook is organised around
gates rather than steps.

## License

MIT
