"""Which checked world a new task reuses (reuse_worlds: no new worlds are built).

A pure policy over each world's history, with no store or queue access: the caller (the
reuse_worlds mechanism) gathers, per world, the task versions built on it and how often it was
reused, and gets back the picks with their tier and the reason, to record on the task.

    select_worlds(worlds, families, count) -> [Pick(world_sha256, family, mechanism, tier, reason)]

Excluded:
  - a world with an ACCEPTED task (a new task there repeats the scenario);
  - a world whose task was rejected by the leak review (create and both repairs) under the new
    prompts: the honest guarantee of that world names its fix, whatever the wording;
  - a world whose world job or world check failed;
  - a world already reused `max_reuses` times.
Preferred, in this order (a lower tier first):
  1. near   its best task scored 4/5: one pass from the band; a new instruction or tests may land it
  2. hard   its best task scored 0/5: too hard; a clearer instruction may lift it into 1-3
  3. unscored  no task was ever scored, for a reason other than a leak (cancelled, killed, infra)
  4. old_leak  its task leaked only under the old prompts: the new symptom and textbook rules may clear it
  5. easy   every scored task passed 5/5: the world itself is easy (a mutation is its better path)
  6. last   a task on it was rejected for a leak only the blind predictor found (any prompts), or
            its defect is greedy / first-fit / a heuristic where an exact answer is needed (from
            world.json's plausible_wrong_fix and counterexample): any honest instruction for it
            names the textbook remedy (L2's finding: aws-w1-l2-t02, aws-w1-l5-t03, aws-iter2-t05)
Sharing (the user's rule: no new worlds, novelty from what each task asks; L3's reuse-distinct
makes the author ask for a different guarantee): a world with a live task of its own may still
serve while it has fewer than MAX_TASKS_SHARED tasks (so at most that many after the pick), never
twice in one call.
Domain cap: once the window has CAP_MIN_LIVE live tasks, a world is skipped if picking it would
put its domain above DOMAIN_CAP of them (every family's live tasks count). Each world has a
domain (ledger/payments, scheduling/planning, replication, …) from its name, see `domain`.
Order: the requested families round-robin; within a family, a world that repeats nothing live
first (neither its own task nor another world's domain + mechanism is live), then the least-
represented domain (live and picked, across tiers), then the best tier, then the least-used
mechanism (counting this call's own picks), then the least-reused world, then the world sha.
Too few eligible worlds is the caller's to refuse: an accepted, leaked or capped world never serves.

`world_uses(conn)` reads that history from the jobs: a version's world is the world_sha256 of its
create / revise / world_check inputs, its pass count the VALID_PASS of its five scored agent runs.
"""

import json
from dataclasses import dataclass, field
from typing import Any

TIERS = ("near", "hard", "unscored", "old_leak", "easy", "last")
GREEDY_WORDS = ("greedy", "first-fit", "first fit", "firstfit", "heuristic")
MAX_REUSES = 2
# A world whose own task is live serves only while it has fewer tasks than this (manager, 22:50).
MAX_TASKS_SHARED = 2
# No domain above this share of the window's live tasks (once there are CAP_MIN_LIVE of them):
# 13 of 46 in-flight tasks were ledgers at 22:45 (L3's finding).
DOMAIN_CAP = 0.20
CAP_MIN_LIVE = 10
# Window 1's prompts went live with the generation pods of 2026-11-18 ~20:48 UTC (release
# 5e1073985063); a version whose create started earlier was authored under the old prompts.
NEW_PROMPTS_SINCE = "2026-11-18T20:48"

# A world's domain from the words of its name, the first rule that matches (the pool's names
# on 2026-11-18: e.g. durable-quorum-ledger is replication, capacity-prerequisite-planner
# scheduling, quota-charge-retry allocation).
DOMAINS = (
    ("compiler/interpreter", ("alias", "minilang", "inliner", "parse", "evaluator", "template", "let",
                              "folding", "optimizer", "bindings", "rule", "lexer", "bytecode", "interpreter")),
    ("replication/consensus", ("replicated", "replica", "quorum", "failover", "linearizability", "raft", "kv")),
    ("scheduling/planning", ("scheduler", "planner", "schedule", "interval", "commitment", "duty", "coverage",
                             "roster", "shift", "calendar")),
    ("allocation/provisioning", ("alloc", "allocator", "allocation", "provisioning", "quota", "capacity",
                                 "assignment", "matching", "pool", "lease", "inventory")),
    ("booking/reservation", ("booking", "reservation", "seat", "ticket")),
    ("ledger/payments", ("ledger", "charge", "payment", "settlement", "credit", "billing", "reconciler",
                         "invoice", "refund", "wallet", "bank", "account", "transfer", "balance", "debit")),
    ("orders/workflow", ("order", "workflow", "dispatch", "compensation", "saga", "pipeline")),
    ("cache/storage", ("cache", "storage", "snapshot", "compaction", "index", "log")),
)
OTHER = "other"


