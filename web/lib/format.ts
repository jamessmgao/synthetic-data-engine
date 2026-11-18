export function usd(n: number | null | undefined, digits = 2): string {
  return n == null ? "—" : `$${n.toFixed(digits)}`;
}

export function pct(n: number | null | undefined): string {
  return n == null ? "—" : `${Math.round(n * 100)}%`;
}

export function duration(seconds: number | null | undefined): string {
  if (seconds == null) return "—";
  const s = Math.round(seconds);
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

// The API's timestamps are ISO strings, UTC, sometimes without a zone suffix.
export function parseTime(iso: string): Date {
  return new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(iso) ? iso : `${iso}Z`);
}

export function ago(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "—";
  return `${duration((now - parseTime(iso).getTime()) / 1000)} ago`;
}

export function between(start: string | null, end: string | null, now = Date.now()): number | null {
  if (!start) return null;
  return ((end ? parseTime(end).getTime() : now) - parseTime(start).getTime()) / 1000;
}

export function localTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  return parseTime(iso).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function bytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}
