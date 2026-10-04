# Runbook — step by step, with the gates that decide whether to continue

Work through the phases in order. Each ends in a gate: a question with a defined answer,
not a judgement call. The gates matter more than the steps — recovery goes wrong by
continuing past a bad signal or stopping before the evidence justifies it.

Record the gate answers as you go. They become the report at the end, and if someone
later asks "could more have been recovered?", they are the answer.

## Contents

- [Phase 0 — Preserve](#phase-0--preserve)
- [Phase 1 — Diagnose](#phase-1--diagnose)
- [Phase 2 — Inventory](#phase-2--inventory)
- [Phase 3 — Rebuild one file](#phase-3--rebuild-one-file)
- [Phase 4 — Rebuild the rest](#phase-4--rebuild-the-rest)
- [Phase 5 — Verify](#phase-5--verify)
- [Phase 6 — Retry what failed](#phase-6--retry-what-failed)
- [Phase 7 — Report and release](#phase-7--report-and-release)
- [Things that are never worth doing](#things-that-are-never-worth-doing)

---

## Phase 0 — Preserve

Every minute the card is writable is a minute it can get worse.

- [ ] Identify the device node precisely (`diskutil list` / `lsblk`). Confirm size and
      removability match the card the person described.
- [ ] Unmount every volume on it. Do not eject — the device node must stay.
- [ ] Confirm nothing remounted it (`diskutil info` / `mount | grep`).
- [ ] Tell the person not to insert the card into the camera again. Cameras write on
      insert.
- [ ] Decide: work on the card directly, or image it first?
      Image it if reads are slow, throw I/O errors, or the card is suspected failing.
      `ddrescue -n /dev/rdiskN card.img card.map` then work on `card.img`.
- [ ] Check free space on the destination ≥ the card's used capacity.

**Gate 0 — may I read?**
Proceed only when the device is unmounted, you are opening it read-only, and the
destination is a *different* volume. If the only free space is on the card itself,
stop and get storage first.

---

## Phase 1 — Diagnose

```bash
sudo python3 probe.py /dev/rdiskN -o WORKDIR
```

- [ ] Read the density map. Note where data lives and how much.
- [ ] Read the structure block. `00 00 00` ratio in the thousands means real media.
- [ ] Note the estimated data volume — Phase 2 checks against it.

**Gate 1 — is anything there?**

| Verdict | Action |
|---|---|
| RECOVERABLE | Continue. |
| OVERWRITTEN | Entropy is flat and structureless. Stop; explain that the region was filled with random data. |
| ERASED | Continue to Phase 2 anyway. Probe samples a fraction of the card and can miss a lightly used one; scan.py reads every sector and is cheap. Only report "gone" after Phase 2 also finds nothing. |

Never report "the data is gone" on probe output alone. The cost of a false negative —
someone abandons recoverable footage — is far higher than the cost of one more scan.

---

## Phase 2 — Inventory

```bash
sudo python3 scan.py /dev/rdiskN -o WORKDIR
```

- [ ] Note raw vs validated counts. Heavy filtering is expected and healthy.
- [ ] Note the pairing rate. It should be at or near 100%.
- [ ] Note the cluster size and how it was determined. If it came from fewer than four
      `ftyp` offsets, treat it as a guess and sanity-check it.
- [ ] **Compare the declared `mdat` total against the probe estimate.** Write both
      numbers down.
- [ ] Show the person the catalogue — what was on the card, with times and durations —
      before spending hours rebuilding. They may only want some of it.

**Gate 2 — does the evidence agree?**

| Observation | Meaning |
|---|---|
| Totals within ~10%, pairing near 100% | Data is present. Proceed, and from here on treat any empty search result as a bug in the search. |
| Declared ≫ measured | Some files were partly overwritten. Proceed, expect partial results, say so up front. |
| No valid `ftyp`/`moov` at all | Either not an MP4-family container (check `references/containers.md`) or genuinely erased. Stop. |
| Pairing well below 100% | Do not proceed to a batch. Diagnose the pairing first — a systematic mis-pair will waste the whole run. |

---

## Phase 3 — Rebuild one file

Never start a multi-hour batch before one file has come out correct.

```bash
sudo python3 rebuild.py /dev/rdiskN -o OUTDIR --only 0
python3 verify.py OUTDIR
```

- [ ] Status `complete`, samples 100%.
- [ ] Box chain walks `ftyp+...+mdat+moov` and ends exactly at EOF.
- [ ] Duration matches what the catalogue said.
- [ ] Video decode errors 0.
- [ ] Audio decode errors 0.
- [ ] Open it and look at it. Metadata agreeing is not the same as a picture.

**Gate 3 — is one file right?**
All six boxes ticked, or fix the cause before batching. If the pilot file is partial,
check the cluster size first — a wrong cluster size breaks every fingerprint and is the
most common root cause.

---

## Phase 4 — Rebuild the rest

```bash
sudo python3 rebuild.py /dev/rdiskN -o OUTDIR --radius 4096
```

- [ ] Watch the first few files, then leave it. Re-running skips completed files.
- [ ] Keep an eye on free space; the script stops at the floor rather than filling the
      disk.
- [ ] Note which files end `lost` or partial — Phase 6 handles them.
- [ ] Confirm `rebuild.json` holds a `cmap` per file. Phase 6 depends on it.

**Gate 4 — did the batch behave?**
A handful of partials is normal. If most files are partial, stop and re-examine the
cluster size and the pairing rather than grinding through with a bigger radius.

---

## Phase 5 — Verify

```bash
python3 verify.py OUTDIR
```

- [ ] Structure broken: 0.
- [ ] Video errors checked **separately** from audio.
- [ ] Durations match the catalogue.
- [ ] Spot-check several files by eye and ear, including the longest.

**Gate 5 — what will you actually claim?**

| Pattern | Cause | Fix |
|---|---|---|
| Structure broken | `mdat` size field written to the wrong box | Patch the field in place; no rebuild needed |
| Video clean, audio glitchy at the end | Output truncated at the last video sample | Locate each file's last cluster by signature and append the tail |
| Video clean, audio glitchy throughout | Clusters with no video NAL boundary resolved by assumption | Add audio-side validation, or report it as a known limit |
| Duration short | Chase stopped early | Phase 6 |

Fix by targeted repair wherever possible. Re-running the batch overwrites files that
are already correct — including any recovered only on a retry pass.

---

## Phase 6 — Retry what failed

Escalate in this order, cheapest first.

- [ ] **Wider radius.** `--radius 16384`. Minutes per file. Often enough.
- [ ] **Free-cluster restriction.** Build the set of clusters claimed by completed files
      (from `rebuild.json`, or by matching 4 KB signatures if the maps were not kept) and
      restrict candidates to what is left. This typically shrinks the search space by one
      to two orders of magnitude, and the remaining set is usually small enough to hold
      in memory — which turns each candidate test from a device read into pure CPU work.
- [ ] For each file still failing, look at what owns the clusters right after its start.

**Gate 6 — is the search space exhausted?**
Every candidate cluster is either claimed by a file that verified, or free. When the
free set has been searched exhaustively and the clusters after the file's start belong
to files that did verify, there is nothing left to try — the payload was overwritten.

That is a conclusion you can state. "We did not find it" is not; say which of the two
you have.

---

## Phase 7 — Report and release

- [ ] Recovered vs original: file count and total duration, as a percentage.
- [ ] Per file: creation time, original duration, recovered duration, sample
      percentage, video and audio error counts **separately**.
- [ ] For anything not recovered: the specific reason, with the evidence.
- [ ] State the cause in terms of what was measured. "Files were fragmented across
      N KB clusters and the FAT was overwritten by the format" is checkable. Do not
      assert a mechanism the data does not show — TRIM returns zeros, so finding
      another file's video at those addresses rules it out.
- [ ] Tell them to copy the output somewhere else before anything else happens to it.
- [ ] Say explicitly whether the card can be reused, and why.

**Gate 7 — would this survive someone checking?**
Every number in the report should trace to a command that was run. If a claim cannot be
traced, remove it or go measure it.

---

## Things that are never worth doing

- Writing anything to the source card, including letting the OS mount it.
- Running a signature carver first "just to see". On a fragmented card it produces
  hundreds of thousands of junk files and can fill the destination disk — one
  documented run wrote 70 GB of all-zero false positives and recovered no video.
- Concluding data is gone from search results alone, when the measured totals say it is
  present.
- Starting a long batch before one file has verified end to end.
- Re-running a batch to fix a defect that a targeted repair can fix.
- Reporting "N files recovered" without saying how many decode cleanly.
- Deleting intermediate JSON before the person has confirmed the output is good.
