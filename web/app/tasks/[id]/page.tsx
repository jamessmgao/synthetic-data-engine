"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { JobsTable } from "@/components/jobs-table";
import { LeakReviewPanel } from "@/components/leak-review";
import { PassPattern, QueryState, StatusBadge } from "@/components/status";
import { useApi } from "@/lib/api";
import { duration, localTime, usd } from "@/lib/format";
import type { AgentRun, Gate, TaskDetail, Version } from "@/lib/types";

export default function TaskPage() {
  const { id } = useParams<{ id: string }>();
  const detail = useApi<TaskDetail>(`/v1/tasks/${encodeURIComponent(id)}`);
  const d = detail.data;

  return (
    <div className="flex flex-col gap-6">
      <div>
        <Link href={d?.task.batch_id ? `/batches/${encodeURIComponent(d.task.batch_id)}` : "/tasks"} className="text-sm text-muted-foreground hover:underline">
          ← {d?.task.batch_id ? `Batch ${d.task.batch_id}` : "Tasks"}
        </Link>
        <h1 className="font-mono text-xl font-semibold">{id}</h1>
        {d?.task.name && <p className="text-muted-foreground">{d.task.name}</p>}
      </div>
      <QueryState error={detail.error} isLoading={detail.isLoading} />
      {d && (
        <>
          <Card>
            <CardContent className="flex flex-col gap-3">
              <div className="flex flex-wrap items-center gap-3">
                <StatusBadge value={d.task.status} />
                {d.dev && (
                  <Badge variant="secondary" title="Mock run (recorded cost 0): left out of reports by default">
                    dev
                  </Badge>
                )}
                <span className="font-medium">{d.decision}</span>
              </div>
              <div className="flex flex-wrap gap-x-6 gap-y-1 text-sm text-muted-foreground">
                <span>stage <span className="font-mono text-foreground">{d.task.stage}</span></span>
                <span>cost <span className="text-foreground">{usd(d.cost_usd)}</span></span>
                <span>created {localTime(d.task.created_at)}</span>
                <span>updated {localTime(d.task.updated_at)}</span>
              </div>
              <Spec slot={d.task.slot} />
            </CardContent>
          </Card>

          <section className="flex flex-col gap-3">
            <h2 className="font-semibold">Versions</h2>
            {[...d.versions].reverse().map((v) => (
              <VersionCard key={v.n} v={v} accepted={d.task.accepted_version === v.n} agentRuns={d.rollouts.filter((r) => r.version === v.n)} />
            ))}
          </section>

          <Card>
            <CardHeader>
              <CardTitle>Jobs</CardTitle>
              <CardDescription>Everything this task ran, in order</CardDescription>
            </CardHeader>
            <CardContent>
              <JobsTable jobs={d.jobs} />
            </CardContent>
          </Card>
        </>
      )}
    </div>
  );
}

// The task's spec (tasks.slot). Short values as chips; long ones (seed, design) folded away.
function Spec({ slot }: { slot: Record<string, unknown> }) {
  const entries = Object.entries(slot ?? {}).filter(([, v]) => v != null && v !== "");
  const short = entries.filter(([, v]) => typeof v !== "object" && String(v).length <= 80);
  const long = entries.filter(([, v]) => typeof v === "object" || String(v).length > 80);
  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap gap-1.5">
        {short.map(([k, v]) => (
          <Badge key={k} variant="outline" className="font-normal">
            <span className="text-muted-foreground">{k}</span> {String(v)}
          </Badge>
        ))}
      </div>
      {long.length > 0 && (
        <details className="text-sm">
          <summary className="cursor-pointer text-muted-foreground">Full spec ({long.map(([k]) => k).join(", ")})</summary>
          <pre className="mt-2 max-h-96 overflow-auto rounded-lg bg-muted p-3 text-xs whitespace-pre-wrap">{JSON.stringify(slot, null, 2)}</pre>
        </details>
      )}
    </div>
  );
}

function gatePassed(g: Gate): boolean | null {
  if (typeof g === "boolean") return g;
  return typeof g?.passed === "boolean" ? g.passed : null;
}

