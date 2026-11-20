const WORKER_ORIGIN = /^https:\/\/cauche-pulse\.[a-z0-9-]+\.workers\.dev$/;

export function denied(status) {
  return new Response(status === 403 ? "Access denied" : "Service unavailable", {
    status,
    headers: {"Cache-Control": "no-store", "Content-Type": "text/plain; charset=utf-8"},
  });
}

export function configured(env) {
  return Boolean(
    WORKER_ORIGIN.test(env.APP_ORIGIN ?? "") &&
    /^[0-9a-f]{64}$/.test(env.CF_ACCESS_AUD ?? "") &&
    /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(env.CAUCHE_OWNER_EMAIL ?? "")
  );
}

export async function authorize(request, env, access) {
  if (!configured(env)) return denied(503);
  if (new URL(request.url).origin !== env.APP_ORIGIN || access?.aud !== env.CF_ACCESS_AUD) {
    return denied(403);
  }
  try {
    const identity = await access.getIdentity();
    if (identity?.email?.toLowerCase() === env.CAUCHE_OWNER_EMAIL.toLowerCase()) return null;
  } catch {
    // The runtime-verified Access identity could not be obtained.
  }
  return denied(403);
}

export function containerRequest(request) {
  const url = new URL(request.url);
  url.protocol = "http:";
  url.host = "container";
  const forwarded = new Request(url, request);
  forwarded.headers.delete("host");
  forwarded.headers.delete("Authorization");
  forwarded.headers.delete("Cf-Access-Jwt-Assertion");
  const appCookies = (forwarded.headers.get("Cookie") ?? "")
    .split(";")
    .map(value => value.trim())
    .filter(value => value.startsWith("__Host-cauche="));
  if (appCookies.length) {
    forwarded.headers.set("Cookie", appCookies.join("; "));
  } else {
    forwarded.headers.delete("Cookie");
  }
  return forwarded;
}

export function privateResponse(response, origin) {
  if (response.status === 101) return response;
  const secured = new Response(response.body, response);
  secured.headers.set("Cache-Control", "private, no-store");
  secured.headers.set("Content-Security-Policy", [
    "default-src 'self'", "script-src 'self'", "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data:", "connect-src 'self' " + origin.replace("https:", "wss:"),
    "object-src 'none'", "frame-ancestors 'none'", "base-uri 'none'", "form-action 'self'",
  ].join("; "));
  secured.headers.set("X-Content-Type-Options", "nosniff");
  secured.headers.set("X-Frame-Options", "DENY");
  secured.headers.set("Referrer-Policy", "no-referrer");
  secured.headers.set("Permissions-Policy", "camera=(), microphone=(), geolocation=()");
  return secured;
}
