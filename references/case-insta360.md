# Reference case — measured results and wrong turns

One complete run, kept because the numbers calibrate expectations and the mistakes are the
genuinely transferable part. All figures are measured, not estimated. No file contents or
identifying metadata are reproduced here.

## Setup

| | |
|---|---|
| Card | 128 GB SDXC, exFAT, 125.1 GB usable |
| Camera | Insta360 GO Ultra (4K HEVC + AAC, writes a main file and an LRV proxy per recording) |
| Event | In-camera format, no writes afterwards |
| Host | macOS, read-only access via `/dev/rdiskN` |
| Cluster size | 131,072 bytes (128 KB) |

## Outcome

| Metric | Value |
|---|---|
| Original footage | 219.0 minutes across 56 recordings (112 files including proxies) |
| **Recovered** | **216.5 minutes — 98.8%** |
| Files fully rebuilt | 110 / 112 |
| Recovered data | 44.35 GB |
| Video decode errors | 0 across all files |
| PhotoRec 7.2, same card | **0 videos**, 924,531 false positives (~70 GB of all-zero `.dovecot` files) |

Wall-clock: batch rebuild 81 minutes for 112 files; a wider-radius retry on the 6 failures
recovered 4 more in 15 minutes; a tail-repair pass took 20 minutes.

## What the card looked like

Sampling 400 points × 1 MB:

```
  0.0 – 0.3 GB   zeros
  0.3 – 49.7 GB  dense data        ← everything lived here
 49.7 – 62.5 GB  zeros
 62.5 – 62.9 GB  dense data
 62.9 – 125.1 GB zeros
```

Structure checks on the dense region: `00 00 00` occurred ~4,928× above chance, `00 00 00 01`
~3,072× above chance, 4 KB-block entropy median 7.954 bits (not the 8.000 of random fill). Real
compressed media, not an overwrite.

Header scan: 120 raw `ftyp` hits → 112 valid; 141 raw `moov` hits → 112 valid (filter: first child
box is `mvhd`); 28 raw JPEG hits → 3 real photos (filter: `Exif` at offset 6).

Pairing on `mdat_size − last_stco_offset ∈ (0, 4096)` matched **112 of 112** with no ambiguity.

Declared `mdat` total 45.3 GB vs 49.4 GB measured on the card — reconciled, so the data was
present. This number was available early and should have settled the question long before it did.

## Fragmentation observed

Main file extent 1 was 16 MiB (128 clusters) in every file; the LRV proxy's was ~2 MiB. Gaps
between successive extents of one file: 18, 16, 148, 44, 16, 4, 16 clusters. Irregular, but every
one resolvable by verification.

Candidate ordering paid off heavily: sequential (`last + 1`) resolved the large majority, observed
gap sizes most of the rest, and the ring search was needed only occasionally.

## The three wrong turns

**1. Fingerprints larger than a cluster — cost: three searches, one wrong conclusion.**
Searches used 189 KB, 614 KB and 838 KB patterns against a 128 KB cluster size. With the file
fragmented at cluster granularity, no location could match; all returned zero hits, including a
full 63 GB byte-aligned sweep. The conclusion drawn — the data had been destroyed by TRIM — was
written into a report before being overturned. Rebuilt as a 127,118-byte single-cluster
fingerprint, the first search hit immediately.

The lesson is not "use small fingerprints" but *when a measurement and a search disagree, suspect
the search*. The totals had already reconciled.

**2. Lost alignment while vectorising — cost: a full 63 GB sweep.**
The scalar loop iterated candidate cluster starts `C` and tested at `C + carry`. The numpy rewrite
let the index array mean "position of sample 73" instead, dropping `carry` and testing only
512-aligned positions — where the target could never be. Widening the search from 1 GB to 63 GB
made the zero result look more conclusive rather than exposing the bug.

**3. Verifying only the video track — cost: a near-miss on shipping broken audio.**
`ffmpeg -map 0:v` reported 0 errors on every file. Including audio revealed ~93 error lines per
minute. Two separate assembly bugs:

- Output length was taken from the last *video* sample; the audio track extends past it, so the
  final 3–5 seconds of audio were missing from every file (up to 134 KB).
- The `mdat` size field was written at offset 28 (the `wide` box's field) instead of 36. ffmpeg
  tolerates this because `stco` offsets are absolute, so it survived all earlier testing.

Both were fixed without re-running the rebuild: the box field was patched in place, and the
missing tails were located with a single device-wide scan matching a 4 KB signature from each
file's last complete cluster, then appended.

## Things worth building in from the start

- **Persist each file's cluster map.** Clusters claimed by rebuilt files cannot belong to another,
  which shrinks the candidate set as work proceeds — most valuable for the hard files, which come
  last. Not having saved them meant the option was gone when it was needed.
- **Resolve one cluster past the end of the NAL walk.** The walk reads length prefixes and skips
  payload, so it can finish while the last NAL's bytes are still unmapped.
- **Keep a single implementation of the chase.** The duplicate scalar/vectorised versions caused
  mistake 2. Where a variant is needed, derive it from the same source rather than copying.
- **Prefer targeted repair over re-running.** Re-running the batch to fix the audio tails would
  have overwritten 110 correct files, including 4 recovered only on the retry pass, to fix a few
  seconds of audio.
