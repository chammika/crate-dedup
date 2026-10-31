# /// script
# requires-python = ">=3.12"
# dependencies = ["duckdb"]
# ///
"""crates.io dump dir -> report.json of crates whose old dependency lines force duplicates downstream.

Usage: uv run analyze.py <dump-data-dir> [out.json]
"""
import json
import sys
import time

import duckdb

data = sys.argv[1]
out = sys.argv[2] if len(sys.argv) > 2 else "report.json"
LIMIT, DETAIL = 500, 10  # ranked items, detail rows per item
db = duckdb.connect()
db.execute("SET preserve_insertion_order = false; SET memory_limit = '10GB'")
t0 = time.time()


def step(sql):
    db.execute(sql)
    print(f"{time.time() - t0:6.0f}s  {sql.split('AS')[0].strip()[:60]}", flush=True)


def csv(name):
    return f"read_csv('{data}/{name}.csv', header=true, all_varchar=true, max_line_size=100000000)"


# Semver-compatible "line" as a sortable int list: ^1.4 -> [1], ^0.3 -> [0,3], ^0.0.7 -> [0,0,7].
# Ranges are resolved against the dep's latest version in `edges` below.
db.execute("""
CREATE MACRO line_(ma, mi, pa) AS
  CASE WHEN ma IS NULL THEN NULL
       WHEN ma > 0 THEN [ma]
       WHEN coalesce(mi, 0) > 0 OR pa IS NULL THEN [0, coalesce(mi, 0)]
       ELSE [0, 0, pa] END;
CREATE MACRO line(v) AS line_(
  TRY_CAST(nullif(regexp_extract(v, '(\\d+)', 1), '') AS INT),
  TRY_CAST(nullif(regexp_extract(v, '\\d+\\.(\\d+)', 1), '') AS INT),
  TRY_CAST(nullif(regexp_extract(v, '\\d+\\.\\d+\\.(\\d+)', 1), '') AS INT));
-- full numeric version as int list: '0.61.2-rc1' -> [0,61,2]
CREATE MACRO ver(v) AS list_transform(string_split(regexp_extract(v, '\\d+(?:\\.\\d+)*'), '.'), lambda x: TRY_CAST(x AS INT));
""")
for req, want in [("^1.4", [1]), ("2", [2]), ("^0.3.1", [0, 3]), ("~0.0.7", [0, 0, 7]), ("0", [0, 0]), ("*", None)]:
    assert db.execute("SELECT line(?)", [req]).fetchone()[0] == want, req
assert db.execute("SELECT ver('0.61.2-rc1')").fetchone()[0] == [0, 61, 2]

# Repository URL normalized for comparison: scheme, www, case, /tree/... paths, .git and trailing slashes dropped.
db.execute("""
CREATE MACRO repo(u) AS nullif(regexp_replace(regexp_replace(regexp_replace(
  lower(trim(u)), '^https?://(www\\.)?', ''), '/(tree|blob)/.*$', ''), '(\\.git)?/*$', ''), '')
""")
for u in ["https://github.com/google/zerocopy", "https://github.com/google/zerocopy.git", "http://www.GitHub.com/google/zerocopy/",
          "https://github.com/google/zerocopy/tree/main/zerocopy-derive"]:
    assert db.execute("SELECT repo(?)", [u]).fetchone()[0] == "github.com/google/zerocopy", u

step(f"""
CREATE TABLE versions AS
SELECT id::INT id, crate_id::INT crate_id, num, rust_version, created_at::TIMESTAMPTZ created_at, line(num) ln
FROM {csv('versions')}
""")

# d90 = downloads of all versions in the last 90 days: "how much is this crate used right now".
step(f"""
CREATE TABLE crates AS
SELECT c.id::INT id, c.name, c.repository, d.downloads::BIGINT downloads, coalesce(r.d90, 0) d90, v.last_release
FROM {csv('crates')} c
JOIN {csv('crate_downloads')} d ON d.crate_id = c.id
LEFT JOIN (SELECT v.crate_id, sum(vd.downloads::BIGINT) d90
           FROM {csv('version_downloads')} vd JOIN versions v ON v.id = vd.version_id::INT GROUP BY 1) r
  ON r.crate_id = c.id::INT
LEFT JOIN (SELECT crate_id, max(created_at) last_release FROM versions GROUP BY 1) v ON v.crate_id = c.id::INT
""")

# cur_since = first release of the crate's current line (e.g. syn 3.0.0): how long the gap has existed.
step(f"""
CREATE TABLE latest AS
SELECT dv.crate_id::INT crate_id, v.id version_id, v.num, v.rust_version, v.ln cur,
       (SELECT min(w.created_at) FROM versions w WHERE w.crate_id = v.crate_id AND w.ln = v.ln) cur_since
FROM {csv('default_versions')} dv
JOIN versions v ON v.id = dv.version_id::INT
""")

