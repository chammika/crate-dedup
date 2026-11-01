# Ranking history

One slim snapshot of the crate-dedup ranking per file, named `<as-of date>.json`:
`{as_of, approximate, items: [{rank, lag, dep, old_line, dep_latest, impact, affected, sole}]}`.

Snapshots with `"approximate": true` were rebuilt after the fact with `analyze.py --as-of` (today's yank
flags and owners). The others were taken from that day's crates.io dump.
