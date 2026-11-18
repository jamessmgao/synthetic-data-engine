"use client";

import { useQuery } from "@tanstack/react-query";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

export async function apiGet<T>(path: string): Promise<T> {
  const res = await fetch(`/api/sde${path}`, { cache: "no-store" });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {}
    throw new ApiError(res.status, detail);
  }
  return res.json();
}

// Everything the pipeline does changes on the scale of seconds to minutes; 10 s is live enough.
export const REFRESH_MS = 10_000;

export function useApi<T>(path: string | null, opts: { refresh?: boolean } = {}) {
  return useQuery<T, Error>({
    queryKey: [path],
    queryFn: () => apiGet<T>(path!),
    enabled: path !== null,
    refetchInterval: opts.refresh === false ? false : REFRESH_MS,
  });
}
