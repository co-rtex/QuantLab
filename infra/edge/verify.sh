#!/bin/sh
# Live verification of the trusted edge (#3) against a running edge stack:
#
#   docker compose -f docker-compose.yml -f infra/edge/compose.yml --profile app up -d --build
#   COMPOSE_PROJECT_NAME=... infra/edge/verify.sh
#
# The victim and the attacker are separate long-lived containers with their own addresses,
# because every connection from the host arrives from one Docker gateway address. Exits
# non-zero on any failure. Expects the default login limit (10 per minute per address).
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
sql() { $compose exec -T postgres psql -U quantlab -tAc "$1"; }

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

if [ "$fail" -eq 0 ]; then echo "ALL CHECKS PASSED"; else echo "SOME CHECKS FAILED"; exit 1; fi
