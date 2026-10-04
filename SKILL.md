---
name: sdcard-video-recovery
description: "Recover video and photo files from a formatted, corrupted, or accidentally-erased SD card, CF card, USB stick, or camera storage — by rebuilding files from the container's own index (MP4 moov/stco/stsz) and chasing fragments cluster by cluster, instead of relying on signature carvers. Use this whenever someone says they formatted a memory card, lost footage, deleted recordings, or wants data recovery from a camera, action cam, drone, dashcam, or phone card — and especially when PhotoRec, TestDisk, Recuva, Disk Drill, or a commercial recovery tool already failed, returned nothing, or produced unplayable files. Also use when a recovered video exists but will not open, reports 'moov atom not found', or plays only the first few seconds."
---

# SD Card Video Recovery

Signature carvers (PhotoRec, Recuva, Disk Drill) assume files are stored contiguously. Camera
footage usually is not: the camera writes a main file and a low-res proxy at the same time, so
the filesystem interleaves them, and every large recording ends up split across many clusters.
When the format wipes the allocation table, the carver loses the chain and either produces
nothing or produces a file that plays for two seconds.

This skill rebuilds files from the index the container carries inside itself. An MP4 `moov`
atom records the exact byte offset and size of every frame. That turns recovery from guessing
into verification: for any candidate cluster you can check whether the bytes there continue the
frame chain exactly. The constraint is strong enough that false positives effectively vanish.

A real run of this method recovered **216.5 of 219.0 minutes (98.8%)** of 4K HEVC footage from a
128 GB card that PhotoRec had recovered **zero** videos from. See
`references/case-insta360.md` for the measured numbers.

## Before anything else: do not write to the card

Recovery reads the raw device. Any write — including the OS mounting the volume and creating
index files — can destroy what is left.

```bash
diskutil unmount /dev/diskNs1      # macOS: unmount, do NOT eject
umount /dev/sdXN                   # Linux
```

Work from the character device (`/dev/rdiskN` on macOS, `/dev/sdX` on Linux) and open it
read-only. If the card is physically failing (I/O errors, slow reads), image it first with
`ddrescue` and run everything against the image instead.

Raw device reads need root, and on macOS raw character devices reject unaligned reads — every
offset and length must be a multiple of the sector size. `scripts/sdr_lib.py` handles the
alignment; reuse it rather than calling `os.read` directly.

## Workflow

Run the four scripts in order. Each writes JSON that the next one consumes, so a long step never
has to be repeated.

`references/runbook.md` is the step-by-step checklist, with a defined gate at the end of each
phase. Follow it when actually doing a recovery — the gates are what keep the job honest, and
the answers you record become the report. The summary:

| Phase | Gate — proceed only when |
|---|---|
| 0 Preserve | Card unmounted, opened read-only, destination on another volume |
| 1 Diagnose | Verdict is RECOVERABLE — or ERASED, in which case scan anyway before concluding |
| 2 Inventory | Declared `mdat` total and the probe estimate agree; pairing near 100% |
| 3 Pilot | **One** file rebuilds complete, structure walks to EOF, video *and* audio clean |
| 4 Batch | Most files complete; a handful of partials is normal |
| 5 Verify | Video and audio checked separately; defects fixed by targeted repair, not a re-run |
| 6 Retry | Search space exhausted: every cluster is either claimed by a verified file, or free and searched |
| 7 Report | Every number traces to a command that was run |

Two of these are worth internalising even if you skip the rest. **Gate 2** prevents the expensive
mistake: once the totals reconcile, the data is on the card, and an empty search result is a bug
in the search rather than evidence of loss. **Gate 3** prevents the wasteful one: never start a
multi-hour batch before a single file has come out correct end to end.

Replace `/dev/rdiskN` with the card's raw device node and `OUTDIR` with a directory on a
*different* volume that has free space for the full recovered size.

```bash
cd "$(dirname "$0")"/../skills/sdcard-video-recovery/scripts   # or wherever this skill lives

sudo python3 probe.py   /dev/rdiskN                   # 1. is the data still there?
sudo python3 scan.py    /dev/rdiskN                   # 2. find and pair the indexes
sudo python3 rebuild.py /dev/rdiskN -o OUTDIR         # 3. chase clusters, write files
python3 verify.py OUTDIR                              # 4. QA report
```

Intermediate JSON is written next to the output directory, so steps can be re-run independently.

To check the toolchain before trusting it with real data — no damaged card required:

```bash
python3 selftest.py     # builds a clip, scatters it across non-contiguous clusters,
                        # rebuilds it, and asserts byte-for-byte equality
```

Run this after changing any parsing or chasing code. The byte comparison is the point: a rebuilt
file can play correctly in ffmpeg while still being structurally wrong, so only an exact
comparison catches that class of defect.

### 1. Probe — decide whether recovery is even possible

This step exists because the most expensive mistake in recovery is spending hours searching for
data that is gone. `probe.py` samples the card at a few hundred points and reports how much
non-zero data is present and how it is distributed.

Read the result like this:

| Observation | Meaning |
|---|---|
| Large contiguous region of high-entropy data | Quick format. Data intact. Proceed. |
| Almost everything reads as `0x00` or `0xFF` | TRIM / secure erase / full format. Data is gone; stop. |
| High-entropy data whose byte distribution is *perfectly* uniform (zero frequency ≈ 1/256) with no structure | Random overwrite. Not recoverable. |

