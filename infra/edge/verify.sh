#!/bin/sh
# Live verification of the trusted edge (#3) against a running edge stack:
#
#   docker compose -f docker-compose.yml -f infra/edge/compose.yml --profile app up -d --build
#   COMPOSE_PROJECT_NAME=... infra/edge/verify.sh
#
# The victim and the attacker are separate long-lived containers with their own addresses,
# because every connection from the host arrives from one Docker gateway address. Exits
# non-zero on any failure. Expects the default login limit (10 per minute per address).
#
# Those checks cover the edge itself. Whether clients that reach the *published port* keep
# their own source address depends on the Docker host: rootful Linux Docker with iptables
# NAT keeps it for remote clients; Docker Desktop, rootless Docker's default port driver
# and docker-proxy (loopback) replace it with a gateway address, which would put every user
# in one bucket again. That needs a request from another machine, whose address as this
# host sees it you know in advance (its LAN address, or `curl https://ifconfig.me` run on
# it for an internet path):
#
#   (remote)  curl -sk https://HOST:PORT/api/v1/auth/login -H 'content-type: application/json' \
#               -d '{"email":"remote@MARKER.example.com","password":"probe-pass-1"}'
#   (here)    infra/edge/verify.sh --check-remote-probe MARKER REMOTE_IP
#
# It passes only if the API recorded exactly REMOTE_IP. Any other address, including one
# outside Docker's subnets (Docker Desktop's 192.168.65.x, rootless slirp), fails (#39).
set -eu

project=${COMPOSE_PROJECT_NAME:?set COMPOSE_PROJECT_NAME (eval "$(scripts/stack-env.sh)")}
compose="docker compose -f docker-compose.yml -f infra/edge/compose.yml --profile app"
net="${project}_edge"
image=curlimages/curl:8.11.1
victim_c="ql-edge-victim-$$"
attacker_c="ql-edge-attacker-$$"
fail=0

check() { # name expected actual
  if [ "$2" = "$3" ]; then echo "PASS  $1 ($3)"; else echo "FAIL  $1: expected $2, got $3"; fail=1; fi
}
sql() { $compose exec -T postgres psql -U quantlab -tAc "$1"; }
# Exit 0 when the address lies in a Docker network of the edge or the default bridge:
# the edge then saw Docker's address, not the client's.
docker_addr() {
  python3 -c '
import ipaddress, sys
ip = ipaddress.ip_address(sys.argv[1])
sys.exit(0 if any(ip in ipaddress.ip_network(n, strict=False) for n in sys.argv[2:] if n) else 1)
' "$1" $(docker network inspect -f '{{range .IPAM.Config}}{{.Subnet}} {{end}}' "$net" bridge)
}
probe_ip() { # marker -> address the API recorded for that probe's failed login
  sql "select ip from audit_events where action='user.login_failed'
       and details->>'email_domain' = '$1.example.com' order by created_at desc limit 1"
}

# Exit 0 when two addresses are the same (so "::ffff:1.2.3.4" equals "1.2.3.4").
same_addr() {
  python3 -c '
import ipaddress, sys
def norm(s):
    ip = ipaddress.ip_address(s.strip())
    return getattr(ip, "ipv4_mapped", None) or ip
sys.exit(0 if norm(sys.argv[1]) == norm(sys.argv[2]) else 1)
' "$1" "$2" 2>/dev/null
}

if [ "${1:-}" = "--check-remote-probe" ]; then
  usage="usage: verify.sh --check-remote-probe MARKER REMOTE_IP"
  marker=$(printf '%s' "${2:?$usage}" | tr -cd 'a-z0-9-')
  [ -n "$marker" ] || { echo "FAIL  MARKER must contain a-z, 0-9 or -"; exit 2; }
  expected=${3:?$usage}
  ip=$(probe_ip "$marker")
  if [ -z "$ip" ]; then echo "FAIL  no failed login for remote@$marker.example.com reached the API"; exit 1; fi
  if same_addr "$ip" "$expected"; then
    echo "PASS  the remote client was recorded with its own address ($ip)"
    exit 0
  fi
  echo "FAIL  the remote client was recorded as $ip, not $expected: this host does not keep"
  echo "      client source addresses (Docker Desktop, rootless Docker, docker-proxy or a"
  echo "      proxy in front). Every client would share one rate-limit bucket. Do not expose it."
  if docker_addr "$ip"; then echo "      ($ip is inside a Docker network of this stack.)"; fi
  exit 1
fi

cleanup() { docker rm -f "$victim_c" "$attacker_c" >/dev/null 2>&1 || true; }
trap cleanup EXIT

for c in "$victim_c" "$attacker_c"; do
  docker run -d --name "$c" --network "$net" --entrypoint sleep "$image" 600 >/dev/null
done
addr() { docker inspect -f "{{(index .NetworkSettings.Networks \"$net\").IPAddress}}" "$1"; }
victim_ip=$(addr "$victim_c")
attacker_ip=$(addr "$attacker_c")
echo "victim $victim_ip, attacker $attacker_ip"

