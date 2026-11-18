// Read-only proxy to the pipeline API. The browser calls /api/sde/<path>; the server adds the
// X-Api-Key, so the key stays in .env.local. Only GET is forwarded: the dashboard never writes.

export const dynamic = "force-dynamic";

export async function GET(request: Request, { params }: { params: Promise<{ path: string[] }> }) {
  const base = process.env.SDE_URL;
  if (!base) {
    return Response.json({ detail: "SDE_URL is not set (web/.env.local)" }, { status: 503 });
  }
  const { path } = await params;
  const search = new URL(request.url).search;
  const target = `${base.replace(/\/$/, "")}/${path.map(encodeURIComponent).join("/")}${search}`;
  try {
    const upstream = await fetch(target, {
      headers: { "X-Api-Key": process.env.SDE_API_KEY ?? "" },
      cache: "no-store",
      signal: AbortSignal.timeout(60_000),
    });
    return new Response(upstream.body, {
      status: upstream.status,
      headers: { "Content-Type": upstream.headers.get("Content-Type") ?? "application/json" },
    });
  } catch (e) {
    return Response.json({ detail: `API unreachable at ${base}: ${String(e)}` }, { status: 502 });
  }
}