function VersionCard({ v, agentRuns, accepted }: { v: Version; agentRuns: AgentRun[]; accepted: boolean }) {
  return (
    <Card className={accepted ? "ring-2 ring-emerald-500/50" : undefined}>
      <CardHeader>
        <CardTitle className="flex flex-wrap items-center gap-2">
          v{v.n}
          <StatusBadge value={v.state} />
          {accepted && <Badge variant="success">accepted</Badge>}
          <PassPattern pattern={v.pass_pattern} />
          <span className="text-sm font-normal text-muted-foreground">{usd(v.cost_usd)}</span>
        </CardTitle>
        <CardDescription className="flex flex-wrap gap-x-4 gap-y-1">
          <span>origin: {v.origin ?? "—"}{v.operator && ` · ${v.operator}`}</span>
          {v.mutation_operators.length > 0 && <span>mutations: {v.mutation_operators.join(", ")}</span>}
          {v.repairs > 0 && <span>{v.repairs} repair{v.repairs > 1 && "s"}</span>}
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <div className="flex flex-wrap items-center gap-1.5 text-sm">
          <span className="mr-1 text-muted-foreground">Gates</span>
          {v.gates ? (
            Object.entries(v.gates).map(([name, g]) => {
              const ok = gatePassed(g);
              return (
                <Badge
                  key={name}
                  variant={ok === true ? "success" : ok === false ? "destructive" : "outline"}
                  title={typeof g === "object" ? JSON.stringify(g, null, 2) : String(g)}
                >
                  {ok === true ? "✓" : ok === false ? "✗" : "?"} {name}
                </Badge>
              );
            })
          ) : (
            <span className="text-muted-foreground">not validated yet</span>
          )}
          <span className="ml-auto flex gap-3">
            {v.bundle_sha256 && (
              <Link href={`/artifacts/bundles/${v.bundle_sha256}`} className="underline underline-offset-4">
                task files
              </Link>
            )}
            {v.world?.world_sha256 && (
              <Link href={`/artifacts/worlds/${v.world.world_sha256}`} className="underline underline-offset-4">
                world
              </Link>
            )}
          </span>
        </div>
        {(v.leak_review || v.leak_attempts.length > 0) && (
          <details open={v.leak_review?.passed === false || undefined} className="group">
            <summary className="cursor-pointer text-sm text-muted-foreground select-none">
              Leak review
              {v.leak_review && ` · ${v.leak_review.passed ? "passed" : v.leak_review.passed === false ? "failed" : "—"}`}
              {v.leak_attempts.length > 0 && ` · ${v.leak_attempts.length} earlier draft${v.leak_attempts.length > 1 ? "s" : ""}`}
            </summary>
            <div className="mt-2 flex flex-col gap-2">
              {v.leak_review && <LeakReviewPanel review={v.leak_review} title="Final bundle" />}
              {v.leak_attempts.map((a, i) => (
                <LeakReviewPanel key={a.job_id} review={a} title={`Earlier draft ${i + 1}`} />
              ))}
            </div>
          </details>
        )}
        {agentRuns.length > 0 && (
          <div className="flex flex-col divide-y rounded-lg border">
            {agentRuns.map((r) => (
              <AgentRunRow key={r.id} r={r} />
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function AgentRunRow({ r }: { r: AgentRun }) {
  const m = r.trace_metrics;
  const f = r.failure;
  return (
    <details className="group px-3 py-2 text-sm">
      <summary className="flex cursor-pointer list-none flex-wrap items-center gap-3">
        <span className="text-muted-foreground group-open:rotate-90">▸</span>
        <span className="w-24 font-mono text-xs">
          #{r.slot}
          {r.retry > 0 && ` retry ${r.retry}`}
        </span>
        <StatusBadge value={r.outcome ?? r.status} />
        {f?.attribution && (
          <Badge variant={f.attribution === "task" ? "destructive" : f.attribution === "model" ? "outline" : "warning"}>
            blamed: {f.attribution}
          </Badge>
        )}
        {f?.timeout_review && (
          <Badge variant={f.timeout_review.verdict === "model" ? "outline" : "warning"}>
            timeout: {f.timeout_review.verdict === "model" ? "model failure" : "not the model"}
            {f.timeout_review.category && ` (${f.timeout_review.category})`}
          </Badge>
        )}
        <span className="min-w-0 flex-1 truncate text-muted-foreground">{f?.explanation ?? r.error ?? ""}</span>
        <span className="text-xs text-muted-foreground tabular-nums">
          {m?.llm_calls != null && `${m.llm_calls} calls · `}
          {duration(m?.duration_seconds)}
          {m?.declared_complete && " · declared done"}
        </span>
        {r.trace_uri && (
          <Link href={`/artifacts/traces/${encodeURIComponent(r.id)}`} onClick={(e) => e.stopPropagation()} className="text-xs underline underline-offset-4">
            trace
          </Link>
        )}
      </summary>
      <div className="mt-3 flex flex-col gap-3 pl-7">
        {f?.timeout_review && f.conclusion && <p className="font-medium">{f.conclusion}</p>}
        {f?.explanation && <p>{f.explanation}</p>}
        {f?.timeout_review?.review_error && <p className="text-xs text-muted-foreground">review: {f.timeout_review.review_error}</p>}
        {r.error && <p className="text-destructive">{r.error}</p>}
        {f?.failing_tests && f.failing_tests.length > 0 && (
          <div>
            <div className="text-xs text-muted-foreground">Failing tests</div>
            <ul className="font-mono text-xs">
              {f.failing_tests.map((t) => (
                <li key={t}>{t}</li>
              ))}
            </ul>
          </div>
        )}
        {f?.pitfall_matched && <div className="text-xs">Pitfall matched: {f.pitfall_matched}</div>}
        {m && (
          <div className="flex flex-wrap gap-x-4 text-xs text-muted-foreground">
            <span>tokens in {m.input_tokens ?? "—"} / out {m.output_tokens ?? "—"}</span>
            <span>cost {usd(r.cost_usd, 3)}</span>
            {m.files_modified && <span>files modified: {m.files_modified.join(", ") || "none"}</span>}
          </div>
        )}
        {m?.tool_calls && m.tool_calls.length > 0 && (
          <details>
            <summary className="cursor-pointer text-xs text-muted-foreground">Last {m.tool_calls.length} tool calls</summary>
            <div className="mt-2 flex flex-col gap-2">
              {m.tool_calls.map((c, i) => (
                <div key={i} className="rounded-md bg-muted p-2 font-mono text-xs">
                  <div className="text-muted-foreground">{c.tool}</div>
                  <pre className="whitespace-pre-wrap">{c.input}</pre>
                  {c.output && <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap text-muted-foreground">{c.output}</pre>}
                </div>
              ))}
            </div>
          </details>
        )}
      </div>
    </details>
  );
}
