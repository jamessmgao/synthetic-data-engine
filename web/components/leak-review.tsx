"use client";

import Link from "next/link";
import { Badge } from "@/components/ui/badge";
import { usd } from "@/lib/format";
import type { LeakReview } from "@/lib/types";

// The leak gate on one bundle (validator/gates.py gate_leak): the semantic review's verdict and
// quotes, and the blind check (a model that saw only the instruction predicts the fix; a judge
// compares it with the real fix: only "specific" is a leak). Shown whether it passed or not.
export function LeakReviewPanel({ review, title }: { review: LeakReview; title: string }) {
  const judge = review.blind?.judge;
  const prediction = review.blind?.prediction;
  const passed = review.passed;
  return (
    <div className="flex flex-col gap-3 rounded-lg border p-3 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">{title}</span>
        <Badge variant={passed === true ? "success" : passed === false ? "destructive" : "outline"}>
          {passed === true ? "passed" : passed === false ? "failed" : "—"}
        </Badge>
        {review.skipped && <span className="text-xs text-muted-foreground">skipped: {review.skipped}</span>}
        <span className="text-xs text-muted-foreground">
          {review.job_type}
          {review.origin ? ` (${review.origin})` : ""} · {review.job_id}
          {review.adopted ? " · adopted" : ""} · {usd(review.cost_usd)}
        </span>
        {review.raw && (
          <Link
            href={`/artifacts/${review.raw.kind}/${encodeURIComponent(review.raw.key)}?path=${encodeURIComponent(review.raw.path)}`}
            className="ml-auto text-xs underline underline-offset-4"
          >
            raw review.json
          </Link>
        )}
      </div>

      {!review.skipped && (
        <>
          <section className="flex flex-col gap-1">
            <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
              <span className="font-medium text-foreground">Semantic review</span>
              <Badge variant={review.verdict === "clean" ? "success" : review.verdict === "leak" ? "destructive" : "outline"}>
                {review.verdict ?? "—"}
              </Badge>
              <span>
                {review.direct ?? 0} direct · {review.strong_hints ?? 0} strong hint{review.strong_hints === 1 ? "" : "s"}
              </span>
            </div>
            {(review.leaks ?? []).length === 0 && <p className="text-xs text-muted-foreground">No sentence found that gives the fix away.</p>}
            {(review.leaks ?? []).map((l, i) => (
              <div key={i} className="rounded-md bg-muted/50 p-2">
                <div className="font-mono text-xs">“{l.quote}”</div>
                <div className="mt-1 text-xs text-muted-foreground">
                  <Badge variant={l.severity === "direct" ? "destructive" : "warning"} className="mr-1">
                    {l.severity}
                  </Badge>
                  reveals: {l.reveals}
                </div>
              </div>
            ))}
            {(review.unverified_quotes ?? []).length > 0 && (
              <p className="text-xs text-muted-foreground">
                {review.unverified_quotes!.length} finding(s) ignored: their quote isn&apos;t in the instruction.
              </p>
            )}
          </section>

          <section className="flex flex-col gap-1">
            <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
              <span className="font-medium text-foreground">Blind check</span>
              {review.blind?.skipped ? (
                <span>skipped: {review.blind.skipped}</span>
              ) : judge ? (
                <Badge variant={judge.match === "specific" ? "destructive" : "success"}>judge: {judge.match}</Badge>
              ) : (
                <Badge variant="outline">inconclusive</Badge>
              )}
            </div>
            {prediction && (
              <div className="rounded-md bg-muted/50 p-2 text-xs">
                <div className="text-muted-foreground">Predicted from the instruction alone</div>
                {prediction.predicted_fix && (
                  <p className="mt-1 whitespace-pre-wrap">
                    <span className="font-medium">Fix: </span>
                    {prediction.predicted_fix}
                  </p>
                )}
                {prediction.predicted_failure && (
                  <p className="mt-1 whitespace-pre-wrap">
                    <span className="font-medium">Failure: </span>
                    {prediction.predicted_failure}
                  </p>
                )}
              </div>
            )}
            {judge && (
              <div className="rounded-md bg-muted/50 p-2 text-xs">
                <div className="text-muted-foreground">Judge, against the real fix</div>
                <p className="mt-1 whitespace-pre-wrap">{judge.why}</p>
              </div>
            )}
          </section>
          {(review.inconclusive ?? []).length > 0 && (
            <p className="text-xs text-amber-700 dark:text-amber-400">Inconclusive: {review.inconclusive!.join(", ")}</p>
          )}
        </>
      )}
    </div>
  );
}
