"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ReportView } from "@/components/report-view";
import { QueryState } from "@/components/status";
import { TasksTable } from "@/components/tasks-table";
import { useApi } from "@/lib/api";
import type { Batch, Report } from "@/lib/types";

export default function BatchPage() {
  const { id } = useParams<{ id: string }>();
  const path = `/v1/batches/${encodeURIComponent(id)}`;
  const batch = useApi<Batch>(path);
  const report = useApi<Report>(`${path}/report`);

  return (
    <div className="flex flex-col gap-6">
      <div>
        <Link href="/batches" className="text-sm text-muted-foreground hover:underline">
          ← Batches
        </Link>
        <h1 className="font-mono text-xl font-semibold">{id}</h1>
      </div>
      <QueryState error={report.error} isLoading={report.isLoading} />
      {report.data && <ReportView report={report.data} />}
      <Card>
        <CardHeader>
          <CardTitle>Tasks</CardTitle>
        </CardHeader>
        <CardContent>
          <QueryState error={batch.error} isLoading={batch.isLoading} />
          {batch.data && <TasksTable tasks={batch.data.tasks} />}
        </CardContent>
      </Card>
    </div>
  );
}
