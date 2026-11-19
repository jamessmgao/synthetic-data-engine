"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import { cn } from "cn";
import { OtherScopeNote, useScope } from "@/components/dev-toggle";
import { QueryState } from "@/components/status";
import { TasksTable } from "@/components/tasks-table";
import { useApi } from "@/lib/api";
import type { TaskSummary } from "@/lib/types";

const FILTERS = ["ALL", "CALIBRATING", "ACCEPTED", "REJECTED", "CANCELLED"] as const;
type Filter = (typeof FILTERS)[number];

export default function TasksPage() {
  return (
    <Suspense>
      <Tasks />
    </Suspense>
  );
}

function Tasks() {
  const [scope] = useScope();
  // Every task once (the API's dev flag), split here: the other view's count comes free.
  const tasks = useApi<TaskSummary[]>("/v1/tasks");
  // Status, family and mechanism live in the URL (?status=…&family=…&mechanism=…), so other
  // pages can link to a filtered list; the search box stays local.
  const params = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const status = params.get("status")?.toUpperCase();
  const filter: Filter = FILTERS.find((f) => f === status) ?? "ALL";
  const family = params.get("family") ?? "";
  const mechanism = params.get("mechanism") ?? "";
  const [q, setQ] = useState("");

  const setParams = (next: Record<string, string>) => {
    const p = new URLSearchParams(params.toString());
    for (const [k, v] of Object.entries(next)) {
      if (v) p.set(k, v);
      else p.delete(k);
    }
    const qs = p.toString();
    router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
  };

  const every = tasks.data ?? [];
  const all = every.filter((t) => t.dev === (scope === "dev"));
  // Family and mechanism options come from the tasks themselves: none on an older API.
  const families = tally(all.map((t) => t.family));
  const mechanisms = tally(all.filter((t) => !family || t.family === family).map((t) => t.mechanism));
  const inTaxonomy = all
    .filter((t) => !family || t.family === family)
    .filter((t) => !mechanism || t.mechanism === mechanism);
  const count = (f: string) => (f === "ALL" ? inTaxonomy.length : inTaxonomy.filter((t) => matches(t, f)).length);
  const shown = inTaxonomy
    .filter((t) => filter === "ALL" || matches(t, filter))
    .filter((t) => !q || `${t.id} ${t.name ?? ""} ${t.stage}`.toLowerCase().includes(q.toLowerCase()))
    .reverse(); // newest first

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <h1 className="text-xl font-semibold">Tasks</h1>
        <OtherScopeNote scope={scope} other={tasks.data ? every.length - all.length : undefined} />
      </div>
      <div className="flex flex-wrap items-center gap-3">
        <div className="flex rounded-lg border p-0.5 text-sm">
          {FILTERS.map((f) => (
            <button
              key={f}
              onClick={() => setParams({ status: f === "ALL" ? "" : f })}
              className={cn("rounded-md px-3 py-1 text-muted-foreground", filter === f && "bg-primary text-primary-foreground")}
            >
              {f === "ALL" ? "All" : f[0] + f.slice(1).toLowerCase()} <span className="tabular-nums opacity-70">{count(f)}</span>
            </button>
          ))}
        </div>
        {families.length > 0 && (
          <select
            value={family}
            onChange={(e) => setParams({ family: e.target.value, mechanism: "" })}
            className="h-8 rounded-md border bg-transparent px-2 text-sm"
            aria-label="Family"
          >
            <option value="">All families</option>
            {families.map(([f, n]) => (
              <option key={f} value={f}>
                {f} ({n})
              </option>
            ))}
          </select>
        )}
        {family && mechanisms.length > 0 && (
          <select
            value={mechanism}
            onChange={(e) => setParams({ mechanism: e.target.value })}
            className="h-8 rounded-md border bg-transparent px-2 text-sm"
            aria-label="Mechanism"
          >
            <option value="">All mechanisms</option>
            {mechanisms.map(([m, n]) => (
              <option key={m} value={m}>
                {m} ({n})
              </option>
            ))}
          </select>
        )}
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Search id, name, stage"
          className="h-8 w-64 rounded-md border bg-transparent px-2 text-sm"
        />
      </div>
      <QueryState error={tasks.error} isLoading={tasks.isLoading} />
      {tasks.data && <TasksTable tasks={shown} />}
    </div>
  );
}

// CREATED counts as in progress, alongside CALIBRATING.
function matches(t: TaskSummary, f: string) {
  return f === "CALIBRATING" ? t.status === "CALIBRATING" || t.status === "CREATED" : t.status === f;
}

// [value, count] for each non-empty value, most common first.
function tally(values: (string | null | undefined)[]): [string, number][] {
  const counts = new Map<string, number>();
  for (const v of values) if (v) counts.set(v, (counts.get(v) ?? 0) + 1);
  return [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
}