# Edges from each crate's latest version. Normal deps only (kind 0); optional ones skipped.
# ponytail: ignores optional/build deps and features, so counts are a lower bound; add when needed.
# ln = the line the req resolves to: a range that admits the dep's latest version resolves to it
# (">=0.52, <0.62" with latest 0.61.2 -> 0.61); otherwise the line of the req's first version.
step(f"""
CREATE TABLE deps AS
SELECT l.crate_id src, d.crate_id::INT dst, d.req, d.kind, d.optional
FROM {csv('dependencies')} d JOIN latest l ON l.version_id = d.version_id::INT
""")

step("""
CREATE TABLE edges AS
WITH d AS (
  SELECT src, dst, req,
         nullif(regexp_extract(req, '<(=?)\\s*(\\d+(?:\\.\\d+)*)', ['eq', 'u']), {'eq': '', 'u': ''}) up
  FROM deps WHERE kind = '0' AND optional = 'f'
)
SELECT src, dst, req,
  CASE WHEN up IS NULL AND regexp_matches(req, '^\\s*>') THEN t.cur
       WHEN up IS NOT NULL AND (CASE WHEN up.eq = '=' THEN ver(t.num)[1:len(ver(up.u))] <= ver(up.u)
                                     ELSE ver(t.num) < ver(up.u) END) THEN t.cur
       ELSE line(req) END ln
FROM d JOIN latest t ON t.crate_id = d.dst
""")

# Transitive closure: (root, dep) for every crate a root pulls in (including itself).
step("""
CREATE TABLE closure AS
WITH RECURSIVE t(root, dep) AS (
  SELECT DISTINCT src, src FROM edges
  UNION
  SELECT t.root, e.dst FROM t JOIN edges e ON e.src = t.dep
) SELECT * FROM t
""")

# Laggard edge: L requires an older line of D than D's current one (one row per L, D pair).
step("""
CREATE TABLE lag AS
SELECT e.src lag_id, e.dst dep_id, min(e.req) req, min(e.ln) old, any_value(l.cur) cur
FROM edges e JOIN latest l ON l.crate_id = e.dst
WHERE e.ln IS NOT NULL AND l.cur IS NOT NULL AND e.ln < l.cur
GROUP BY ALL
""")

# Roots that pull D's current line anywhere in their tree.
step("""
CREATE TABLE cur_pulls AS
SELECT DISTINCT c.root, e.dst
FROM closure c JOIN edges e ON e.src = c.dep JOIN latest l ON l.crate_id = e.dst
WHERE e.ln = l.cur
""")

# aff: every (L, D, root) where root has L in its tree AND pulls D's current line -> root builds D twice.
step("""
CREATE TABLE aff AS
SELECT lag.lag_id, lag.dep_id, lag.old, c.root
FROM lag JOIN closure c ON c.dep = lag.lag_id
SEMI JOIN cur_pulls p ON p.root = c.root AND p.dst = lag.dep_id
""")

# k: how many laggards in root's tree hold D on that same old line. All k must upgrade to drop it.
step("""
CREATE TABLE share AS SELECT root, dep_id, old, count(*) k FROM aff GROUP BY ALL
""")

# SCORE (see README): impact(L -> D) = sum over affected roots R of d90(R) / k(R).
# A sole blocker (k = 1) earns R's full downloads; blame shared by k crates is split k ways.
step("""
CREATE TABLE score AS
SELECT a.lag_id, a.dep_id, a.old, round(sum(r.d90 / s.k))::BIGINT impact,
       count(*) affected, count(*) FILTER (s.k = 1) sole
FROM aff a JOIN share s USING (root, dep_id, old) JOIN crates r ON r.id = a.root
GROUP BY ALL
""")

step(f"""
CREATE TABLE top AS SELECT * FROM score ORDER BY impact DESC, affected DESC LIMIT {LIMIT}
""")

