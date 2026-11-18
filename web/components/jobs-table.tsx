"use client";

import Link from "next/link";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { StatusBadge } from "@/components/status";
import { between, duration, localTime, usd } from "@/lib/format";
import type { Job } from "@/lib/types";

// Display names for the API's job types ("rollout" is an agent run, "create" authors the task, "analyze" analyzes the trajectories).
const JOB_TYPE_LABELS: Record<string, string> = { rollout: "agent_run", create: "author_task", analyze: "analyze_trajectory" };

export function jobTypeLabel(type: string): string {
  return JOB_TYPE_LABELS[type] ?? type;
}

export function jobLabel(j: Job): string {
  const slot = j.type === "rollout" ? ` #${j.slot}${j.retry ? ` retry ${j.retry}` : ""}` : "";
  const origin = j.origin && j.origin !== "initial" ? ` (${j.origin})` : "";
  return `${jobTypeLabel(j.type)} v${j.version}${slot}${origin}`;
}

export function JobsTable({ jobs, showTask = false, empty = "No jobs." }: { jobs: Job[]; showTask?: boolean; empty?: string }) {
  if (jobs.length === 0) return <p className="text-sm text-muted-foreground">{empty}</p>;
  return (
    <Table>
      <TableHeader>
        <TableRow>
          {showTask && <TableHead>Task</TableHead>}
          <TableHead>Job</TableHead>
          <TableHead>Status</TableHead>
          <TableHead>Outcome</TableHead>
          <TableHead>Started</TableHead>
          <TableHead className="text-right">Ran</TableHead>
          <TableHead className="text-right">Cost</TableHead>
          <TableHead>Worker</TableHead>
          <TableHead>Files</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {jobs.map((j) => (
          <TableRow key={j.id}>
            {showTask && (
              <TableCell>
                <Link href={`/tasks/${encodeURIComponent(j.task_id)}`} className="font-mono text-xs underline-offset-4 hover:underline">
                  {j.task_id}
                </Link>
              </TableCell>
            )}
            <TableCell className="font-mono text-xs" title={j.id}>
              {jobLabel(j)}
              {j.attempts > 1 && <span className="ml-1 text-amber-600"> ×{j.attempts}</span>}
            </TableCell>
            <TableCell>
              <StatusBadge value={j.status === "LEASED" ? "LEASED" : j.status} />
            </TableCell>
            <TableCell>
              <StatusBadge value={j.outcome} />
              {j.error && (
                <div className="mt-0.5 max-w-xs truncate text-xs text-destructive" title={j.error}>
                  {j.error}
                </div>
              )}
            </TableCell>
            <TableCell className="text-muted-foreground">{localTime(j.started_at)}</TableCell>
            <TableCell className="text-right tabular-nums">{duration(between(j.started_at, j.finished_at))}</TableCell>
            <TableCell className="text-right tabular-nums">{usd(j.cost_usd)}</TableCell>
            <TableCell className="max-w-40 truncate font-mono text-xs text-muted-foreground" title={j.lease_owner ?? ""}>
              {j.lease_owner ?? "—"}
            </TableCell>
            <TableCell className="space-x-2 text-xs">
              {j.trace_uri && (
                <Link href={`/artifacts/traces/${encodeURIComponent(j.id)}`} className="underline underline-offset-4">
                  trace
                </Link>
              )}
              {j.bundle_sha256 && (
                <Link href={`/artifacts/bundles/${j.bundle_sha256}`} className="underline underline-offset-4">
                  bundle
                </Link>
              )}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}
