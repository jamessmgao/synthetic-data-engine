"use client";

import Link from "next/link";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import { cn } from "cn";
import { QueryState } from "@/components/status";
import { useApi } from "@/lib/api";
import { bytes } from "@/lib/format";
import type { ArtifactFile, ArtifactListing } from "@/lib/types";

const TITLES: Record<string, string> = { bundles: "Task bundle", worlds: "World", traces: "Trace" };
// Opened first when no file is picked: the task statement, then whatever explains a run.
const DEFAULT_FILES = ["instruction.md", "README.md", "result.json", "task.toml"];

export default function ArtifactPage() {
  return (
    <Suspense>
      <ArtifactView />
    </Suspense>
  );
}

function ArtifactView() {
  const { kind, key } = useParams<{ kind: string; key: string }>();
  const search = useSearchParams();
  const router = useRouter();
  const [filter, setFilter] = useState("");
  const base = `/v1/artifacts/${kind}/${encodeURIComponent(key)}`;
  // Artifacts are immutable (content-addressed, or a finished trace): no refresh needed.
  const listing = useApi<ArtifactListing>(base, { refresh: false });
  const files = listing.data?.files ?? [];
  const picked =
    search.get("path") ??
    DEFAULT_FILES.map((name) => files.find((f) => f.path === name || f.path.endsWith(`/${name}`))?.path).find(Boolean) ??
    files[0]?.path ??
    null;
  const file = useApi<ArtifactFile>(picked ? `${base}/file?path=${encodeURIComponent(picked)}` : null, { refresh: false });
  const shown = files.filter((f) => !filter || f.path.toLowerCase().includes(filter.toLowerCase()));

  return (
    <div className="flex flex-col gap-4">
      <div>
        <button onClick={() => router.back()} className="text-sm text-muted-foreground hover:underline">
          ← Back
        </button>
        <h1 className="text-xl font-semibold">
          {TITLES[kind] ?? kind} <span className="font-mono text-base font-normal text-muted-foreground">{key}</span>
        </h1>
      </div>
      <QueryState error={listing.error} isLoading={listing.isLoading} />
      {listing.data && (
        <div className="grid gap-4 md:grid-cols-[18rem_minmax(0,1fr)]">
          <aside className="flex flex-col gap-2">
            {files.length > 15 && (
              <input
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
                placeholder="Filter files"
                className="h-8 rounded-md border bg-transparent px-2 text-sm"
              />
            )}
            <div className="max-h-[75vh] overflow-auto rounded-lg border p-1">
              {shown.map((f) => (
                <Link
                  key={f.path}
                  href={`?path=${encodeURIComponent(f.path)}`}
                  replace
                  className={cn(
                    "flex items-baseline justify-between gap-2 rounded px-2 py-1 font-mono text-xs hover:bg-muted",
                    f.path === picked && "bg-muted font-semibold",
                  )}
                  title={f.path}
                >
                  <span className="truncate" style={{ paddingLeft: `${(f.path.split("/").length - 1) * 0.6}rem` }}>
                    {f.path.split("/").length > 1 ? <span className="text-muted-foreground">{f.path.slice(0, f.path.lastIndexOf("/") + 1)}</span> : null}
                    {f.path.split("/").pop()}
                  </span>
                  <span className="shrink-0 text-muted-foreground">{bytes(f.size)}</span>
                </Link>
              ))}
            </div>
            {listing.data.total > files.length && (
              <p className="text-xs text-muted-foreground">Showing {files.length} of {listing.data.total} files.</p>
            )}
          </aside>
          <section className="min-w-0 rounded-lg border">
            <div className="flex items-center justify-between border-b px-3 py-2 font-mono text-xs">
              <span>{picked ?? "No files"}</span>
              {file.data && <span className="text-muted-foreground">{bytes(file.data.size)}{file.data.truncated && " · first 1 MB"}</span>}
            </div>
            <div className="p-3">
              <QueryState error={file.error} isLoading={file.isLoading} />
              {file.data?.binary && <p className="text-sm text-muted-foreground">Binary file.</p>}
              {file.data?.text != null && <FileText path={file.data.path} text={file.data.text} />}
            </div>
          </section>
        </div>
      )}
    </div>
  );
}

function FileText({ path, text }: { path: string; text: string }) {
  let body = text;
  if (path.endsWith(".json")) {
    try {
      body = JSON.stringify(JSON.parse(text), null, 2);
    } catch {}
  }
  return <pre className="max-h-[75vh] overflow-auto text-xs leading-relaxed whitespace-pre-wrap">{body}</pre>;
}