def domain(name: str) -> str:
    """The domain of a world named `name` (DOMAINS), or "other"."""
    words = set(name.lower().replace("_", "-").split("-"))
    for label, keys in DOMAINS:
        if words & set(keys):
            return label
    return OTHER


@dataclass
class Use:
    """One task version built on the world."""
    pass_count: int | None = None   # 0-5 when all five agent runs were scored, else None
    accepted: bool = False          # the version was ACCEPTED in band
    leak_rejected: bool = False     # rejected because create and both repairs failed the leak review
    new_prompts: bool = True        # authored under the current prompt set (window 1+)
    live: bool = False              # its task is in flight or accepted, and the version is window 1+
    blind_leak: bool = False        # leak-rejected while the semantic review was clean (the blind one failed it)
    task: str = ""                  # the task the version belongs to


@dataclass
class World:
    sha: str
    family: str
    mechanism: str
    uses: list[Use] = field(default_factory=list)
    reuses: int = 0                 # tasks already started on it by reuse_worlds
    failed: bool = False            # its world job or world check failed
    name: str = ""                  # world.json's name (the world job's result)
    greedy: bool = False            # its defect is greedy / first-fit / heuristic (greedy_defect)

    @property
    def domain(self) -> str:
        return domain(self.name)

    @property
    def tasks(self) -> int:
        """Distinct tasks built on it (a use without a task id counts as its own task)."""
        return len({u.task or f"#{i}" for i, u in enumerate(self.uses)})


@dataclass
class Pick:
    world_sha256: str
    family: str
    mechanism: str
    tier: str
    reason: str


def tier(world: World, max_reuses: int = MAX_REUSES) -> tuple[str | None, str]:
    """(tier, reason), or (None, why it is excluded)."""
    if world.failed:
        return None, "its world job or world check failed"
    if any(u.accepted for u in world.uses):
        return None, "it already has an accepted task (a new one would repeat the scenario)"
    if any(u.leak_rejected and u.new_prompts for u in world.uses):
        return None, "its task was rejected by the leak review under the current prompts"
    if world.reuses >= max_reuses:
        return None, f"already reused {world.reuses} times (cap {max_reuses})"
    if any(u.blind_leak for u in world.uses):
        return "last", "a task on it was rejected for a leak only the blind predictor found"
    if world.greedy:
        return "last", "its defect is greedy / first-fit / a heuristic (the remedy is the textbook one)"
    counts = [u.pass_count for u in world.uses if u.pass_count is not None]
    if counts:
        if 4 in counts:
            return "near", f"its best task scored 4/5 (scores {counts})"
        if 0 in counts:
            return "hard", f"a task scored 0/5 (scores {counts})"
        if all(c == 5 for c in counts):
            return "easy", f"every scored task passed 5/5 (scores {counts})"
    if any(u.leak_rejected for u in world.uses):
        return "old_leak", "its task leaked only under the old prompts"
    if not counts:
        return "unscored", "no task on it was ever scored (not for a leak)"
    return "easy", f"scores {counts}"