# curl through the edge: TLS for "localhost" (the default site), connected to the edge.
edge() { # container path [curl args...] -> HTTP status
  c=$1 path=$2
  shift 2
  docker exec "$c" curl -sk -o /dev/null -w '%{http_code}' \
    --connect-to localhost:443:edge:443 "https://localhost$path" "$@"
}
json() { # container path body [curl args...]
  c=$1 path=$2 body=$3
  shift 3
  edge "$c" "$path" -X POST -H 'content-type: application/json' -d "$body" "$@"
}
started=$(sql "select now()")
stamp=$(date +%s)
victim="victim$stamp@example.com"
pw="Correct-horse-9"

echo "== victim registers through the edge"
# Bodies are built outside "$(...)": nested quoting there lets the shell brace-expand them.
reg_body="{\"email\":\"$victim\",\"password\":\"$pw\",\"display_name\":\"victim\"}"
check "victim registered" 201 "$(json "$victim_c" /api/v1/auth/register "$reg_body")"

# Limits use fixed one-minute windows and Docker reuses addresses across runs: start the
# attacker in a fresh window so an earlier run's attempts do not count.
wait=$((61 - $(date +%s) % 60))
echo "== waiting ${wait}s for a fresh rate-limit window"
sleep "$wait"

echo "== attacker: 12 failed logins, each claiming to be the victim"
codes=""
for i in $(seq 1 12); do
  guess="{\"email\":\"guess$i-$stamp@example.com\",\"password\":\"wrong-pass-1\"}"
  codes="$codes $(json "$attacker_c" /api/v1/auth/login "$guess" \
    -H "X-Forwarded-For: $victim_ip" -H "X-Real-IP: $victim_ip" -H "Forwarded: for=$victim_ip")"
done
echo "codes:$codes"
check "attacker's first 10 attempts are 401" "401 401 401 401 401 401 401 401 401 401" \
  "$(echo "$codes" | awk '{print $1,$2,$3,$4,$5,$6,$7,$8,$9,$10}')"
check "attacker then throttled" "429 429" "$(echo "$codes" | awk '{print $11,$12}')"

echo "== the victim, whose address the attacker claimed, is not throttled"
login_body="{\"email\":\"$victim\",\"password\":\"$pw\"}"
check "victim login" 200 "$(json "$victim_c" /api/v1/auth/login "$login_body")"

echo "== the API recorded the attacker's real address, never the claimed one"
since="created_at >= '$started'"
check "failed logins recorded as the victim's address" 0 \
  "$(sql "select count(*) from audit_events where action='user.login_failed' and ip='$victim_ip' and $since")"
check "failed logins recorded as the attacker's address" 10 \
  "$(sql "select count(*) from audit_events where action='user.login_failed' and ip='$attacker_ip' and $since")"
check "victim's login recorded with the victim's address" "$victim_ip" \
  "$(sql "select ip from audit_events e join users u on u.id = e.user_id where e.action='user.login' and u.email='$victim'")"

echo "== bypassing the edge"
direct() { docker exec "$attacker_c" curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$@" || true; }
check "web API proxy without the edge secret" 403 \
  "$(direct -H "X-Forwarded-For: $victim_ip" http://web:3000/api/v1/health)"
check "web page with a forged edge secret" 403 \
  "$(direct -H 'X-QuantLab-Edge: guess' http://web:3000/login)"
check "API not reachable from the edge network" 000 "$(direct http://api:8000/api/v1/health)"
check "client cannot override the edge secret header" 200 \
  "$(edge "$attacker_c" /api/v1/health -H 'X-QuantLab-Edge: guess')"

echo "== the edge caps request bodies (110 MB)"
check "121 MB body rejected at the edge" 413 "$(docker exec "$attacker_c" sh -c \
  "head -c 121000000 /dev/zero | curl -sk -o /dev/null -w '%{http_code}' \
   --connect-to localhost:443:edge:443 https://localhost/api/v1/auth/login \
   -H 'content-type: application/json' --data-binary @-")"

echo "== only the edge publishes a host port"
check "services other than the edge publishing ports" 0 \
  "$($compose ps --format '{{.Service}} {{.Ports}}' | grep -v '^edge ' | grep -c -- '->' || true)"

echo "== a client of the published port (from this host)"
marker="local-$stamp"
port=${QUANTLAB_EDGE_PORT:-8443}
curl -sk -o /dev/null "https://localhost:$port/api/v1/auth/login" -H 'content-type: application/json' \
  -d "{\"email\":\"local@$marker.example.com\",\"password\":\"probe-pass-1\"}" || true
local_ip=$(probe_ip "$marker")
if [ -n "$local_ip" ] && docker_addr "$local_ip"; then
  echo "INFO  recorded as $local_ip, a Docker address: expected for a connection from this host,"
  echo "      so it proves nothing about remote clients. Run the remote probe (header comment)."
else
  echo "INFO  recorded as ${local_ip:-nothing}"
fi

if [ "$fail" -eq 0 ]; then
  echo "ALL EDGE CHECKS PASSED. Before exposing, also pass: verify.sh --check-remote-probe MARKER REMOTE_IP"
else
  echo "SOME CHECKS FAILED"; exit 1
fi
