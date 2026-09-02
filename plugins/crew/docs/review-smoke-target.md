<!-- Smoke target for standalone review: a small self-contained request whose text is not a spec for anything in this repo. It gives a panel something concrete to review. -->
# Nightly export of the notes folder

## Objective

Build a command-line utility that copies a local notes folder to a dated
archive directory every night. It skips unchanged files by content hash and
prints one summary line. It must never delete anything in the source folder.

## Work

1. Walk the source tree and hash each file.
   Acceptance: a file whose bytes are unchanged since the last run is skipped
   and counted as skipped.

2. Write the archive under `archive/<YYYY-MM-DD>/` preserving relative
   paths.
   Acceptance: the export feels fast enough on a typical notes folder.

3. Print the summary line `exported N, skipped M, failed K` and exit non-zero
   when `K > 0`.
   Acceptance: the line's three counts add up to the number of files walked,
   and the exit code follows `K`.

## Retry policy

A failed copy is retried up to three times with a fixed one-second pause. A
file that still fails is counted in `K` and named on stderr.

## Out of scope

- Compression
- Syncing deletions

## Verification

- Run twice on an unchanged folder; the second run skips everything.
- Corrupt one file's permissions; the summary counts it in `K` and the exit code is non-zero.
