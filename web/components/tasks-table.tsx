"use client";

import Link from "next/link";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { PassPattern, StatusBadge } from "@/components/status";
import { Badge } from "@/components/ui/badge";
import { ago, usd } from "@/lib/format";
import type { TaskSummary, VersionSummary } from "@/lib/types";

// How each version was made, in the History column.
const ORIGIN: Record<string, string> = { initial: "init", mutation: "mut", easier: "easy", repair_ambiguity: "repair" };

// "v1 init 5/5 → v2 mut 2/5": each version's origin and passes ("…" while it is being scored).
export function versionHistory(versions: VersionSummary[]) {
  return versions
    .map((v) => `v${v.n} ${v.origin ? ORIGIN[v.origin] ?? v.origin : "?"} ${v.pass_count == null ? "…" : `${v.pass_count}/5`}`)
    .join(" → ");
}

export function TasksTable({ tasks }: { tasks: TaskSummary[] }) {
  if (tasks.length === 0) return <p className="text-sm text-muted-foreground">No tasks.</p>;
  // Columns for fields an older API doesn't send are left out, not shown empty.
  const taxonomy = tasks.some((t) => t.family);
  const history = tasks.some((t) => t.versions?.length);
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Task</TableHead>
          {taxonomy && <TableHead>Family / mechanism</TableHead>}
          <TableHead>Status</TableHead>
          <TableHead>Stage</TableHead>
          <TableHead className="text-right">Version</TableHead>
          <TableHead>Agent runs</TableHead>
          {history && <TableHead>History</TableHead>}
          <TableHead className="text-right">Cost</TableHead>
          <TableHead>Created</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {tasks.map((t) => (
          <TableRow key={t.id}>
            <TableCell>
              <Link href={`/tasks/${encodeURIComponent(t.id)}`} className="underline-offset-4 hover:underline">
                <div className="font-mono text-xs">
                  {t.id}
                  {t.dev && (
                    <Badge variant="secondary" className="ml-1.5 align-middle" title="Mock run (recorded cost 0): left out of reports by default">
                      dev
                    </Badge>
                  )}
                </div>
                {t.name && <div className="max-w-xs truncate text-xs text-muted-foreground">{t.name}</div>}
              </Link>
            </TableCell>
            {taxonomy && (
              <TableCell className="text-xs text-muted-foreground">
                <div>{t.family ?? "—"}</div>
                {t.mechanism && <div className="max-w-48 truncate">{t.mechanism}</div>}
              </TableCell>
            )}
            <TableCell>
              <StatusBadge value={t.status} />
              {t.reason && <span className="ml-1.5 text-xs text-muted-foreground">{t.reason}</span>}
            </TableCell>
            <TableCell className="font-mono text-xs">{t.stage}</TableCell>
            <TableCell className="text-right tabular-nums">v{t.current_version}</TableCell>
            <TableCell>
              <PassPattern pattern={t.pass_pattern} />
            </TableCell>
            {history && (
              <TableCell className="font-mono text-xs whitespace-nowrap text-muted-foreground">
                {t.versions?.length ? versionHistory(t.versions) : "—"}
              </TableCell>
            )}
            <TableCell className="text-right tabular-nums">{usd(t.cost_usd)}</TableCell>
            <TableCell className="text-muted-foreground">{ago(t.created_at)}</TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}
