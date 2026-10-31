import { createHash, timingSafeEqual } from "node:crypto";
import { isIP } from "node:net";

// Trusted TLS edge (#3, ADR-0008, infra/edge/). With QUANTLAB_EDGE_SECRET set, the web tier
// serves only requests that carry the edge's X-QuantLab-Edge header, and trusts the one
// X-Forwarded-For value the edge wrote (it overwrites the header with its TCP peer).
// Without the secret (local development, no edge) no client address is forwarded at all:
// Next keeps a client-supplied X-Forwarded-For as is, so anything forwarded would be
// chosen by the client. The API then skips its per-address limit instead of putting
// every user into this server's bucket.

const EDGE_HEADER = "x-quantlab-edge";

function digest(value: string): Buffer {
  return createHash("sha256").update(value).digest();
}

/** The configured edge secret, or null in no-edge mode. Read per request (portable images). */
export function edgeSecret(): string | null {
  const s = process.env.QUANTLAB_EDGE_SECRET ?? "";
  return s.length > 0 ? s : null;
}

/** True when the request may be served: no edge configured, or it came through the edge. */
export function fromEdge(headers: Headers): boolean {
  const secret = edgeSecret();
  if (secret === null) return true;
  const given = headers.get(EDGE_HEADER);
  return given !== null && timingSafeEqual(digest(given), digest(secret));
}

/** The client address to forward to the API, or null when there is none to trust. */
export function edgeClientAddress(headers: Headers): string | null {
  if (edgeSecret() === null || !fromEdge(headers)) return null;
  const value = (headers.get("x-forwarded-for") ?? "").trim();
  // The edge writes exactly one address; anything else is not from the edge.
  return isIP(value) ? value : null;
}

export function edgeRequiredResponse(): Response {
  return Response.json(
    { error: { code: "edge_required", message: "This server only accepts requests through its TLS edge." } },
    { status: 403 },
  );
}
