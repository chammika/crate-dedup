# crate-dedup

**🔗 Live ranking: [chammika.github.io/crate-dedup](https://chammika.github.io/crate-dedup/)**: the top 500
upgrades that would remove duplicate crates from the ecosystem, rebuilt daily from the crates.io dump.
Expand any row to see which crates benefit, who else holds the old version, and who maintains it.

Finds crates on crates.io whose outdated dependency forces the rest of the ecosystem to compile
two copies of the same crate (e.g. `syn 2` and `syn 3`), and ranks them by how much one upgrade
PR would help.

```
uv run analyze.py <crates.io-dump>/data report.json
```

Input: the daily dump at https://static.crates.io/db-dump.tar.gz (CSV files under `data/`).

### Past rankings and history

```
uv run analyze.py <dump>/data report.json --as-of 2026-10-01 --archive <dir> --history <dir>
```

- `--as-of DATE` rebuilds the ranking as it stood at the start of `DATE`. Versions published later are
  dropped, each crate's current version is re-derived with crates.io's rule (highest non-yanked stable
  release), and `d90` covers the 90 days before `DATE`.
- `--archive DIR` holds the older daily download files that the dump's 90-day window doesn't cover, from
  https://static.crates.io/archive/version-downloads/ (`<date>.csv`). The script warns if any day is missing.
- `--history DIR` also writes a slim `<date>.json` snapshot (rank, crates, impact, affected, sole). Snapshots
  live on the `history` branch.

Rebuilt rankings are approximate (`"approximate": true`). Yank flags and owners are today's, because the
dump doesn't record when they changed. Measured error: rebuilding today's date from today's dump gives the
same top-100 set as the live ranking, with a median rank difference of 1.

## Model

- **Graph:** one node per crate, using its current (default) version. Edges are that version's
  normal, non-optional dependencies.
- **Line:** the semver-compatible series a requirement resolves to: `^1.4` → `1`, `^0.3` → `0.3`,
  `^0.0.7` → `0.0.7`. A range that admits the dependency's latest version (`>=0.52, <0.62` with
  latest 0.61.2) resolves to the latest line.
- **Laggard edge `L → D`:** crate `L` requires an older line of `D` than `D`'s latest.
- **Affected crate `R`:** `L` is somewhere in `R`'s dependency tree, and `R`'s tree also requires `D`'s
  latest line, so `R` compiles `D` twice. `R` can be `L` itself.

## Score

```
impact(L → D) = Σ over affected crates R of  d90(R) / k(R)
```

- `d90(R)`: downloads of `R` (all versions) in the last 90 days. This weights by current usage, so
  abandoned crates count for little.
- `k(R)`: the number of laggard crates in `R`'s tree that hold `D` on the same old line. The
  duplicate only goes away when all `k` upgrade, so the credit is split `k` ways. A **sole blocker**
  (`k = 1`) gets `R`'s full weight.

In plain terms: impact is the download traffic that would stop compiling an extra copy of `D` if
`L` upgraded, with credit shared among co-blockers.

**Grace period:** pairs whose new line of `D` was released less than `GRACE_DAYS` (30) days before
the dump date are left out of the ranking. Lockstep projects often clear such duplicates in their next
release, and new lines still get patches, so a PR that early is premature. This filters the list and
doesn't change the score. The excluded count is `stats.in_grace`.

Known limits:
- Downloads are double counted through nesting. If `A` depends on `B`, both count. That's fine for
  ranking, but impact is not a count of real builds.
- Optional, build and dev dependencies are ignored, so true duplication is higher.
- Only each crate's latest version is analyzed. Users pinned to older versions aren't modeled.

**MSRV inference:** derive and helper crates often declare no `rust-version`, but their parent's promise
binds them. zerocopy says 1.56, so zerocopy-derive can't need more. The parent is a crate with the same
(normalized) repository URL that depends on `lag` in any way. With several parents, the most downloaded
wins, because forks copy the upstream repository URL but often keep a stale MSRV.

## report.json

`{ "generated_at": ..., "items": [...] }`, top 500 by impact. Each item:

| field | meaning |
|---|---|
| `rank`, `impact` | position and score (above) |
| `affected` | crates that compile `dep` twice and have `lag` in their tree |
| `sole` | affected crates where `lag` is the only blocker: upgrading fully removes the duplicate |
| `lag`, `lag_version`, `lag_d90`, `repository`, `owners`, `lag_last_release` | the crate to upgrade, and who maintains it |
| `dep`, `req`, `old_line`, `dep_latest`, `dep_line_since` | the outdated dependency, and when its latest line was first released |
| `lag_msrv`, `lag_msrv_via`, `dep_msrv`, `msrv_bump` | `rust_version` of each. `msrv_bump: true` means upgrading raises `lag`'s MSRV. `null` means unknown. If `lag` declares none, `lag_msrv` is inherited from its parent, and `lag_msrv_via` names it (see MSRV inference above) |
| `top_affected` | the 10 affected crates with the most downloads: `{name, d90, sole}` |
| `co_blockers` | the 10 other crates holding the same old line in the same trees: `{name, shared}` |
