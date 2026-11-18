import { Badge, type BadgeVariant } from "@/components/ui/badge";
import { cn } from "cn";

const VARIANT: Record<string, BadgeVariant> = {
  // task status
  ACCEPTED: "success",
  REJECTED: "destructive",
  CANCELLED: "secondary",
  CALIBRATING: "info",
  CREATED: "outline",
  // job status
  READY: "outline",
  LEASED: "info",
  DONE: "secondary",
  DEAD: "destructive",
  // outcomes
  OK: "success",
  FAILED: "destructive",
  VALID_PASS: "success",
  VALID_FAIL: "warning",
  INVALID: "destructive",
  TIMEOUT: "warning",
  // version states
  BAND_OK: "success",
  OUT_OF_BAND: "warning",
  UNRESOLVED: "destructive",
};

export function StatusBadge({ value, className }: { value: string | null | undefined; className?: string }) {
  if (!value) return <span className="text-muted-foreground">—</span>;
  return (
    <Badge variant={VARIANT[value] ?? "outline"} className={className}>
      {value}
    </Badge>
  );
}

// "✓✗··✓": one mark per agent run slot (evaluation/verdict.py:pass_pattern).
export function PassPattern({ pattern }: { pattern: string }) {
  return (
    <span className="inline-flex gap-0.5 font-mono text-sm" title={`${[...pattern].filter((c) => c === "✓").length} passed`}>
      {[...pattern].map((c, i) => (
        <span
          key={i}
          className={cn(
            "inline-flex size-5 items-center justify-center rounded",
            c === "✓" && "bg-emerald-500/15 text-emerald-700 dark:text-emerald-400",
            c === "✗" && "bg-rose-500/15 text-rose-700 dark:text-rose-400",
            c !== "✓" && c !== "✗" && "bg-muted text-muted-foreground",
          )}
        >
          {c}
        </span>
      ))}
    </span>
  );
}

export function QueryState({ error, isLoading }: { error: Error | null; isLoading: boolean }) {
  if (error) {
    return (
      <div className="rounded-lg border border-destructive/40 bg-destructive/5 p-3 text-sm text-destructive">
        {error.message}
      </div>
    );
  }
  if (isLoading) return <div className="p-3 text-sm text-muted-foreground">Loading…</div>;
  return null;
}

export function Stat({ label, value, hint }: { label: string; value: React.ReactNode; hint?: React.ReactNode }) {
  return (
    <div className="rounded-xl p-4 ring-1 ring-foreground/10">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="mt-1 text-2xl font-semibold tabular-nums">{value}</div>
      {hint && <div className="mt-1 text-xs text-muted-foreground">{hint}</div>}
    </div>
  );
}

// Accepted / rejected / cancelled / in progress, as one stacked bar.
export function ProgressBar({
  accepted,
  rejected,
  cancelled,
  total,
}: {
  accepted: number;
  rejected: number;
  cancelled: number;
  total: number;
}) {
  const w = (n: number) => `${total ? (n / total) * 100 : 0}%`;
  return (
    <div className="flex h-2 w-40 overflow-hidden rounded-full bg-muted" title={`${accepted} accepted, ${rejected} rejected, ${cancelled} cancelled of ${total}`}>
      <div className="bg-emerald-500" style={{ width: w(accepted) }} />
      <div className="bg-rose-500" style={{ width: w(rejected) }} />
      <div className="bg-zinc-400" style={{ width: w(cancelled) }} />
    </div>
  );
}
