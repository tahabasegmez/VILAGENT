import { type NextRequest } from "next/server";

const BACKEND_BASE_URL =
  process.env.VILAGENT_INTERNAL_GATEWAY_BASE_URL ?? "http://127.0.0.1:8001";
const INTERNAL_AUTH_TOKEN = process.env.VILAGENT_INTERNAL_AUTH_TOKEN;
const INTERNAL_AUTH_HEADER = "X-VILAGENT-Internal-Token";

type RouteContext = {
  params: Promise<{ path?: string[] }>;
};

function buildBackendUrl(path: string[], search: string) {
  const pathname = `/api/computer-use/${path.map(encodeURIComponent).join("/")}`;
  const url = new URL(pathname, BACKEND_BASE_URL);
  url.search = search;
  return url;
}

async function proxyComputerUseRequest(
  request: NextRequest,
  context: RouteContext,
) {
  if (!INTERNAL_AUTH_TOKEN) {
    return Response.json(
      {
        detail:
          "VILAGENT_INTERNAL_AUTH_TOKEN is required for the VILAGENT computer-use proxy.",
      },
      { status: 503 },
    );
  }

  const { path = [] } = await context.params;
  const backendUrl = buildBackendUrl(path, request.nextUrl.search);
  const headers = new Headers();
  const contentType = request.headers.get("content-type");
  const accept = request.headers.get("accept");
  if (contentType) headers.set("content-type", contentType);
  if (accept) headers.set("accept", accept);
  headers.set(INTERNAL_AUTH_HEADER, INTERNAL_AUTH_TOKEN);

  const response = await fetch(backendUrl, {
    method: request.method,
    headers,
    body:
      request.method === "GET" || request.method === "HEAD"
        ? undefined
        : request.body,
    // Node.js 18+ undici requires duplex: "half" when streaming the body
    ...((request.method !== "GET" && request.method !== "HEAD") ? { duplex: "half" } : {}),
    cache: "no-store",
  }).catch((error: any) => {
    const message = error instanceof Error 
      ? error.message + (error.cause ? ` (cause: ${error.cause})` : "") 
      : String(error);
    return Response.json(
      {
        detail: `VILAGENT Gateway is not reachable at ${backendUrl.origin}: ${message}`,
      },
      { status: 503 },
    );
  });

  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers: response.headers,
  });
}

export async function GET(request: NextRequest, context: RouteContext) {
  return proxyComputerUseRequest(request, context);
}

export async function POST(request: NextRequest, context: RouteContext) {
  return proxyComputerUseRequest(request, context);
}

export async function PUT(request: NextRequest, context: RouteContext) {
  return proxyComputerUseRequest(request, context);
}

export async function PATCH(request: NextRequest, context: RouteContext) {
  return proxyComputerUseRequest(request, context);
}

export async function DELETE(request: NextRequest, context: RouteContext) {
  return proxyComputerUseRequest(request, context);
}
