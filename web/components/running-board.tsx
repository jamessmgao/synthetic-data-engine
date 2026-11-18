"use client";

import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { cn } from "cn";
import { Badge } from "@/components/ui/badge";
import { JobsTable, jobTypeLabel } from "@/components/jobs-table";
import { useApi } from "@/lib/api";
import { between, short, utcTime } from "@/lib/format";
import type { Job, TaskSummary } from "@/lib/types";

// "Running now" as a kanban: one card per task version per stage, columns in pipeline order.
// Everything comes from /v1/queue's leased jobs; the only extra request is /v1/tasks for the
// tasks in calibration, and only while agent runs are leased, for the ✓/✗ of finished runs.

type Stage = { key: string; label: string; types: string[] };

const STAGES: Stage[] = [
  { key: "world", label: "World", types: ["world", "world_check"] },
  { key: "author", label: "Author task", types: ["create"] },
  { key: "validate", label: "Validate", types: ["validate"] },
  { key: "runs", label: "Agent runs", types: ["rollout"] },
  { key: "analyze", label: "Analyze", types: ["analyze"] },
  { key: "revision", label: "Revision", types: ["revise"] },
];
const OTHER: Stage = { key: "other", label: "Other", types: [] };
// Minutes after which a job is flagged "possibly stuck" (a heuristic; world_check and revise have none).
const STUCK_MIN: Record<string, number> = { world: 45, create: 45, validate: 20, rollout: 70, analyze: 15 };

const stageOf = (type: string) => STAGES.find((s) => s.types.includes(type)) ?? OTHER;

// The lane of a task, from its batch prefix: aws-w1-l3-t02 → l3, aws-iter2-t05 → iter2,
// aws-agent-b1-t07 → agent-b1 (task ids are `<batch_id>-tNN`, api/submit.py).
export function laneOf(taskId: string): string {
  const batch = taskId.replace(/-t\d+$/, "");
  return batch.replace(/^aws-/, "").replace(/^w\d+-/, "") || batch;
}

// The pod's short id: "generation-84dd57877-snzsh:1:revise+create+world:5" → "snzsh".
export function workerShort(owner: string | null): string {
  if (!owner) return "—";
  const pod = owner.split(":")[0];
  return pod.split("-").pop() || pod;
}

// A deploy (or a Spot interruption) hands in-flight jobs back with "released at …"
// (shared/jobs/worker.py release_in_flight); a LEASED job with that error was re-claimed since.
const isRelease = (error: string | null) => !!error && error.toLowerCase().startsWith("released");

// NOTE: started_at is the job's FIRST start: the claim keeps it (`COALESCE(started_at, ?)` in
// shared/storage/sql.py), so a restarted job's time overstates how long this attempt has run.
function stuckReason(j: Job, now: number): string | null {
  const limit = STUCK_MIN[j.type];
  const s = between(j.started_at, null, now);
  if (limit == null || s == null || s <= limit * 60) return null;
  const first = isRelease(j.error) || j.attempts > 1 ? " since first start (restarted; this attempt may be shorter)" : "";
  return `${jobTypeLabel(j.type)} has run ${short(s)}${first}, over the ${limit} min expected`;
}

type CardData = {
  key: string;
  taskId: string;
  version: number;
  stage: Stage;
  jobs: Job[];
  since: string | null; // the earliest started_at of its jobs
  seconds: number | null;
  stuck: string[];
  restarts: number; // re-claims after a release (attempts − 1), 0 if none known
  restarted: boolean;
};

function buildCards(jobs: Job[], now: number): CardData[] {
  const by = new Map<string, Job[]>();
  for (const j of jobs) {
    const k = `${j.task_id}|${j.version}|${stageOf(j.type).key}`;
    by.set(k, [...(by.get(k) ?? []), j]);
  }
  return [...by.entries()].map(([key, js]) => {
    const since = js.map((j) => j.started_at).filter((t): t is string => !!t).sort()[0] ?? null;
    const released = js.filter((j) => isRelease(j.error));
    return {
      key,
      taskId: js[0].task_id,
      version: js[0].version,
      stage: stageOf(js[0].type),
      jobs: [...js].sort((a, b) => a.slot - b.slot || a.id.localeCompare(b.id)),
      since,
      seconds: between(since, null, now),
      stuck: js.map((j) => stuckReason(j, now)).filter((r): r is string => !!r),
      restarts: Math.max(0, ...released.map((j) => j.attempts - 1)),
      restarted: released.length > 0,
    };
  });
}

