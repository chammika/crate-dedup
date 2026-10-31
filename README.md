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

Known limits:
- Downloads are double counted through nesting. If `A` depends on `B`, both count. That's fine for
  ranking, but impact is not a count of real builds.
- Optional, build and dev dependencies are ignored, so true duplication is higher.
- Only each crate's latest version is analyzed. Users pinned to older versions aren't modeled.

## report.json

`{ "generated_at": ..., "items": [...] }`, top 500 by impact. Each item:

| field | meaning |
|---|---|
| `rank`, `impact` | position and score (above) |
| `affected` | crates that compile `dep` twice and have `lag` in their tree |
| `sole` | affected crates where `lag` is the only blocker: upgrading fully removes the duplicate |
| `lag`, `lag_version`, `lag_d90`, `repository`, `owners`, `lag_last_release` | the crate to upgrade, and who maintains it |
| `dep`, `req`, `old_line`, `dep_latest`, `dep_line_since` | the outdated dependency, and when its latest line was first released |
| `lag_msrv`, `dep_msrv`, `msrv_bump` | `rust_version` of each. `msrv_bump: true` means upgrading raises `lag`'s MSRV. `null` means unknown |
| `top_affected` | the 10 affected crates with the most downloads: `{name, d90, sole}` |
| `co_blockers` | the 10 other crates holding the same old line in the same trees: `{name, shared}` |

## v2 idea: campaigns

Single-PR ranking undervalues crates that only help together. `jni-macros → syn 2` has 10k affected
crates but 0 where it's the sole blocker: its PR alone removes nothing, but together with
`jni-sys-macros` it clears syn 2 from those trees. This is a coordination problem, not a ranking
one, so v2 adds a campaign per old line (e.g. "syn 2"), built greedily (greedy set cover):

1. Start with every laggard on that line not yet fixed.
2. Pick the laggard whose upgrade removes the most duplicate-download weight *now*, i.e. the
   `d90(R)` of affected crates where it is the last remaining blocker. Break ties by the `impact` above.
3. Mark it fixed, recompute `k(R)` for the rest (co-blockers' shares grow, and some become sole
   blockers), and repeat.

The output is an ordered PR list with cumulative coverage, e.g. "these 8 upgrades remove syn 2
from 90% of affected download weight". It reuses the `aff` and `share` tables, and it could become a
"Campaigns" page next to the ranking. Merged PRs (from `prs.yml`) start pre-marked as fixed, so the
campaign shows remaining work.

Not visible in the dump: whether `D` appears in `L`'s public API. If it's internal (typical for
derive macros and syn), a semver-compatible release of `L` reaches everyone through `cargo update`.
If it's public, `L` needs a major bump and its dependents must upgrade too. That needs a manual
annotation in `prs.yml`.
