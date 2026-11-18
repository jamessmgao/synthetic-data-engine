"use client";

import Link from "next/link";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { ProgressBar } from "@/components/status";
import { Badge } from "@/components/ui/badge";
import { ago } from "@/lib/format";
import type { BatchSummary } from "@/lib/types";

export function BatchesTable({ batches }: { batches: BatchSummary[] }) {
  if (batches.length === 0) return <p className="text-sm text-muted-foreground">No batches yet.</p>;
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Batch</TableHead>
          <TableHead className="text-right">Tasks</TableHead>
          <TableHead>Progress</TableHead>
          <TableHead className="text-right">Accepted</TableHead>
          <TableHead className="text-right">Rejected</TableHead>
          <TableHead className="text-right">Cancelled</TableHead>
          <TableHead className="text-right">In progress</TableHead>
          <TableHead>Created</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {batches.map((b) => (
          <TableRow key={b.batch_id}>
            <TableCell>
              <Link href={`/batches/${encodeURIComponent(b.batch_id)}`} className="font-mono text-xs underline-offset-4 hover:underline">
                {b.batch_id}
              </Link>
              {b.dev && (
                <Badge variant="secondary" className="ml-1.5 align-middle" title="Dev batch (mock runs, or listed in SDE_DEV_BATCHES): left out of Prod numbers">
                  dev
                </Badge>
              )}
            </TableCell>
            <TableCell className="text-right tabular-nums">{b.tasks}</TableCell>
            <TableCell>
              <ProgressBar accepted={b.accepted} rejected={b.rejected} cancelled={b.cancelled} total={b.tasks} />
            </TableCell>
            <TableCell className="text-right tabular-nums text-emerald-700 dark:text-emerald-400">{b.accepted}</TableCell>
            <TableCell className="text-right tabular-nums text-rose-700 dark:text-rose-400">{b.rejected}</TableCell>
            <TableCell className="text-right tabular-nums text-muted-foreground">{b.cancelled}</TableCell>
            <TableCell className="text-right tabular-nums">{b.in_progress}</TableCell>
            <TableCell className="text-muted-foreground">{ago(b.created_at)}</TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}