// Normal cards by time in stage, longest first, then the possibly stuck ones (the user wants them last).
function sortCards(cards: CardData[]): CardData[] {
  return [...cards].sort((a, b) => Number(a.stuck.length > 0) - Number(b.stuck.length > 0) || (b.seconds ?? 0) - (a.seconds ?? 0));
}

// --- filters, kept in the URL query string (like the artifact viewer's ?path=) ---

function useFilters() {
  const params = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const set = (changes: Record<string, string | null>) => {
    const next = new URLSearchParams(params.toString());
    for (const [k, v] of Object.entries(changes)) {
      if (v) next.set(k, v);
      else next.delete(k);
    }
    const qs = next.toString();
    router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
  };
  return {
    view: params.get("view") === "table" ? "table" : "board",
    lanes: (params.get("lane") ?? "").split(",").filter(Boolean),
    type: params.get("type") ?? "",
    q: params.get("q") ?? "",
    set,
  };
}

export function RunningNow({ jobs, now }: { jobs: Job[]; now: number }) {
  const f = useFilters();
  const lanes = [...new Set(jobs.map((j) => laneOf(j.task_id)))].sort();
  const types = [...new Set(jobs.map((j) => j.type))].sort(
    (a, b) => STAGES.indexOf(stageOf(a)) - STAGES.indexOf(stageOf(b)) || a.localeCompare(b),
  );
  const shown = jobs.filter(
    (j) =>
      (f.lanes.length === 0 || f.lanes.includes(laneOf(j.task_id))) &&
      (!f.type || j.type === f.type) &&
      (!f.q || j.task_id.toLowerCase().includes(f.q.toLowerCase())),
  );
  const toggleLane = (l: string) => {
    const next = f.lanes.includes(l) ? f.lanes.filter((x) => x !== l) : [...f.lanes, l];
    f.set({ lane: next.join(",") || null });
  };

  if (jobs.length === 0) return <p className="text-sm text-muted-foreground">Nothing running.</p>;

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <div className="inline-flex gap-0.5 rounded-lg bg-muted p-0.5" role="group" aria-label="Board or table">
          {(["board", "table"] as const).map((v) => (
            <button
              key={v}
              type="button"
              onClick={() => f.set({ view: v === "board" ? null : v })}
              aria-pressed={f.view === v}
              className={cn(
                "rounded-md px-2.5 py-1 text-xs font-medium",
                f.view === v ? "bg-background shadow-sm ring-1 ring-foreground/10" : "text-muted-foreground hover:text-foreground",
              )}
            >
              {v === "board" ? "Board" : "Table"}
            </button>
          ))}
        </div>
        <select
          value={f.type}
          onChange={(e) => f.set({ type: e.target.value || null })}
          aria-label="Job type"
          className="h-8 rounded-md border bg-background px-2 text-sm"
        >
          <option value="">All job types</option>
          {types.map((t) => (
            <option key={t} value={t}>
              {jobTypeLabel(t)}
            </option>
          ))}
        </select>
        <input
          value={f.q}
          onChange={(e) => f.set({ q: e.target.value || null })}
          placeholder="Search task id"
          aria-label="Search task id"
          className="h-8 w-full min-w-0 rounded-md border bg-transparent px-2 text-sm sm:w-56"
        />
      </div>
      {lanes.length > 1 && (
        <div className="flex flex-wrap items-center gap-1.5" role="group" aria-label="Lanes">
          <span className="text-xs text-muted-foreground">Lanes</span>
          {lanes.map((l) => (
            <button
              key={l}
              type="button"
              onClick={() => toggleLane(l)}
              aria-pressed={f.lanes.includes(l)}
              className={cn(
                "h-6 rounded-full border px-2 font-mono text-xs",
                f.lanes.includes(l) ? "border-transparent bg-primary text-primary-foreground" : "text-muted-foreground hover:text-foreground",
              )}
            >
              {l} <span className="tabular-nums opacity-70">{jobs.filter((j) => laneOf(j.task_id) === l).length}</span>
            </button>
          ))}
          {f.lanes.length > 0 && (
            <button type="button" onClick={() => f.set({ lane: null })} className="text-xs text-muted-foreground underline-offset-4 hover:underline">
              clear
            </button>
          )}
        </div>
      )}
      {f.view === "table" ? (
        <JobsTable jobs={shown} showTask empty="No running job matches the filters." />
      ) : (
        <Board jobs={shown} now={now} />
      )}
    </div>
  );
}

