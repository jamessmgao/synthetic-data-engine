"use client";

import { BatchesTable } from "@/components/batches-table";
import { QueryState } from "@/components/status";
import { useApi } from "@/lib/api";
import type { BatchSummary } from "@/lib/types";

export default function BatchesPage() {
  const batches = useApi<BatchSummary[]>("/v1/batches");
  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-xl font-semibold">Batches</h1>
      <QueryState error={batches.error} isLoading={batches.isLoading} />
      {batches.data && <BatchesTable batches={batches.data} />}
    </div>
  );
}
