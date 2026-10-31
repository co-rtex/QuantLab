# The AWS edge image: Caddy with the NLB/PROXY-protocol Caddyfile baked in (Fargate cannot
# bind-mount config files). Build and push to the edge ECR repository (README.md).
FROM caddy:2.10.2-alpine
COPY Caddyfile /etc/caddy/Caddyfile
# Refuse to start without a real edge secret: an empty one would be sent and accepted.
CMD ["sh", "-c", "[ \"${#QUANTLAB_EDGE_SECRET}\" -ge 32 ] || { echo 'QUANTLAB_EDGE_SECRET must be at least 32 characters' >&2; exit 1; }; exec caddy run --config /etc/caddy/Caddyfile --adapter caddyfile"]
