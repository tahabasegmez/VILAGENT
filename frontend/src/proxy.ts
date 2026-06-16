import { NextResponse, type NextRequest } from "next/server";

const LEGACY_UI_PREFIXES = [
  "/login",
  "/setup",
  "/workspace",
  "/legacy",
  "/blog",
  "/docs",
  "/en",
  "/zh",
];

export function proxy(request: NextRequest) {
  const { pathname } = request.nextUrl;
  if (
    LEGACY_UI_PREFIXES.some(
      (prefix) => pathname === prefix || pathname.startsWith(`${prefix}/`),
    )
  ) {
    return NextResponse.redirect(new URL("/operator", request.url));
  }
  return NextResponse.next();
}

export const config = {
  matcher: ["/((?!api|_next|favicon.ico).*)"],
};
