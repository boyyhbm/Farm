# syntax=docker/dockerfile:1
FROM node:24-bookworm-slim AS builder
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY apps/dashboard ./
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/proxy_ca; fi; \
    npm ci && npm run build
FROM node:24-bookworm-slim AS runner
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 HOSTNAME=0.0.0.0 PORT=3000
WORKDIR /app
RUN useradd --uid 10001 --create-home farm
COPY --from=builder --chown=farm:farm /app/.next/standalone ./
COPY --from=builder --chown=farm:farm /app/.next/static ./.next/static
COPY --from=builder --chown=farm:farm /app/public ./public
USER farm
EXPOSE 3000
CMD ["node", "server.js"]
