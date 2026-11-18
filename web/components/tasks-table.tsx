"use client";

import Link from "next/link";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { PassPattern, StatusBadge } from "@/components/status";
import { Badge } from "@/components/ui/badge";
import { ago, usd } from "@/lib/format";
import type { TaskSummary } from "@/lib/types";

export function TasksTable({ tasks }: { tasks: TaskSummary[] }) {
  if (tasks.length === 0) return <p className="text-sm text-muted-foreground">No tasks.</p>;
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Task</TableHead>
          <TableHead>Status</TableHead>
          <TableHead>Stage</TableHead>
          <TableHead className="text-right">Version</TableHead>
          <TableHead>Agent runs</TableHead>
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
            <TableCell>
              <StatusBadge value={t.status} />
              {t.reason && <span className="ml-1.5 text-xs text-muted-foreground">{t.reason}</span>}
            </TableCell>
            <TableCell className="font-mono text-xs">{t.stage}</TableCell>
            <TableCell className="text-right tabular-nums">v{t.current_version}</TableCell>
            <TableCell>
              <PassPattern pattern={t.pass_pattern} />
            </TableCell>
            <TableCell className="text-right tabular-nums">{usd(t.cost_usd)}</TableCell>
            <TableCell className="text-muted-foreground">{ago(t.created_at)}</TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}
