# Fragmentation and cluster chasing

The core problem. `stco` gives offsets **within the file**; the card stores the file in scattered
clusters. Recovery means rebuilding that mapping without the FAT.

## Contents

- [Why camera files fragment](#why-camera-files-fragment)
- [The single-cluster rule](#the-single-cluster-rule)
- [Deriving the fingerprint](#deriving-the-fingerprint)
- [The chase loop](#the-chase-loop)
- [Candidate ordering](#candidate-ordering)
- [Unverifiable clusters](#unverifiable-clusters)
- [Determining cluster size](#determining-cluster-size)
- [Failure modes](#failure-modes)

## Why camera files fragment

Action cameras record a full-resolution file and a low-resolution proxy (LRV) simultaneously. The
allocator hands out an extent to one, then the other, alternating. Add free-space reuse from
earlier deleted recordings and the layout becomes irregular.

Measured in the reference case: the main file took 16 MiB (128 clusters), then the proxy took
about 2 MiB, then back. Observed gaps between successive extents were 18, 16, 148, 44, 16, 4, 16
clusters — no usable pattern, but every one resolvable by verification.

The practical consequence: the first extent of every file is contiguous and large (16 MiB here),
so naive carving recovers the first ~2.5 seconds of each recording and nothing more. That symptom
— *every* file playable for a few seconds — is the signature of extent-level fragmentation.

## The single-cluster rule

**Any fingerprint used to locate the continuation must fit inside one cluster.**

If the pattern you search for spans more than one cluster, and the file is fragmented at cluster
granularity, then no location on the device can match it — because the bytes that would complete
the pattern live in a different cluster. The search returns zero hits everywhere, which is
indistinguishable from "the data was erased".

This is not a hypothetical. In the reference case three searches used fingerprints of 189 KB,
614 KB and 838 KB against a 128 KB cluster size. All returned zero hits, including a full 63 GB
byte-aligned sweep. The conclusion drawn — "data destroyed by TRIM" — was wrong, and the totals
had already reconciled (49.4 GB present vs 45.3 GB declared), which should have overruled it.
Rebuilt with a 127,118-byte fingerprint, the first search hit immediately.

If a search over the whole device finds nothing, check the fingerprint's span against the cluster
size before concluding anything about the data.

## Deriving the fingerprint

The sample that straddles the extent boundary is the anchor. Its tail begins at the first byte of
the continuation cluster, so the fingerprint starts there and must end before the cluster does.

Worked example (reference case, cluster size 131,072):

```
boundary B              = 16,777,216      file offset where extent 1 ends
sample 72               = offset 16,688,270, size 216,064   (spans B)
  NAL0: length 55,638 → consumes 55,642 bytes
  NAL1: length 36,220 → consumes 36,224 bytes, ending at 16,780,136   (crosses B)

bytes of NAL1 past the boundary  = 16,780,136 − B = 2,920
remaining bytes of sample 72     = 216,064 − 55,642 − 36,224 = 124,198
```

So at the continuation cluster `C`:

- `C + 0 .. C + 2,920` — tail of NAL1, opaque payload, not verifiable
- `C + 2,920` — next NAL length field; the chain must consume exactly 124,198 bytes
- total span `127,118 bytes < 131,072` ✓

Test: `walk(C + 2920, 124198)`. Candidates are cluster-aligned, so iterate `C` over cluster
boundaries.

Two mistakes to avoid:

- **Dropping the carry.** The continuation does not start at a sample boundary. Searching for
  sample 73 at `C` instead of `C + carry` finds nothing.
- **Changing alignment when vectorising.** Rewriting the scan with numpy, it is easy to let the
  index array mean "position of sample 73" when the loop meant "candidate cluster start". That
  single confusion invalidated a full-device sweep in the reference case. Keep the loop variable's
  meaning explicit and assert the alignment.

If a boundary leaves no room for a verifiable chain inside one cluster, fall back to the next
sample that does, and treat the intervening clusters with the ordering rules below.

## The chase loop

```
cmap = [file_start // CL]                    # file cluster 0 → disk cluster
state = (sample_index=0, q=0, remaining=0)

loop:
    advance the NAL walk using cmap
    if the walk completed all samples        → done
    if the walk needs cluster k == len(cmap) → resolve it:
        for each candidate c in ordered_candidates():
            cmap.append(c)
            re-run the walk from the checkpoint
            if it passes and at least one length field fell inside cluster k:
                accept
            else:
                cmap.pop()
        if nothing accepted                  → truncate here, report partial
```

Requiring that at least one length field landed *inside* the candidate cluster is what makes
acceptance meaningful; without it, a cluster the walk merely skips over would "pass" trivially.

Cache cluster reads — the ring search revisits the same neighbourhood repeatedly. A few thousand
cached clusters cut device reads by roughly an order of magnitude in practice.

## Candidate ordering

Order determines whether a file takes 40 seconds or 40 minutes.

1. **`last + 1`** — contiguous continuation. Resolves the large majority of clusters.
2. **Previously observed gaps** — `last + 1 + g` for each `g` seen in this file. Allocation
   patterns repeat; this catches most of the rest cheaply.
3. **Widening ring** — `last + 1 ± r` for increasing `r`. Cap it: ±16,384 clusters is ±2 GB and
   costs up to ~4 GB of reads per hard cluster. A full-device ring is impractical (~100 GB of
   reads per cluster).

Run a bounded radius for the batch, then retry only the files that failed with a larger radius.
In the reference case the batch used ±4,096 and finished 106 of 112 files in 81 minutes; a ±16,384
retry over the remaining 6 recovered 4 more in 15 minutes.

An optimisation worth building in from the start: **record each file's resolved cluster map.**
Clusters claimed by already-rebuilt files cannot belong to another, so the candidate set shrinks
as work proceeds. Omitting this in the reference case made it unavailable exactly when the hard
files needed it, and recomputing would have cost another full batch run.

## Unverifiable clusters

A cluster containing no video NAL length field cannot be checked by the video walk — the chain
skips over it. Options, in order of preference:

1. Validate with the audio track (see `mp4-structure.md` — AAC first-byte element type). This is
   cheap and effective, since audio chunks are interleaved finely (~500 bytes per chunk).
2. Assume sequential and mark the cluster unverified. Video stays correct — the NAL chain
   re-anchors on the far side — but audio in that cluster may be wrong.

Count and report unverified clusters. They are the honest explanation for any residual audio
glitches.

## Determining cluster size

In order of reliability:

1. **exFAT boot sector** at partition start: `BytesPerSectorShift` at offset 108,
   `SectorsPerClusterShift` at 109; cluster size = `1 << (108 + 109)`. A quick format rewrites this
   and typically preserves the geometry.
2. **Alignment of `ftyp` offsets.** Files start on cluster boundaries, so the GCD of the observed
   offsets is a multiple of the cluster size. In the reference case every `ftyp` sat on a 131,072
   boundary.
3. **Common defaults.** 128 KB for 64–256 GB exFAT; 32 KB for smaller FAT32.

Getting this wrong breaks everything downstream — fingerprints will be mis-sized and candidates
mis-aligned — so confirm it with method 2 even when method 1 succeeds.

## Failure modes

| Symptom | Likely cause |
|---|---|
| Zero hits anywhere on the device | Fingerprint spans more than one cluster |
| Chase stalls almost immediately (<1% of samples) | That file's early clusters were overwritten |
| Video clean, audio glitchy | Unverified clusters resolved by assumption |
| File ends a few KB short; audio tail missing | Output length taken from the video track only |
| Rebuilt file plays in ffmpeg but not elsewhere | `mdat` size written at the wrong offset |
| Chase picks a wrong cluster that still validates | Fingerprint too short — require several consecutive samples |
