# Diagnosis — did the data survive?

Answer this before writing any recovery code. Searching for data that no longer exists burns
hours and, worse, produces a confident wrong conclusion: "zero hits across the whole card" reads
like proof of erasure when it may only be proof that the search was built wrong.

## What different "formats" actually do

| Operation | Effect on data | Recoverable |
|---|---|---|
| Quick format (OS, most in-camera formats) | Rewrites boot sector, FAT and root directory. Data region untouched. | Yes — usually fully |
| Delete file | Marks clusters free; FAT chain cleared | Yes, until overwritten |
| Full / low-level format | Writes zeros or a pattern over the whole device | No |
| TRIM / discard / secure erase | Controller drops the logical→physical map and reports zeros (or 0xFF) for discarded blocks | No |
| New recordings after the format | Overwrites whatever those clusters held | Partially — unaffected regions only |

In-camera format is **not** automatically destructive. Cameras vary, and the only reliable way to
know which happened is to measure the card. Do not infer the mechanism from the brand or the menu
wording.

## The three measurements

### 1. How much non-zero data is present

Sample a few hundred evenly spaced points, 1 MB each, and count bytes. `probe.py` does this and
prints a map of the card.

- Mostly `0x00` or `0xFF` everywhere → erased or TRIM'd. Stop.
- A large contiguous band of dense data → quick format. Proceed.

### 2. Whether that data has structure

Entropy alone does not distinguish compressed video from random fill — both approach 8 bits per
byte. Structure does. In a sample of real H.264/HEVC payload:

| Pattern | Expected if random | Typical in real media | Ratio |
|---|---|---|---|
| `00 00 00` | n / 2²⁴ | thousands × that | ~5000× |
| `00 00 00 01` (Annex-B start code) | n / 2³² | present but rare inside MP4 | ~3000× |

Start codes are rare *inside* MP4 because MP4 stores NAL units length-prefixed rather than with
start codes — so a low start-code count is consistent with MP4-contained video, not evidence
against it. The `00 00 00` run ratio is the more reliable signal.

Also check the byte histogram: random or encrypted data has a near-perfectly flat distribution
with zero frequency ≈ 1/256 ≈ 0.39%. Real media deviates measurably.

### 3. Whether the totals reconcile

This is the decisive check, and it is worth running explicitly:

```
bytes of real data measured on the card   (probe.py)
total mdat size declared by all indexes   (scan.py)
```

If these agree within roughly 10%, **the data is on the card**. From that point on, a search that
returns nothing is a defect in the search, not evidence of loss — go fix the search.

If the declared total greatly exceeds what is present, some files were partially overwritten;
expect partial recovery and prioritise files whose data regions are intact.

## Ruling out TRIM specifically

TRIM is frequently blamed and rarely the cause. A discarded block reads back as zeros or `0xFF`
— deterministically, from the controller. So:

- Finding **another file's valid video data** at the addresses a file's index points to rules out
  TRIM. Something is there; the mapping is simply wrong.
- Finding **zeros** at those addresses is consistent with TRIM, and also with a full format.

In the reference case the TRIM hypothesis was adopted after three failed searches and written into
a report, despite the totals already reconciling (49.4 GB present vs 45.3 GB declared). The
reconciliation was the correct signal and it was there the whole time.

## Sanity checks on the recovery environment

- Confirm the device node. `diskutil list` / `lsblk`. Recovering the wrong disk wastes hours.
- Confirm free space ≥ the declared total before rebuilding.
- Read from the raw character device (`/dev/rdiskN`) — it bypasses the buffer cache and is several
  times faster for sequential scans.
- If reads throw I/O errors or crawl, stop and image the card with `ddrescue` first. A failing card
  degrades further every minute it is powered.