def select_worlds(worlds: list[World], families: list[str], count: int,
                  max_reuses: int = MAX_REUSES, domain_cap: float = DOMAIN_CAP) -> list[Pick]:
    """Up to `count` worlds for new tasks in `families`, families round-robin. `worlds` is the
    whole pool (every family's live tasks count toward the domain cap); only `families` are picked."""
    rank = {t: i for i, t in enumerate(TIERS)}
    by_family: dict[str, list[tuple[str, str, World]]] = {f: [] for f in families}
    for w in worlds:
        if w.family not in by_family:
            continue
        t, why = tier(w, max_reuses)
        if t is not None:
            by_family[w.family].append((t, why, w))
    mechanism_uses: dict[tuple[str, str], int] = {}
    domain_live: dict[str, int] = {}
    scenario_live: set[tuple[str, str]] = set()
    for w in worlds:
        key = (w.family, w.mechanism)
        mechanism_uses[key] = mechanism_uses.get(key, 0) + len(w.uses) + w.reuses
        live = sum(u.live for u in w.uses)
        if live:
            domain_live[w.domain] = domain_live.get(w.domain, 0) + live
            scenario_live.add((w.domain, w.mechanism))

    def own_live(w: World) -> bool:
        return any(u.live for u in w.uses)

    def repeats(w: World) -> bool:
        return own_live(w) or (w.domain != OTHER and (w.domain, w.mechanism) in scenario_live)

    def eligible(w: World) -> bool:
        if own_live(w) and w.tasks >= MAX_TASKS_SHARED:
            return False
        if w.domain == OTHER:
            return True
        total = sum(domain_live.values())
        # The domain cap: once the window has CAP_MIN_LIVE live tasks, no domain past `domain_cap`.
        return not (total >= CAP_MIN_LIVE and (domain_live.get(w.domain, 0) + 1) / (total + 1) > domain_cap)

    picks: list[Pick] = []
    taken: set[str] = set()
    while len(picks) < count:
        progressed = False
        for family in families:
            if len(picks) >= count:
                break
            pool = [c for c in by_family[family] if c[2].sha not in taken and eligible(c[2])]
            if not pool:
                continue
            # Non-repeats first; then the least-represented domain, across tiers; then the best tier.
            pool.sort(key=lambda c: (repeats(c[2]), domain_live.get(c[2].domain, 0), rank[c[0]],
                                     mechanism_uses[(family, c[2].mechanism)], c[2].reuses, c[2].sha))
            t, why, w = pool[0]
            if own_live(w):
                why = f"{why}; shares the world with a live task ({w.tasks} so far)"
            elif repeats(w):
                why = f"{why}; repeats a live {w.domain} + {w.mechanism} scenario"
            picks.append(Pick(w.sha, w.family, w.mechanism, t, f"{why}; domain {w.domain}"))
            taken.add(w.sha)
            mechanism_uses[(family, w.mechanism)] += 1
            domain_live[w.domain] = domain_live.get(w.domain, 0) + 1
            scenario_live.add((w.domain, w.mechanism))
            progressed = True
        if not progressed:
            break
    return picks


def _input(row: Any) -> dict[str, Any]:
    v = row["input"]
    return v if isinstance(v, dict) else json.loads(v or "{}")


def world_uses(conn: Any) -> dict[str, list[Use]]:
    """Every world's task versions so far, as Use records, keyed by world sha."""
    world: dict[tuple[str, int], str] = {}
    authoring: dict[tuple[str, int], list[tuple[str, str | None, bool]]] = {}
    for r in conn.execute("SELECT task_id, version, type, input, outcome, result, created_at FROM jobs"
                          " WHERE type IN ('create', 'revise', 'world_check') ORDER BY created_at, id").fetchall():
        key = (r["task_id"], r["version"])
        sha = _input(r).get("world_sha256")
        if sha:
            world.setdefault(key, sha)
        if r["type"] != "world_check":
            res = r["result"] if isinstance(r["result"], dict) else json.loads(r["result"] or "{}")
            leak = (res or {}).get("leak") or {}
            blind_only = leak.get("passed") is False and leak.get("verdict") == "clean" and bool(leak.get("blind"))
            authoring.setdefault(key, []).append((r["created_at"], r["outcome"], blind_only))
    scored: dict[tuple[str, int], list[bool]] = {}
    for r in conn.execute("SELECT task_id, version, outcome FROM jobs WHERE type = 'rollout'"
                          " AND outcome IN ('VALID_PASS', 'VALID_FAIL')").fetchall():
        scored.setdefault((r["task_id"], r["version"]), []).append(r["outcome"] == "VALID_PASS")
    tasks = {r["id"]: r for r in conn.execute(
        "SELECT id, status, reason, current_version, accepted_version FROM tasks").fetchall()}
    uses: dict[str, list[Use]] = {}
    for (tid, n), sha in sorted(world.items()):
        task = tasks.get(tid)
        if task is None:
            continue
        runs = scored.get((tid, n), [])
        jobs = authoring.get((tid, n), [])
        # Rejected as invalid at this version with its last create / revise failed: no draft got
        # past the leak review (or the author) on a world that had passed its check.
        leak = (task["status"] == "REJECTED" and task["reason"] == "invalid" and task["current_version"] == n
                and bool(jobs) and jobs[-1][1] == "FAILED")
        new_prompts = bool(jobs) and jobs[0][0] >= NEW_PROMPTS_SINCE
        uses.setdefault(sha, []).append(Use(
            pass_count=sum(runs) if len(runs) == 5 else None,
            accepted=task["status"] == "ACCEPTED" and task["accepted_version"] == n,
            leak_rejected=leak,
            new_prompts=new_prompts,
            live=new_prompts and task["status"] not in ("REJECTED", "CANCELLED")
            and (task["status"] != "ACCEPTED" or task["accepted_version"] == n),
            blind_leak=leak and jobs[-1][2],
            task=tid))
    return uses


