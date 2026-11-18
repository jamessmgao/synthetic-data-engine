"use client";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Stat } from "@/components/status";
import { duration, pct, usd } from "@/lib/format";
import type { Breakdown, Report } from "@/lib/types";

// The batch report (api/service.py:_summary): yield, cost, the pass-count histogram, and per-family rates.
export function ReportView({ report: r }: { report: Report }) {
  const rejected = Object.values(r.rejected).reduce((a, b) => a + b, 0);
  const hist = Object.entries(r.pass_count_histogram);
  const max = Math.max(1, ...hist.map(([, n]) => n));
  return (
    <div className="flex flex-col gap-4">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-6">
        <Stat label="Learnable rate" value={pct(r.yield.learnable_rate)} hint={`${r.accepted_total} of ${r.candidates - r.in_progress - r.cancelled} finished`} />
        <Stat label="Accepted" value={r.accepted_total} hint={`${r.accepted_v1} at v1`} />
        <Stat label="Rejected" value={rejected} hint={Object.entries(r.rejected).map(([k, v]) => `${v} ${k}`).join(", ") || "none"} />
        <Stat label="In progress" value={r.in_progress} hint={`${r.cancelled} cancelled`} />
        <Stat label="Cost" value={usd(r.cost_usd.total)} hint={`authoring ${usd(r.cost_usd.authoring)} · eval ${usd(r.cost_usd.evaluation)}`} />
        <Stat label="Wall clock" value={duration(r.wall_clock_s)} hint={`${usd(r.cost_usd.per_accepted)} per accepted`} />
      </div>
      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,2fr)]">
        <Card>
          <CardHeader>
            <CardTitle>Passes per scored version</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="flex h-32 items-end gap-2">
              {hist.map(([k, n]) => {
                const inBand = Number(k) >= 1 && Number(k) <= 3;
                return (
                  <div key={k} className="flex flex-1 flex-col items-center gap-1">
                    <span className="text-xs tabular-nums text-muted-foreground">{n}</span>
                    <div
                      className={`w-full rounded-t ${inBand ? "bg-emerald-500" : "bg-zinc-400 dark:bg-zinc-600"}`}
                      style={{ height: `${(n / max) * 88}px`, minHeight: n ? 2 : 0 }}
                    />
                    <span className="text-xs tabular-nums">{k}/5</span>
                  </div>
                );
              })}
            </div>
            <p className="mt-2 text-xs text-muted-foreground">Green: the accept band (1–3 of 5).</p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>By family</CardTitle>
          </CardHeader>
          <CardContent>
            <BreakdownTable rows={r.by_family} />
          </CardContent>
        </Card>
      </div>
    </div>
  );
}

function BreakdownTable({ rows }: { rows: Record<string, Breakdown> }) {
  const entries = Object.entries(rows);
  if (entries.length === 0) return <p className="text-sm text-muted-foreground">No v2 specs.</p>;
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Family</TableHead>
          <TableHead className="text-right">Tasks</TableHead>
          <TableHead className="text-right">Accepted</TableHead>
          <TableHead className="text-right">Rejected</TableHead>
          <TableHead className="text-right">Learnable</TableHead>
          <TableHead className="text-right">Pass rate</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {entries.map(([name, b]) => (
          <TableRow key={name}>
            <TableCell className="font-mono text-xs">{name}</TableCell>
            <TableCell className="text-right tabular-nums">{b.candidates}</TableCell>
            <TableCell className="text-right tabular-nums">{b.accepted}</TableCell>
            <TableCell className="text-right tabular-nums">{b.rejected}</TableCell>
            <TableCell className="text-right tabular-nums">{pct(b.learnable_rate)}</TableCell>
            <TableCell className="text-right tabular-nums" title={`${b.valid_rollouts} valid agent runs`}>
              {pct(b.pass_rate)}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}
