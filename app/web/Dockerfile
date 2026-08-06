# syntax=docker/dockerfile:1
# Multi-stage build for the Next.js (standalone) web app.
#
# ---------------------------------------------------------------------------
# Base image is pinned to an immutable DIGEST, not just the moving `22-alpine`
# tag (audit finding P1-7). A tag is a mutable pointer: docker-build.yml
# resolves it when the PR runs and `azd deploy` resolves it again later, so the
# image CI proved and the image production runs can be different builds of
# different Node patch releases with no diff anywhere in the repo to show it.
# `docker.io/library/node:22-alpine` moved to the digest below on 2026-07-29;
# the tag itself carries no record of that. With the digest present, the two
# builds either use the same bytes or the pull fails loudly.
#
# The TAG is kept in the reference alongside the digest on purpose: it is what
# Dependabot's docker ecosystem tracks (its `FROM_LINE` regex parses
# `image:tag@sha256:...` and `UpdateChecker#updated_requirements` rewrites the
# tag and the digest together), and it keeps the major legible to a human.
#
# Do NOT assume the digest keeps itself fresh. dependabot-core suppresses a
# digest-only refresh of an UNCHANGED comparable tag behind the server-side
# `docker_digest_only_update_suppression` experiment, whose state is not
# observable from this repo -- and `22-alpine` is a floating tag whose NAME
# never changes, so if that experiment is on, no PR is ever opened here.
# Treat refreshing this digest as a manual audit step.
#
# Currently sha256:c610fcdf... = the multi-platform OCI image index for
# node 22.23.2 (linux/amd64, arm64/v8, arm/v6, arm/v7, s390x), published
# 2026-07-29 and pinned 2026-08-06.
#
# To refresh:
#   docker buildx imagetools inspect node:22-alpine --format '{{json .Manifest.Digest}}'
#
# That prints the MANIFEST LIST (OCI image index) digest, which is the only
# correct value here -- verified: for this tag it returns exactly the digest
# pinned above, and `--format '{{.Manifest.MediaType}}'` on the same reference
# returns `application/vnd.oci.image.index.v1+json`.
#
# Two ways to get a PLATFORM-SPECIFIC digest by accident, both of which look
# identical in shape and neither of which CI can catch (docker-build runs on
# amd64, so an amd64-only pin passes every check):
#
#   1. `docker inspect` on a locally pulled image. The daemon only ever holds
#      the manifest for ITS platform, so RepoDigests/Id give you the amd64
#      digest, not the index. This is the likelier mistake, because
#      `docker inspect` is the more familiar command.
#   2. Copying from the `Manifests:` block that bare
#      `docker buildx imagetools inspect node:22-alpine` prints underneath the
#      index digest -- one entry per platform (linux/amd64 is currently
#      sha256:76789712..., which is NOT what is pinned above).
#
# Either mistake makes the image unbuildable on every other architecture, and
# it fails confusingly: a local `azd deploy` from an arm64 Mac reports a
# manifest/platform mismatch that never mentions the pin.
#
# All three FROM lines below must carry the SAME digest --
# scripts/tests/test_base_image_pins.py fails if they drift apart, and a
# wrong digest fails docker-build.yml on the PR that introduces it.
#
# Keep the MAJOR in sync with app-ci.yml's actions/setup-node version and the
# invariant documented in AGENTS.md ("CI uses Node 22"). Bump it deliberately
# alongside a CI version update (and note the reason here, including which
# dependency actually requires the newer major), not via an unreviewed
# Dependabot major-version merge: see AI4IA#187, where this image drifted to
# node:26-alpine untested even though every runtime dependency (next, eslint,
# typescript, vitest) only required Node 22, and package.json's engines range
# was widened only to stop it contradicting the image, not because of a real
# Node 26 requirement. .github/dependabot.yml ignores majors here for exactly
# that reason.
# ---------------------------------------------------------------------------
FROM node:22-alpine@sha256:c610fcdfb1d5b4740dd70c284ed3cb16bb857e0f7166196e36a5501df7a3aa32 AS deps
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci --prefer-offline --no-audit

FROM node:22-alpine@sha256:c610fcdfb1d5b4740dd70c284ed3cb16bb857e0f7166196e36a5501df7a3aa32 AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build

FROM node:22-alpine@sha256:c610fcdfb1d5b4740dd70c284ed3cb16bb857e0f7166196e36a5501df7a3aa32 AS runner
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
