"use client";

import Link from "next/link";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { BatchesTable } from "@/components/batches-table";
import { JobsTable } from "@/components/jobs-table";
import { QueryState, Stat } from "@/components/status";
import { ApiError, useApi } from "@/lib/api";
import { duration, pct, usd } from "@/lib/format";
import type { BatchSummary, JobStatus, Queue, Report } from "@/lib/types";

// The pipeline's job types in flow order (README "The flow").
const JOB_TYPES = ["world", "world_check", "create", "revise", "validate", "rollout", "analyze"];
const JOB_STATUSES: JobStatus[] = ["READY", "LEASED", "DONE", "DEAD"];

export default function Overview() {
  const report = useApi<Report>("/v1/report");
  const queue = useApi<Queue>("/v1/queue");
  const batches = useApi<BatchSummary[]>("/v1/batches");
  const noTasks = report.error instanceof ApiError && report.error.status === 404;
  const r = report.data;

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-xl font-semibold">Overview</h1>
        <p className="text-sm text-muted-foreground">Every task the pipeline has. Refreshes every 10 s.</p>
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
          <Stat label="Rollouts" value={r.valid_rollouts} hint={`${r.invalid_rollouts} invalid · ${duration(r.wall_clock_s)} wall`} />
        </div>
      )}
      {noTasks && <p className="text-sm text-muted-foreground">No tasks yet.</p>}

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
                        <TableCell className="font-mono text-xs">{t}</TableCell>
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
            <CardTitle>Running now</CardTitle>
            <CardDescription>{queue.data ? `${queue.data.running.length} leased jobs, oldest first` : "Leased jobs"}</CardDescription>
          </CardHeader>
          <CardContent>
            {queue.data && <JobsTable jobs={queue.data.running} showTask empty="Nothing running." />}
          </CardContent>
        </Card>
      </div>

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
          {batches.data && <BatchesTable batches={batches.data.slice(0, 8)} />}
        </CardContent>
      </Card>
    </div>
  );
}
