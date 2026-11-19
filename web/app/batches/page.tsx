"use client";

import { BatchesTable } from "@/components/batches-table";
import { OtherScopeNote, useScope } from "@/components/dev-toggle";
import { QueryState } from "@/components/status";
import { useApi } from "@/lib/api";
import type { BatchSummary } from "@/lib/types";

export default function BatchesPage() {
  const batches = useApi<BatchSummary[]>("/v1/batches");
  const [scope] = useScope();
  const every = batches.data ?? [];
  const shown = every.filter((b) => b.dev === (scope === "dev"));
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <h1 className="text-xl font-semibold">Batches</h1>
        <OtherScopeNote scope={scope} other={batches.data ? every.length - shown.length : undefined} />
      </div>
      <QueryState error={batches.error} isLoading={batches.isLoading} />
      {batches.data && <BatchesTable batches={shown} />}
    </div>
  );
}