The distinguishing test is structure, not entropy. Compressed video is high-entropy but still
contains far more `00 00 00` runs than chance predicts — `probe.py` reports that ratio. A value
in the thousands means real media; a value near 1.0 means random fill.

Later, cross-check the probe's byte count against the total the indexes declare (step 2). **If
those two numbers agree, the data is present**, and any search that finds nothing is a flaw in
the search, not evidence of loss. This cross-check is the single most useful judgement in the
whole process; `references/diagnosis.md` explains why.

### 2. Scan — find the indexes and pair them

`scan.py` sweeps the device for `ftyp` (file start) and `moov` (index), validates each candidate,
and pairs them.

Validation matters more than it looks. A four-byte signature hits by chance roughly once per 4 GB,
so on a 50 GB card expect a dozen false `moov` matches. Requiring the first child box to be `mvhd`
removes them. The same applies to JPEG: require `Exif` or `JFIF` at offset 6, not just `FF D8 FF E1`.

Pairing uses a quiet property of the format: `mdat_size − last_stco_offset` equals the size of the
final sample, always a small positive number. Matching on that paired 112 of 112 files in the
reference case with no ambiguity.

The scan also extracts each recording's creation time, duration, resolution and codec from `mvhd`
and `tkhd` — so even before rebuilding you can hand the user a catalogue of what was on the card.
MP4 timestamps count seconds from **1904-01-01**, not the Unix epoch.

### 3. Rebuild — chase the fragments

`rebuild.py` walks each file cluster by cluster. Within a cluster it parses the sample structure
(for H.264/HEVC: length-prefixed NAL units) and confirms the chain lands exactly on the sample
boundaries `stsz` declares. When the chain runs past the end of the resolved map, it tries
candidates for the next cluster — sequential first, then previously-observed gap sizes, then a
widening ring — and accepts the one that validates.

**The fingerprint must fit inside one cluster.** This is the rule that decides whether the method
works at all. If the data you match against spans more than one cluster, and the file is
fragmented at cluster granularity, no candidate can ever match — the search returns zero hits
everywhere, which looks exactly like "the data is gone". In the reference case this mistake cost
three search rounds and produced a confidently wrong conclusion. `references/fragmentation.md`
derives the correct single-cluster fingerprint, including the carry offset for a sample that
straddles the boundary.

Cluster size is usually 128 KB on a 128 GB exFAT card. `rebuild.py` reads it from the boot sector
when one survives and otherwise infers it from the alignment of the `ftyp` offsets.

### 4. Verify — check video and audio separately

```bash
ffmpeg -v error -i out.mp4 -map 0:v -f null -     # video track
ffmpeg -v error -i out.mp4 -map 0:a -f null -     # audio track
```

Checking only the video track hides a whole class of defect. In the reference case the video was
flawless while the last 3–5 seconds of every file's audio was missing, because the output length
had been taken from the last video sample and the audio track extends past it.

Two output-assembly details are easy to get wrong and `verify.py` checks both:

- **`mdat` size field position.** With the common `ftyp`(28) + `wide`(8) + `mdat` layout the field
  sits at offset **36**, not 28. Writing it at 28 corrupts the `wide` box. ffmpeg tolerates this
  because `stco` offsets are absolute, so the error survives casual testing and surfaces later in
  other players.
- **Output length.** Take the maximum sample end across **all** tracks, capped at the declared
  `mdat` size — not the video track's last sample. Note that a NAL walk reads only length
  prefixes and skips payload, so the chase can finish while the final NAL's bytes still lie in an
  unresolved cluster; resolve one more cluster before writing.

## Reporting to the user

Give the catalogue from step 2 (what was on the card) alongside the rebuild results, so they can
see what was recovered *and* what was lost. Per file: creation time, original duration, recovered
duration, percentage of samples validated, and video/audio error counts separately.

State the cause in terms of what was measured. "Files were fragmented across 128 KB clusters and
the FAT was overwritten by the format" is checkable. Avoid asserting a mechanism like TRIM unless
the probe data supports it — TRIM returns zeros, so finding another file's video data at those
addresses rules it out.

## When this does not apply

- **Fragmented MP4 (fMP4/CMAF)** — indexes live in per-fragment `moof` boxes; each fragment is
  self-describing, so plain carving often succeeds. Check for `moof` before using this method.
- **MPEG-TS / AVCHD (.MTS, .M2TS)** — 188-byte packets with `0x47` sync bytes and continuity
  counters, which makes validation easier but needs different code.
- **MJPEG, RAW burst, proprietary containers** — no global index; fall back to carving.
- **Encrypted volumes, TRIM'd cards, physically damaged flash** — out of scope.

See `references/containers.md` for how to recognise each case.

## References

Load these as needed; they are written to be read independently.

| File | Read it when |
|---|---|
| `references/runbook.md` | **Doing an actual recovery** — the phase-by-phase checklist and the gate criteria |
| `references/diagnosis.md` | Deciding whether data survived; interpreting probe output; TRIM vs quick format |
| `references/mp4-structure.md` | Parsing boxes, `stco`/`stsz`/`stsc`/`co64`, NAL length prefixes, timestamps |
| `references/fragmentation.md` | Building fingerprints, the single-cluster rule, candidate ordering, carry offsets |
| `references/containers.md` | Identifying the container and choosing an approach per camera/format |
| `references/case-insta360.md` | A fully measured end-to-end run, including the wrong turns and what they cost |