def greedy_defect(world_json: dict[str, Any]) -> bool:
    """The world's defect is greedy / first-fit / a heuristic: its plausible_wrong_fix or
    counterexample says so."""
    text = json.dumps([world_json.get("plausible_wrong_fix"), world_json.get("counterexample")]).lower()
    return any(w in text for w in GREEDY_WORDS)


def greedy_worlds(artifacts: Any, shas: list[str]) -> set[str]:
    """The worlds among `shas` with a greedy defect, from each one's world.json (one small file
    per world, cached on local disk by the artifact store). A world.json that can't be read
    counts as not greedy."""
    found = set()
    for sha in shas:
        try:
            rel = f"worlds/{sha}/world.json"
            if artifacts.fetch_file(rel) and greedy_defect(json.loads((artifacts.root / rel).read_text())):
                found.add(sha)
        except (OSError, ValueError, AttributeError):
            continue
    return found


def world_names(conn: Any) -> dict[str, str]:
    """world sha -> world.json's name, from the world jobs' results."""
    names: dict[str, str] = {}
    for r in conn.execute("SELECT result FROM jobs WHERE type = 'world' AND outcome = 'OK'").fetchall():
        res = r["result"] if isinstance(r["result"], dict) else json.loads(r["result"] or "{}")
        if res.get("world_sha256") and res.get("name"):
            names[res["world_sha256"]] = res["name"]
    return names


def diversity_summary(conn: Any, since: str = NEW_PROMPTS_SINCE, batch_prefix: str = "") -> str:
    """Tasks created since `since` (in batches starting with `batch_prefix`): how many per family,
    mechanism and domain, how many on a reused world, and the domain + mechanism pairs that repeat."""
    names = world_names(conn)
    world: dict[tuple[str, int], str] = {}
    for r in conn.execute("SELECT task_id, version, input FROM jobs WHERE type IN ('create', 'revise', 'world_check')"
                          " ORDER BY created_at, id").fetchall():
        sha = _input(r).get("world_sha256")
        if sha:
            world.setdefault((r["task_id"], r["version"]), sha)
    rows = conn.execute("SELECT id, slot, status, current_version FROM tasks WHERE created_at >= ?"
                        " ORDER BY id", (since,)).fetchall()
    fam, mech, dom, pairs = {}, {}, {}, {}
    reused = 0
    n = 0
    for r in rows:
        if not r["id"].startswith(batch_prefix):
            continue
        n += 1
        slot = r["slot"] if isinstance(r["slot"], dict) else json.loads(r["slot"] or "{}")
        sha = slot.get("reused_world") or world.get((r["id"], 1)) or world.get((r["id"], r["current_version"]))
        d = domain(names.get(sha, "")) if sha else "(world not built yet)"
        f, m = slot.get("family", "?"), slot.get("mechanism", "?")
        reused += "reused_world" in slot
        for counter, key in ((fam, f), (mech, f"{f}/{m}"), (dom, d), (pairs, f"{d} + {m}")):
            counter[key] = counter.get(key, 0) + 1

    def line(c: dict[str, int]) -> str:
        return ", ".join(f"{k} {v}" for k, v in sorted(c.items(), key=lambda kv: (-kv[1], kv[0])))

    repeats = {k: v for k, v in pairs.items() if v > 1 and not k.startswith((OTHER, "(world"))}
    return "\n".join([
        f"tasks since {since}{f' in {batch_prefix}*' if batch_prefix else ''}: {n} ({reused} on a reused world)",
        f"families ({len(fam)}): {line(fam)}",
        f"mechanisms ({len(mech)}): {line(mech)}",
        f"domains ({len(dom)}): {line(dom)}",
        f"repeated domain + mechanism: {line(repeats) or 'none'}"])


if __name__ == "__main__":
    import sys

    from shared.storage import from_env

    with from_env().store.tx() as c:
        print(diversity_summary(c, *sys.argv[1:3]))
