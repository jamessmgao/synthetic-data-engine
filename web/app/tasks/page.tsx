"use client";

import { useState } from "react";
import { cn } from "cn";
import { ScopeToggle, useScope } from "@/components/dev-toggle";
import { QueryState } from "@/components/status";
import { TasksTable } from "@/components/tasks-table";
import { useApi } from "@/lib/api";
import type { TaskSummary } from "@/lib/types";

const FILTERS = ["ALL", "CALIBRATING", "ACCEPTED", "REJECTED", "CANCELLED"] as const;

export default function TasksPage() {
  const [scope, setScope] = useScope();
  // Every task once (the API's dev flag), split here: the other view's count comes free.
  const tasks = useApi<TaskSummary[]>("/v1/tasks");
  const [filter, setFilter] = useState<(typeof FILTERS)[number]>("ALL");
  const [q, setQ] = useState("");

  const every = tasks.data ?? [];
  const all = every.filter((t) => t.dev === (scope === "dev"));
  const count = (f: string) => (f === "ALL" ? all.length : all.filter((t) => matches(t, f)).length);
  const shown = all
    .filter((t) => filter === "ALL" || matches(t, filter))
    .filter((t) => !q || `${t.id} ${t.name ?? ""} ${t.stage}`.toLowerCase().includes(q.toLowerCase()))
    .reverse(); // newest first

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <h1 className="text-xl font-semibold">Tasks</h1>
        <ScopeToggle scope={scope} onChange={setScope} other={tasks.data ? every.length - all.length : undefined} />
      </div>
      <div className="flex flex-wrap items-center gap-3">
        <div className="flex rounded-lg border p-0.5 text-sm">
          {FILTERS.map((f) => (
            <button
              key={f}
              onClick={() => setFilter(f)}
              className={cn("rounded-md px-3 py-1 text-muted-foreground", filter === f && "bg-primary text-primary-foreground")}
            >
              {f === "ALL" ? "All" : f[0] + f.slice(1).toLowerCase()} <span className="tabular-nums opacity-70">{count(f)}</span>
            </button>
          ))}
        </div>
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
