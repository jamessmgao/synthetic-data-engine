"use client";

import Link from "next/link";
import { Suspense } from "react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { BatchesTable } from "@/components/batches-table";
import { OtherScopeNote, scopeQuery, useScope } from "@/components/dev-toggle";
import { jobTypeLabel } from "@/components/jobs-table";
import { RunningNow } from "@/components/running-board";
import { QueryState, Stat } from "@/components/status";
import { ApiError, useApi } from "@/lib/api";
import { duration, pct, usd } from "@/lib/format";
import type { BatchSummary, JobStatus, Queue, Report, TaskSummary } from "@/lib/types";

// The pipeline's job types in flow order (README "The flow").
const JOB_TYPES = ["world", "world_check", "create", "revise", "validate", "rollout", "analyze"];
const JOB_STATUSES: JobStatus[] = ["READY", "LEASED", "DONE", "DEAD"];

export default function Overview() {
  const [scope] = useScope();
  const report = useApi<Report>(`/v1/report${scopeQuery(scope)}`);
  const queue = useApi<Queue>("/v1/queue");
  const batches = useApi<BatchSummary[]>("/v1/batches");
  const tasks = useApi<TaskSummary[]>("/v1/tasks");
  const noTasks = report.error instanceof ApiError && report.error.status === 404;
  const r = report.data;

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Overview</h1>
          <p className="text-sm text-muted-foreground">Every task the pipeline has. Refreshes every 10 s.</p>
        </div>
        <OtherScopeNote scope={scope} other={r?.other_scope} />
      </div>

      {!noTasks && <QueryState error={report.error} isLoading={report.isLoading} />}
      {r && (
        <div className="grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-6">
          <Stat label="Tasks" value={r.candidates} hint={`${r.in_progress} in progress`} />
          <Stat label="Accepted" value={r.accepted_total} hint={`${r.accepted_v1} at v1`} />
          <Stat
            label="Rejected"
            value={Object.values(r.rejected).reduce((a, b) => a + b, 0)}
            hint={Object.entries(r.rejected).map(([k, v]) => `${v} ${k}`).join(", ") || "none"}
          />
          <Stat label="Learnable rate" value={pct(r.yield.learnable_rate)} hint="accepted / finished" />
          <Stat label="Cost" value={usd(r.cost_usd.total)} hint={`${usd(r.cost_usd.per_accepted)} per accepted`} />
          <Stat label="Agent runs" value={r.valid_rollouts} hint={`${r.invalid_rollouts} invalid · ${duration(r.wall_clock_s)} wall`} />
        </div>
      )}
      {noTasks && <p className="text-sm text-muted-foreground">No tasks yet.</p>}

      <ByFamily tasks={(tasks.data ?? []).filter((t) => t.dev === (scope === "dev"))} />

      <Card>
        <CardHeader>
          <CardTitle>Running now</CardTitle>
          <CardDescription>
            {queue.data
              ? `${queue.data.running.length} leased job${queue.data.running.length === 1 ? "" : "s"}, one card per task and stage`
              : "Leased jobs"}
          </CardDescription>
        </CardHeader>
        <CardContent>
          {queue.data && (
            <Suspense fallback={null}>
              <RunningNow jobs={queue.data.running} now={queue.dataUpdatedAt} />
            </Suspense>
          )}
        </CardContent>
      </Card>

      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,2fr)]">
        <Card>
          <CardHeader>
            <CardTitle>Queues</CardTitle>
            <CardDescription>Jobs by type and status</CardDescription>
          </CardHeader>
          <CardContent>
            <QueryState error={queue.error} isLoading={queue.isLoading} />
            {queue.data && (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Type</TableHead>
                    {JOB_STATUSES.map((s) => (
                      <TableHead key={s} className="text-right">
                        {s === "LEASED" ? "RUNNING" : s}
                      </TableHead>
                    ))}
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {[...JOB_TYPES, ...Object.keys(queue.data.counts).filter((t) => !JOB_TYPES.includes(t))]
                    .filter((t) => queue.data.counts[t])
                    .map((t) => (
                      <TableRow key={t}>
                        <TableCell className="font-mono text-xs">{jobTypeLabel(t)}</TableCell>
                        {JOB_STATUSES.map((s) => {
                          const n = queue.data.counts[t]?.[s] ?? 0;
                          return (
                            <TableCell
                              key={s}
                              className={`text-right tabular-nums ${n === 0 ? "text-muted-foreground/50" : s === "DEAD" ? "text-destructive" : s === "LEASED" ? "font-semibold text-sky-600 dark:text-sky-400" : ""}`}
                            >
                              {n}
                            </TableCell>
                          );
                        })}
                      </TableRow>
                    ))}
                </TableBody>
              </Table>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Recent batches</CardTitle>
            <CardDescription>
              <Link href="/batches" className="underline-offset-4 hover:underline">
                All batches →
              </Link>
            </CardDescription>
          </CardHeader>
          <CardContent>
            <QueryState error={batches.error} isLoading={batches.isLoading} />
            {batches.data && <BatchesTable batches={batches.data.filter((b) => b.dev === (scope === "dev")).slice(0, 8)} />}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}

// One row per family: its tasks, how they ended, and the yield (accepted / decided). Each row
// opens the tasks list filtered to that family. Nothing to show on an API without families.
function ByFamily({ tasks }: { tasks: TaskSummary[] }) {
  const rows = new Map<string, { tasks: number; accepted: number; rejected: number; inFlight: number }>();
  for (const t of tasks) {
    if (!t.family) continue;
    const r = rows.get(t.family) ?? { tasks: 0, accepted: 0, rejected: 0, inFlight: 0 };
    r.tasks += 1;
    if (t.status === "ACCEPTED") r.accepted += 1;
    else if (t.status === "REJECTED") r.rejected += 1;
    else if (t.status === "CALIBRATING" || t.status === "CREATED") r.inFlight += 1;
    rows.set(t.family, r);
  }
  if (rows.size === 0) return null;
  return (
    <Card>
      <CardHeader>
        <CardTitle>By family</CardTitle>
        <CardDescription>Yield = accepted / decided (accepted + rejected); cancelled tasks are left out</CardDescription>
      </CardHeader>
      <CardContent>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Family</TableHead>
              <TableHead className="text-right">Tasks</TableHead>
              <TableHead className="text-right">Accepted</TableHead>
              <TableHead className="text-right">Rejected</TableHead>
              <TableHead className="text-right">In flight</TableHead>
              <TableHead className="text-right">Yield</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {[...rows.entries()]
              .sort((a, b) => b[1].tasks - a[1].tasks || a[0].localeCompare(b[0]))
              .map(([family, r]) => (
                <TableRow key={family}>
                  <TableCell>
                    <Link href={`/tasks?family=${encodeURIComponent(family)}`} className="font-mono text-xs underline-offset-4 hover:underline">
                      {family}
                    </Link>
                  </TableCell>
                  <TableCell className="text-right tabular-nums">{r.tasks}</TableCell>
                  <TableCell className="text-right tabular-nums">{r.accepted}</TableCell>
                  <TableCell className="text-right tabular-nums">{r.rejected}</TableCell>
                  <TableCell className="text-right tabular-nums">{r.inFlight}</TableCell>
                  <TableCell className="text-right tabular-nums">
                    {r.accepted + r.rejected ? pct(r.accepted / (r.accepted + r.rejected)) : "—"}
                  </TableCell>
                </TableRow>
              ))}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  );
}
