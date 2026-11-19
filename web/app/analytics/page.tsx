"use client";

import Link from "next/link";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { OtherScopeNote, scopeQuery, useScope } from "@/components/dev-toggle";
import { QueryState, Stat } from "@/components/status";
import { useApi } from "@/lib/api";
import { pct, usd } from "@/lib/format";
import type { Report, TaskSummary, VersionSummary } from "@/lib/types";

// Yield by taxonomy and how tasks reached the band: tasks from /v1/tasks (family, mechanism,
// per-version origin and pass count), rates and costs from /v1/report.
export default function AnalyticsPage() {
  const [scope] = useScope();
  const tasks = useApi<TaskSummary[]>("/v1/tasks");
  const report = useApi<Report>(`/v1/report${scopeQuery(scope)}`);

  const every = tasks.data ?? [];
  const all = every.filter((t) => t.dev === (scope === "dev"));
  const hasTaxonomy = all.some((t) => t.family);

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Analytics</h1>
          <p className="text-sm text-muted-foreground">Yield by family and mechanism, and how accepted tasks got into the band.</p>
        </div>
        <OtherScopeNote scope={scope} other={tasks.data ? every.length - all.length : undefined} />
      </div>
      <QueryState error={tasks.error ?? report.error} isLoading={tasks.isLoading} />
      {tasks.data && !hasTaxonomy && (
        <p className="rounded-lg border p-3 text-sm text-muted-foreground">
          This API doesn&apos;t report task families yet; the tables below need its taxonomy fields.
        </p>
      )}
      {tasks.data && hasTaxonomy && (
        <>
          <Headline tasks={all} report={report.data} />
          <FamilyTable tasks={all} report={report.data} />
          <div className="grid gap-4 lg:grid-cols-2">
            <VersionsToAccept tasks={all} />
            <Revisions tasks={all} />
          </div>
          <MechanismTable tasks={all} report={report.data} />
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------------------------
// Derivations

const IN_FLIGHT = new Set(["CREATED", "CALIBRATING"]);

interface Tally {
  tasks: number;
  inFlight: number;
  accepted: number;
  rejected: number;
  cancelled: number;
  acceptedV1: number;
  versionsToAccept: number[]; // the accepted version of each accepted task
  mutations: number; // mutated versions, over every task in the group
}

function tally(tasks: TaskSummary[]): Tally {
  const t: Tally = { tasks: 0, inFlight: 0, accepted: 0, rejected: 0, cancelled: 0, acceptedV1: 0, versionsToAccept: [], mutations: 0 };
  for (const x of tasks) {
    t.tasks++;
    if (IN_FLIGHT.has(x.status)) t.inFlight++;
    if (x.status === "REJECTED") t.rejected++;
    if (x.status === "CANCELLED") t.cancelled++;
    if (x.status === "ACCEPTED") {
      t.accepted++;
      const v = x.accepted_version ?? x.current_version;
      t.versionsToAccept.push(v);
      if (v === 1) t.acceptedV1++;
    }
    t.mutations += (x.versions ?? []).filter((v) => v.origin === "mutation").length;
  }
  return t;
}

const yieldOf = (t: Tally) => (t.accepted + t.rejected ? t.accepted / (t.accepted + t.rejected) : null);
const mean = (xs: number[]) => (xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null);
const fixed1 = (n: number | null) => (n == null ? "—" : n.toFixed(1));

function groupBy(tasks: TaskSummary[], key: (t: TaskSummary) => string | null | undefined) {
  const out = new Map<string, TaskSummary[]>();
  for (const t of tasks) {
    const k = key(t) ?? "(none)";
    out.set(k, [...(out.get(k) ?? []), t]);
  }
  return out;
}

const tasksHref = (q: Record<string, string>) => `/tasks?${new URLSearchParams(q).toString()}`;

// ---------------------------------------------------------------------------------------------
// Sections

function Headline({ tasks, report }: { tasks: TaskSummary[]; report?: Report }) {
  const t = tally(tasks);
  const revised = t.accepted - t.acceptedV1;
  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-6">
      <Stat label="Tasks" value={t.tasks} hint={`${t.inFlight} in flight · ${t.cancelled} cancelled`} />
      <Stat label="Yield" value={pct(yieldOf(t))} hint={`${t.accepted} accepted of ${t.accepted + t.rejected} decided`} />
      <Stat label="Accepted" value={t.accepted} hint={`${t.acceptedV1} at v1 · ${revised} after a revision`} />
      <Stat label="Rejected" value={t.rejected} hint={report ? Object.entries(report.rejected).map(([k, v]) => `${v} ${k}`).join(", ") || "none" : undefined} />
      <Stat label="Versions to accept" value={fixed1(mean(t.versionsToAccept))} hint="mean over accepted tasks" />
      <Stat label="Cost per accepted" value={usd(report?.cost_usd.per_accepted)} hint="all spend ÷ accepted (ledger)" />
    </div>
  );
}

function FamilyTable({ tasks, report }: { tasks: TaskSummary[]; report?: Report }) {
  const groups = [...groupBy(tasks, (t) => t.family)].sort((a, b) => b[1].length - a[1].length);
  return (
    <Card>
      <CardHeader>
        <CardTitle>By family</CardTitle>
      </CardHeader>
      <CardContent>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Family</TableHead>
              <TableHead className="text-right">Tasks</TableHead>
              <TableHead className="text-right">In flight</TableHead>
              <TableHead className="text-right">Accepted</TableHead>
              <TableHead className="text-right">Rejected</TableHead>
              <TableHead className="text-right">Yield</TableHead>
              <TableHead className="text-right" title="Accepted at v1 / accepted after a revision">v1 / revised</TableHead>
              <TableHead className="text-right" title="Mean accepted version">Versions</TableHead>
              <TableHead className="text-right" title="World mutations across the family's tasks">Mutations</TableHead>
              <TableHead className="text-right" title="Passing share of valid agent runs (report)">Pass rate</TableHead>
              <TableHead className="text-right" title="Authoring spend per task (report, ledger)">$ / task</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {groups.map(([family, ts]) => {
              const t = tally(ts);
              const b = report?.by_family[family];
              return (
                <TableRow key={family}>
                  <TableCell className="font-mono text-xs">
                    <Link href={tasksHref({ family })} className="underline-offset-4 hover:underline">
                      {family}
                    </Link>
                  </TableCell>
                  <TableCell className="text-right tabular-nums">{t.tasks}</TableCell>
                  <TableCell className="text-right tabular-nums">{t.inFlight}</TableCell>
                  <TableCell className="text-right tabular-nums">
                    <Link href={tasksHref({ family, status: "ACCEPTED" })} className="underline-offset-4 hover:underline">
                      {t.accepted}
                    </Link>
                  </TableCell>
                  <TableCell className="text-right tabular-nums">{t.rejected}</TableCell>
                  <TableCell className="text-right tabular-nums font-medium">{pct(yieldOf(t))}</TableCell>
                  <TableCell className="text-right tabular-nums">
                    {t.acceptedV1} / {t.accepted - t.acceptedV1}
                  </TableCell>
                  <TableCell className="text-right tabular-nums">{fixed1(mean(t.versionsToAccept))}</TableCell>
                  <TableCell className="text-right tabular-nums">{t.mutations}</TableCell>
                  <TableCell className="text-right tabular-nums">{pct(b?.pass_rate)}</TableCell>
                  <TableCell className="text-right tabular-nums">{usd(b?.cost_usd?.authoring_per_task)}</TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
        <p className="mt-2 text-xs text-muted-foreground">Yield = accepted ÷ (accepted + rejected); cancelled and in-flight tasks are left out.</p>
      </CardContent>
    </Card>
  );
}

function MechanismTable({ tasks, report }: { tasks: TaskSummary[]; report?: Report }) {
  const rows = [...groupBy(tasks, (t) => (t.family ? `${t.family}\u0000${t.mechanism ?? "(none)"}` : null))]
    .map(([k, ts]) => {
      const [family, mechanism] = k.split("\u0000");
      return { family, mechanism: mechanism ?? "(none)", t: tally(ts) };
    })
    .sort((a, b) => a.family.localeCompare(b.family) || b.t.tasks - a.t.tasks);
  return (
    <Card>
      <CardHeader>
        <CardTitle>By mechanism</CardTitle>
      </CardHeader>
      <CardContent>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Family</TableHead>
              <TableHead>Mechanism</TableHead>
              <TableHead className="text-right">Tasks</TableHead>
              <TableHead className="text-right">In flight</TableHead>
              <TableHead className="text-right">Accepted</TableHead>
              <TableHead className="text-right">Rejected</TableHead>
              <TableHead className="text-right">Yield</TableHead>
              <TableHead className="text-right">Mutations</TableHead>
              <TableHead className="text-right">Pass rate</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map(({ family, mechanism, t }, i) => (
              <TableRow key={`${family}/${mechanism}`}>
                <TableCell className="font-mono text-xs text-muted-foreground">{i === 0 || rows[i - 1].family !== family ? family : ""}</TableCell>
                <TableCell className="font-mono text-xs">
                  <Link href={tasksHref({ family, mechanism })} className="underline-offset-4 hover:underline">
                    {mechanism}
                  </Link>
                </TableCell>
                <TableCell className="text-right tabular-nums">{t.tasks}</TableCell>
                <TableCell className="text-right tabular-nums">{t.inFlight}</TableCell>
                <TableCell className="text-right tabular-nums">{t.accepted}</TableCell>
                <TableCell className="text-right tabular-nums">{t.rejected}</TableCell>
                <TableCell className="text-right tabular-nums font-medium">{pct(yieldOf(t))}</TableCell>
                <TableCell className="text-right tabular-nums">{t.mutations}</TableCell>
                <TableCell className="text-right tabular-nums">{pct(report?.by_mechanism[mechanism]?.pass_rate)}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  );
}

// How many versions accepted tasks needed, as one bar per version count.
function VersionsToAccept({ tasks }: { tasks: TaskSummary[] }) {
  const accepted = tasks.filter((t) => t.status === "ACCEPTED");
  const buckets = ["v1", "v2", "v3", "v4+"].map((label, i) => ({
    label,
    n: accepted.filter((t) => {
      const v = t.accepted_version ?? t.current_version;
      return i < 3 ? v === i + 1 : v >= 4;
    }).length,
  }));
  const max = Math.max(1, ...buckets.map((b) => b.n));
  // The path each accepted task took: its versions' origins, e.g. "initial → mutation".
  const paths = [...groupBy(accepted, (t) => pathOf(t.versions, t.accepted_version ?? t.current_version))].sort((a, b) => b[1].length - a[1].length);
  return (
    <Card>
      <CardHeader>
        <CardTitle>Versions to accept</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <div className="flex h-32 items-end gap-3" role="img" aria-label={buckets.map((b) => `${b.label}: ${b.n}`).join(", ")}>
          {buckets.map((b) => (
            <div key={b.label} className="group flex flex-1 flex-col items-center gap-1" title={`${b.n} accepted at ${b.label}`}>
              <span className="text-xs tabular-nums text-muted-foreground">{b.n}</span>
              <div className="w-full max-w-16 rounded-t bg-primary/80 group-hover:bg-primary" style={{ height: `${(b.n / max) * 88}px`, minHeight: b.n ? 2 : 0 }} />
              <span className="text-xs tabular-nums">{b.label}</span>
            </div>
          ))}
        </div>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Path to the band</TableHead>
              <TableHead className="text-right">Accepted</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {paths.map(([path, ts]) => (
              <TableRow key={path}>
                <TableCell className="font-mono text-xs">{path}</TableCell>
                <TableCell className="text-right tabular-nums">{ts.length}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  );
}

function pathOf(versions: VersionSummary[] | undefined, upTo: number): string {
  const vs = (versions ?? []).filter((v) => v.n <= upTo);
  return vs.length ? vs.map((v) => v.origin ?? "?").join(" → ") : "(unknown)";
}

// Every revision whose version and the one before it were both scored: did it move toward the band?
type Move = "into band" | "toward" | "no change" | "overshoot" | "away";
const MOVES: Move[] = ["into band", "toward", "no change", "overshoot", "away"];

function moveOf(before: number, after: number): Move {
  const inBand = (n: number) => n >= 1 && n <= 3;
  if (inBand(after)) return "into band";
  if (before === after) return "no change";
  if ((before >= 4 && after === 0) || (before === 0 && after >= 4)) return "overshoot";
  const dist = (n: number) => (n === 0 ? 1 : n >= 4 ? n - 3 : 0);
  return dist(after) < dist(before) ? "toward" : "away";
}

function Revisions({ tasks }: { tasks: TaskSummary[] }) {
  const byOrigin = new Map<string, Record<Move, number>>();
  for (const t of tasks) {
    const vs = [...(t.versions ?? [])].sort((a, b) => a.n - b.n);
    for (let i = 1; i < vs.length; i++) {
      const [prev, cur] = [vs[i - 1], vs[i]];
      if (prev.pass_count == null || cur.pass_count == null) continue;
      const origin = cur.origin ?? "?";
      const row = byOrigin.get(origin) ?? (Object.fromEntries(MOVES.map((m) => [m, 0])) as Record<Move, number>);
      row[moveOf(prev.pass_count, cur.pass_count)]++;
      byOrigin.set(origin, row);
    }
  }
  const rows = [...byOrigin].sort((a, b) => total(b[1]) - total(a[1]));
  return (
    <Card>
      <CardHeader>
        <CardTitle>Revisions</CardTitle>
      </CardHeader>
      <CardContent>
        {rows.length === 0 ? (
          <p className="text-sm text-muted-foreground">No scored revision yet.</p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Revision</TableHead>
                <TableHead className="text-right">Scored</TableHead>
                {MOVES.map((m) => (
                  <TableHead key={m} className="text-right">
                    {m}
                  </TableHead>
                ))}
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map(([origin, r]) => (
                <TableRow key={origin}>
                  <TableCell className="font-mono text-xs">{origin}</TableCell>
                  <TableCell className="text-right tabular-nums">{total(r)}</TableCell>
                  {MOVES.map((m) => (
                    <TableCell key={m} className={`text-right tabular-nums ${m === "into band" && r[m] ? "font-medium" : ""}`}>
                      {r[m]}
                    </TableCell>
                  ))}
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
        <p className="mt-2 text-xs text-muted-foreground">
          Each scored version against the scored version before it. Toward / away: closer to or farther from 1–3 of 5; overshoot: jumped across the band (4–5 → 0 or 0 → 4–5).
        </p>
      </CardContent>
    </Card>
  );
}

const total = (r: Record<Move, number>) => MOVES.reduce((a, m) => a + r[m], 0);
