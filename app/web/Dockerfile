# syntax=docker/dockerfile:1
# Multi-stage build for the Next.js (standalone) web app.
# Keep this version in sync with app-ci.yml's actions/setup-node version and
# the invariant documented in AGENTS.md ("CI uses Node 22"). Bump deliberately
# alongside a CI version update (and note the reason here, including which
# dependency actually requires the newer major), not via an unreviewed
# Dependabot major-version merge: see AI4IA#187, where this image drifted to
# node:26-alpine untested even though every runtime dependency (next, eslint,
# typescript, vitest) only required Node 22, and package.json's engines range
# was widened only to stop it contradicting the image, not because of a real
# Node 26 requirement.
FROM node:26-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci --prefer-offline --no-audit

FROM node:26-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build

FROM node:26-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
ENV PORT=8080
ENV HOSTNAME=0.0.0.0

RUN addgroup --system --gid 1001 nodejs \
  && adduser --system --uid 1001 nextjs

COPY --from=builder /app/public ./public
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static

USER nextjs
EXPOSE 8080
CMD ["node", "server.js"]