# Details for the expandable row: biggest affected crates, and the other crates holding the same old line.
step(f"""
CREATE TABLE top_affected AS
SELECT a.lag_id, a.dep_id,
       list({{'name': r.name, 'd90': r.d90, 'sole': s.k = 1}} ORDER BY r.d90 DESC)[1:{DETAIL}] top_affected
FROM top t JOIN aff a USING (lag_id, dep_id, old) JOIN share s USING (root, dep_id, old) JOIN crates r ON r.id = a.root
GROUP BY ALL
""")
step(f"""
CREATE TABLE co_blockers AS
SELECT lag_id, dep_id, list({{'name': c.name, 'shared': n}} ORDER BY n DESC)[1:{DETAIL}] co_blockers
FROM (SELECT a.lag_id, a.dep_id, b.lag_id other, count(*) n
      FROM top t JOIN aff a USING (lag_id, dep_id, old)
      JOIN aff b ON b.root = a.root AND b.dep_id = a.dep_id AND b.old = a.old AND b.lag_id <> a.lag_id
      GROUP BY ALL) x
JOIN crates c ON c.id = x.other
GROUP BY ALL
""")
step(f"""
CREATE TABLE owners AS
SELECT o.crate_id::INT crate_id, list(coalesce(u.username, tm.login) ORDER BY o.created_at) owners
FROM {csv('crate_owners')} o
LEFT JOIN {csv('users')} u ON o.owner_kind = '0' AND u.id = o.owner_id
LEFT JOIN {csv('teams')} tm ON o.owner_kind = '1' AND tm.id = o.owner_id
WHERE o.crate_id::INT IN (SELECT lag_id FROM top)
GROUP BY 1
""")

# MSRV of L: its own rust_version, else the one declared by its parent: a crate from the same repository
# that depends on L in any way (zerocopy -> zerocopy-derive); the parent's promise binds L. With several
# parents, the most downloaded wins: forks copy the upstream repository URL but often keep a stale MSRV.
step("""
CREATE TABLE msrv AS
SELECT t.lag_id, coalesce(lv.rust_version, p.rv) msrv, CASE WHEN lv.rust_version IS NULL THEN p.via END msrv_via
FROM (SELECT DISTINCT lag_id FROM top) t
JOIN latest lv ON lv.crate_id = t.lag_id
LEFT JOIN (
  SELECT d.dst lag_id, arg_max(pl.rust_version, pc.d90) rv, arg_max(pc.name, pc.d90) via
  FROM deps d
  JOIN crates pc ON pc.id = d.src JOIN crates lc ON lc.id = d.dst JOIN latest pl ON pl.crate_id = d.src
  WHERE d.dst IN (SELECT lag_id FROM top) AND d.src <> d.dst
    AND repo(pc.repository) = repo(lc.repository) AND pl.rust_version IS NOT NULL
  GROUP BY 1
) p USING (lag_id)
""")

rows = db.execute("""
SELECT lc.name lag, lv.num lag_version, lc.d90 lag_d90, lc.repository, ow.owners, lc.last_release::DATE lag_last_release,
       dc.name dep, lag.req, array_to_string(t.old, '.') old_line, dv.num dep_latest, dv.cur_since::DATE dep_line_since,
       m.msrv lag_msrv, m.msrv_via lag_msrv_via, dv.rust_version dep_msrv,
       ver(dv.rust_version) > ver(m.msrv) msrv_bump,
       t.impact, t.affected, t.sole,
       coalesce(ta.top_affected, []) top_affected, coalesce(cb.co_blockers, []) co_blockers
FROM top t
JOIN lag USING (lag_id, dep_id)
JOIN crates lc ON lc.id = t.lag_id
JOIN latest lv ON lv.crate_id = t.lag_id
JOIN crates dc ON dc.id = t.dep_id
JOIN latest dv ON dv.crate_id = t.dep_id
LEFT JOIN owners ow ON ow.crate_id = t.lag_id
LEFT JOIN msrv m ON m.lag_id = t.lag_id
LEFT JOIN top_affected ta USING (lag_id, dep_id)
LEFT JOIN co_blockers cb USING (lag_id, dep_id)
ORDER BY t.impact DESC, t.affected DESC
""").fetchall()
cols = [d[0] for d in db.description]
items = [dict(zip(cols, r), rank=i + 1) for i, r in enumerate(rows)]

# Ecosystem-wide numbers for the summary boxes.
stats = dict(zip(
    ["crates", "edges", "crates_with_dup", "opportunities", "data_as_of"],
    db.execute("""
SELECT (SELECT count(*) FROM latest), (SELECT count(*) FROM edges), (SELECT count(DISTINCT root) FROM aff),
       (SELECT count(*) FROM score), (SELECT max(created_at)::DATE FROM versions)
""").fetchone()))
stats["most_duplicated"] = dict(zip(["name", "crates", "lines"], db.execute("""
SELECT c.name, count(DISTINCT a.root) n, (SELECT count(DISTINCT ln) FROM edges e WHERE e.dst = a.dep_id)
FROM aff a JOIN crates c ON c.id = a.dep_id GROUP BY c.name, a.dep_id ORDER BY n DESC LIMIT 1
""").fetchone()))

with open(out, "w") as f:
    json.dump({"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "stats": stats, "items": items},
              f, default=str, separators=(",", ":"))
print(stats)
print(f"wrote {len(items)} items to {out}")
for r in items[:25]:
    print(f"{r['impact']:>14,} {r['affected']:>6} {r['sole']:>6}  {r['lag']} -> {r['dep']} {r['req']} (latest {r['dep_latest']})")
