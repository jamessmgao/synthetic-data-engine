"use client";

import { useSyncExternalStore } from "react";

// Prod / Dev: which tasks a page counts and lists. Prod = real work; Dev = mock runs (recorded
// cost exactly 0) and the batches the API lists as dev (SDE_DEV_BATCHES). One setting for every
// page, remembered in this browser; storage can be unavailable (private mode, blocked), so every
// access is guarded, with an in-memory fallback, and the default (and the server render) is Prod.
export type Scope = "prod" | "dev";

const KEY = "sde.scope";
let memory: Scope = "prod";
const listeners = new Set<() => void>();

function read(): Scope {
  try {
    const v = window.localStorage.getItem(KEY);
    return v === "dev" || v === "prod" ? v : memory;
  } catch {
    return memory;
  }
}

function write(v: Scope) {
  memory = v;
  try {
    window.localStorage.setItem(KEY, v);
  } catch {}
  listeners.forEach((l) => l());
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  window.addEventListener("storage", listener); // another tab changed it
  return () => {
    listeners.delete(listener);
    window.removeEventListener("storage", listener);
  };
}

export function useScope(): [Scope, (v: Scope) => void] {
  return [useSyncExternalStore(subscribe, read, () => "prod" as Scope), write];
}

// The query string for a scoped API path.
export const scopeQuery = (scope: Scope) => `?scope=${scope}`;

export function ScopeToggle({
  scope,
  onChange,
  other,
}: {
  scope: Scope;
  onChange: (v: Scope) => void;
  other?: number; // tasks the other view has
}) {
  const option = (value: Scope, label: string) => (
    <button
      type="button"
      onClick={() => onChange(value)}
      aria-pressed={scope === value}
      className={`rounded-md px-2.5 py-1 text-xs font-medium ${scope === value ? "bg-background shadow-sm ring-1 ring-foreground/10" : "text-muted-foreground hover:text-foreground"}`}
    >
      {label}
    </button>
  );
  return (
    <div className="flex items-center gap-2">
      <div className="inline-flex gap-0.5 rounded-lg bg-muted p-0.5" role="group" aria-label="Which tasks this page counts">
        {option("prod", "Prod")}
        {option("dev", "Dev")}
      </div>
      {other ? (
        <span className="text-xs text-muted-foreground">
          {other} task{other === 1 ? "" : "s"} in the {scope === "prod" ? "Dev" : "Prod"} view
        </span>
      ) : null}
    </div>
  );
}