function Board({ jobs, now }: { jobs: Job[]; now: number }) {
  const cards = buildCards(jobs, now);
  // The ✓/✗ of finished agent runs: one request for the tasks in calibration, only while any agent run is leased.
  const hasRuns = jobs.some((j) => j.type === "rollout");
  const summaries = useApi<TaskSummary[]>(hasRuns ? "/v1/tasks?status=CALIBRATING" : null);
  const byId = new Map((summaries.data ?? []).map((t) => [t.id, t]));
  const columns = [...STAGES, ...(cards.some((c) => c.stage === OTHER) ? [OTHER] : [])];

  return (
    <div className={cn("grid grid-cols-1 gap-3 md:grid-cols-3", columns.length > 6 ? "xl:grid-cols-7" : "xl:grid-cols-6")}>
      {columns.map((s) => {
        const col = sortCards(cards.filter((c) => c.stage === s));
        return (
          <section key={s.key} className="flex min-w-0 flex-col gap-2 rounded-lg bg-muted/40 p-2" aria-label={s.label}>
            <h3 className="flex items-baseline justify-between px-1 text-xs font-medium text-muted-foreground">
              <span>{s.label}</span>
              <span className={cn("tabular-nums", col.length > 0 && "font-semibold text-sky-600 dark:text-sky-400")}>{col.length}</span>
            </h3>
            {col.length === 0 && <p className="px-1 pb-1 text-xs text-muted-foreground/60">—</p>}
            {col.map((c) => (
              <TaskCard key={c.key} card={c} now={now} summary={byId.get(c.taskId)} />
            ))}
          </section>
        );
      })}
    </div>
  );
}

