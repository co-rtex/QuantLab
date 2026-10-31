#!/usr/bin/env bash
# Local checks of the edge Caddyfiles (#39, ADR-0008, ADR-0012). This is NOT the #39 gate:
# it runs on one machine, on an internal Docker network, with no NLB and no second host.
#
#   infra/terraform/edge/check-proxy-protocol.sh
#
# 1. `caddy adapt` and `caddy validate` for infra/edge/Caddyfile and this directory's
#    Caddyfile, with placeholder environment values.
# 2. Behaviour of this directory's Caddyfile (PROXY protocol behind an NLB): an edge, an
#    upstream that echoes the headers it receives, and clients at chosen addresses on an
#    internal network. Each client sends a spoofed X-Forwarded-For, Forwarded and
#    X-Real-IP; the PROXY header, when sent, states 203.0.113.7. v2 (binary) is what the
#    NLB sends (ADR-0012); v1 (text) is checked too.
#      A, A2  allowed source, PROXY v1 / v2  -> 200, X-Forwarded-For = 203.0.113.7
#      B      allowed source, no PROXY header -> 200, X-Forwarded-For = its own address
#      C, C2  other source,   PROXY v1 / v2  -> 400 (the header is rejected)
#      D      other source,   no PROXY header -> 200, X-Forwarded-For = its own address
#    In every 200, Forwarded and X-Real-IP are removed and X-QuantLab-Edge is set.
#
# Uses only the pinned image below (`--pull never`): pull it first if it is missing.
# The edge secret is a placeholder; nothing here is a real credential.
set -euo pipefail

IMG=caddy:2.10.2-alpine
NET=ql-caddy-proxytest
SUBNET=172.31.250
PH=placeholder-not-a-secret-0123456789abcdef
REPO=$(cd "$(dirname "$0")/../../.." && pwd)
WORK=$(mktemp -d)
fail=0

cleanup() {
  docker rm -f ql-edge ql-echo >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

caddy_run() { # caddyfile, then docker options and the caddy command
  local cf=$1; shift
  docker run --rm --pull never --network none --read-only --tmpfs /data --tmpfs /config \
    -v "$cf":/etc/caddy/Caddyfile:ro "$@"
}

echo "== caddy $(docker run --rm --pull never "$IMG" caddy version | cut -d' ' -f1)"
for name in edge terraform; do
  if [ "$name" = edge ]; then
    cf=$REPO/infra/edge/Caddyfile
    env=(-e QUANTLAB_EDGE_SECRET=$PH -e QUANTLAB_EDGE_SITE=localhost -e QUANTLAB_EDGE_TLS=internal)
  else
    cf=$REPO/infra/terraform/edge/Caddyfile
    env=(-e QUANTLAB_EDGE_SECRET=$PH -e QUANTLAB_EDGE_UPSTREAM=web.internal:3000
      -e "QUANTLAB_EDGE_PROXY_CIDRS=10.0.0.0/24 10.0.1.0/24")
  fi
  for cmd in adapt validate; do
    if caddy_run "$cf" "${env[@]}" "$IMG" caddy "$cmd" --config /etc/caddy/Caddyfile \
      --adapter caddyfile >/dev/null 2>"$WORK/err"; then
      echo "ok    caddy $cmd $name"
    else
      echo "FAIL  caddy $cmd $name"; cat "$WORK/err"; fail=1
    fi
  done
done

cat >"$WORK/echo.Caddyfile" <<'CADDY'
{
	admin off
}
:3000 {
	respond "xff=[{header.X-Forwarded-For}] edge=[{header.X-Quantlab-Edge}] fwd=[{header.Forwarded}] xri=[{header.X-Real-IP}]"
}
CADDY

docker network create --internal --subnet "$SUBNET.0/24" "$NET" >/dev/null
docker run -d --rm --pull never --name ql-echo --network "$NET" --ip "$SUBNET.5" \
  -v "$WORK/echo.Caddyfile":/etc/caddy/Caddyfile:ro "$IMG" \
  caddy run --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null
docker run -d --rm --pull never --name ql-edge --network "$NET" --ip "$SUBNET.10" \
  --read-only --tmpfs /data --tmpfs /config \
  -e QUANTLAB_EDGE_SECRET=$PH -e QUANTLAB_EDGE_UPSTREAM="$SUBNET.5:3000" \
  -e QUANTLAB_EDGE_PROXY_CIDRS="$SUBNET.20/32" \
  -v "$REPO/infra/terraform/edge/Caddyfile":/etc/caddy/Caddyfile:ro "$IMG" \
  caddy run --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null
# Wait until the edge answers (a fixed sleep flakes on a loaded machine).
ready=0
for _ in $(seq 1 40); do
  if docker run --rm --pull never --network "$NET" --ip "$SUBNET.20" "$IMG" \
    sh -c "wget -q -O /dev/null http://$SUBNET.10:8080/" >/dev/null 2>&1; then
    ready=1; break
  fi
  sleep 0.5
done
[ "$ready" = 1 ] || { echo "FAIL  the edge did not become ready"; exit 1; }

HTTP='GET / HTTP/1.1\r\nHost: x\r\nX-Forwarded-For: 6.6.6.6\r\nForwarded: for=6.6.6.6\r\nX-Real-IP: 6.6.6.6\r\nConnection: close\r\n\r\n'
PROXY_V1="PROXY TCP4 203.0.113.7 $SUBNET.10 40000 8080\\r\\n"
# PROXY v2, as the NLB sends it: signature, 0x21 (v2, PROXY), 0x11 (TCP over IPv4), length
# 12, then 203.0.113.7 -> 172.31.250.10, ports 40000 -> 8080. Octal, for busybox printf.
PROXY_V2='\015\012\015\012\000\015\012\121\125\111\124\012\041\021\000\014\313\000\161\007\254\037\372\012\234\100\037\220'

probe() { # case, source address, PROXY header (v1/v2/no), expected status, expected XFF
  local payload=$HTTP out status body
  [ "$3" = v1 ] && payload=$PROXY_V1$HTTP
  [ "$3" = v2 ] && payload=$PROXY_V2$HTTP
  # Keep the connection open after the request: a half-closed client makes Caddy cancel
  # the proxied request.
  out=$(docker run --rm --pull never --network "$NET" --ip "$2" "$IMG" \
    sh -c "(printf '$payload'; sleep 2) | nc -w 5 $SUBNET.10 8080" 2>&1 | tr -d '\r' || true)
  status=$(printf '%s\n' "$out" | head -1 | cut -d' ' -f2)
  body=$(printf '%s\n' "$out" | tail -1)
  local want="xff=[$5] edge=[$PH] fwd=[] xri=[]"
  if [ "$status" = "$4" ] && { [ "$4" != 200 ] || [ "$body" = "$want" ]; }; then
    echo "ok    $1: from $2, PROXY header $3 -> $status ${body:+($body)}"
  else
    echo "FAIL  $1: from $2, PROXY header $3 -> $status, body: $body (wanted $4 ${5:+xff=[$5]})"
    fail=1
  fi
}

probe A "$SUBNET.20" v1 200 203.0.113.7
probe A2 "$SUBNET.20" v2 200 203.0.113.7
probe B "$SUBNET.20" no 200 "$SUBNET.20"
probe C "$SUBNET.30" v1 400 ""
probe C2 "$SUBNET.30" v2 400 ""
probe D "$SUBNET.30" no 200 "$SUBNET.30"

if [ "$fail" -eq 0 ]; then
  echo "all local edge checks passed (this does not clear the #39 gate)"
fi
exit "$fail"
