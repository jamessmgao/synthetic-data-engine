"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useIsFetching } from "@tanstack/react-query";
import { cn } from "cn";
import { useApi } from "@/lib/api";
import { usd } from "@/lib/format";
import { ScopeToggle, useScope } from "@/components/dev-toggle";
import type { Health } from "@/lib/types";

const LINKS = [
  { href: "/", label: "Overview" },
  { href: "/batches", label: "Batches" },
  { href: "/tasks", label: "Tasks" },
  { href: "/analytics", label: "Analytics" },
];

export function Nav() {
  const pathname = usePathname();
  const [scope, setScope] = useScope();
  const health = useApi<Health>("/healthz");
  const fetching = useIsFetching();
  const budget = health.data?.budget;

  return (
    <header className="border-b bg-background">
      <div className="mx-auto flex h-14 max-w-7xl items-center gap-6 px-4">
        <Link href="/" className="font-semibold tracking-tight whitespace-nowrap" title="Agentic RL Environment Generation Platform">
          RL Environment Platform
        </Link>
        <nav className="flex items-center gap-1 text-sm">
          {LINKS.map((l) => (
            <Link
              key={l.href}
              href={l.href}
              className={cn(
                "rounded-md px-3 py-1.5 text-muted-foreground hover:bg-muted hover:text-foreground",
                (l.href === "/" ? pathname === "/" : pathname.startsWith(l.href)) && "bg-muted text-foreground",
              )}
            >
              {l.label}
            </Link>
          ))}
        </nav>
        <div className="ml-auto flex items-center gap-4 text-sm text-muted-foreground">
          <ScopeToggle scope={scope} onChange={setScope} />
          {budget && (
            <span title="Pipeline spend (and the global cap, if one is set)">
              spent {usd(budget.spent_usd)}
              {budget.cap_usd != null && ` / ${usd(budget.cap_usd, 0)}`}
            </span>
          )}
          <span className="flex items-center gap-1.5">
            <span
              className={cn(
                "size-2 rounded-full",
                health.error ? "bg-rose-500" : health.data?.status === "ok" ? "bg-emerald-500" : "bg-amber-500",
                fetching > 0 && "animate-pulse",
              )}
            />
            {health.error ? "API down" : (health.data?.status ?? "…")}
          </span>
        </div>
      </div>
    </header>
  );
}