function TaskCard({ card: c, now, summary }: { card: CardData; now: number; summary?: TaskSummary }) {
  const origins = [...new Set(c.jobs.map((j) => j.origin).filter((o): o is string => !!o && o !== "initial"))];
  const kinds = [...new Set(c.jobs.map((j) => j.type))];
  const errors = c.jobs.filter((j) => j.error && !isRelease(j.error));
  const stuck = c.stuck.length > 0;
  return (
    <div
      className={cn(
        "flex min-w-0 flex-col gap-1.5 rounded-lg bg-card p-2.5 text-card-foreground ring-1 ring-foreground/10",
        stuck && "ring-amber-500/40",
      )}
    >
      <div className="flex items-start justify-between gap-2">
        <Link
          href={`/tasks/${encodeURIComponent(c.taskId)}`}
          className="min-w-0 font-mono text-xs font-medium break-all underline-offset-4 hover:underline"
          title={c.taskId}
        >
          {c.taskId}
        </Link>
        <span className="shrink-0 rounded bg-muted px-1.5 font-mono text-[11px] text-muted-foreground">{laneOf(c.taskId)}</span>
      </div>
      <div className="flex items-baseline justify-between gap-2 text-xs text-muted-foreground">
        <span className="min-w-0 truncate">
          v{c.version}
          {c.stage.types.length !== 1 || c.stage === OTHER ? ` · ${kinds.map(jobTypeLabel).join(", ")}` : ""}
          {origins.length > 0 && ` · ${origins.join(", ")}`}
        </span>
        <span
          className="shrink-0 tabular-nums text-foreground"
          title={`In stage since ${utcTime(c.since)}${c.restarted ? " (first start; restarted since)" : ""}`}
        >
          {short(c.seconds)}
        </span>
      </div>
      {c.stage.key === "runs" && <RunStrip jobs={c.jobs} version={c.version} summary={summary} />}
      {(c.restarted || stuck) && (
        <div className="flex flex-wrap gap-1">
          {c.restarted && (
            <Badge variant="secondary" className="text-muted-foreground" title={`Handed back by a deploy and re-claimed${c.restarts > 0 ? ` (${c.restarts + 1} claims)` : ""}; times are since the first start`}>
              {c.restarts > 1 ? `restarted ×${c.restarts}` : "restarted (deploy)"}
            </Badge>
          )}
          {stuck && (
            <Badge variant="warning" title={c.stuck.join("\n")}>
              possibly stuck
            </Badge>
          )}
        </div>
      )}
      <ul className="flex flex-col gap-0.5 text-[11px] text-muted-foreground">
        {c.jobs.map((j) => (
          <li key={j.id} className="flex min-w-0 items-baseline gap-1.5" title={j.id}>
            {j.type === "rollout" && <span className="font-mono">#{j.slot}{j.retry ? `r${j.retry}` : ""}</span>}
            <span className="truncate font-mono" title={j.lease_owner ?? ""}>
              {workerShort(j.lease_owner)}
            </span>
            {c.jobs.length > 1 && (
              <span className="tabular-nums" title={utcTime(j.started_at)}>
                {short(between(j.started_at, null, now))}
              </span>
            )}
            <span className="ml-auto flex shrink-0 gap-1.5">
              {j.trace_uri && (
                <Link href={`/artifacts/traces/${encodeURIComponent(j.id)}`} className="underline underline-offset-4 hover:text-foreground">
                  trace
                </Link>
              )}
              {j.bundle_sha256 && (
                <Link href={`/artifacts/bundles/${j.bundle_sha256}`} className="underline underline-offset-4 hover:text-foreground">
                  bundle
                </Link>
              )}
            </span>
          </li>
        ))}
      </ul>
      {errors.map((j) => (
        <div key={j.id} className="truncate text-xs text-destructive" title={j.error ?? ""}>
          {j.error}
        </div>
      ))}
    </div>
  );
}

// One mark per agent run slot: ✓ / ✗ finished (from the task summary's pass_pattern, when it is
// for this version), ● running now, ○ not started or queued.
function RunStrip({ jobs, version, summary }: { jobs: Job[]; version: number; summary?: TaskSummary }) {
  const pattern = summary && summary.current_version === version ? summary.pass_pattern : null;
  const running = new Set(jobs.map((j) => j.slot));
  const n = Math.max(pattern?.length ?? 5, ...jobs.map((j) => j.slot));
  const marks = Array.from({ length: n }, (_, i) => {
    const p = pattern?.[i];
    if (running.has(i + 1)) return "●";
    return p === "✓" || p === "✗" ? p : "○";
  });
  const done = marks.filter((m) => m === "✓" || m === "✗").length;
  return (
    <div className="flex items-center gap-2">
      <span className="inline-flex gap-0.5 font-mono text-xs" aria-label={marks.join("")}>
        {marks.map((m, i) => (
          <span
            key={i}
            title={`run #${i + 1}: ${m === "●" ? "running" : m === "✓" ? "passed" : m === "✗" ? "failed" : "not started"}`}
            className={cn(
              "inline-flex size-4 items-center justify-center rounded",
              m === "✓" && "bg-emerald-500/15 text-emerald-700 dark:text-emerald-400",
              m === "✗" && "bg-rose-500/15 text-rose-700 dark:text-rose-400",
              m === "●" && "bg-sky-500/15 text-sky-700 dark:text-sky-400",
              m === "○" && "bg-muted text-muted-foreground",
            )}
          >
            {m}
          </span>
        ))}
      </span>
      <span className="text-[11px] text-muted-foreground">
        {running.size} running{pattern ? ` · ${done} scored` : ""}
      </span>
    </div>
  );
}
