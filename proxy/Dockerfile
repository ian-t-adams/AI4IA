# AI4IA-owned wrapper for microsoft/SimpleL7Proxy (MIT) at
# d9eb1d1fa42820792a9699bfc253562fba07d977; the file-level provenance is in
# proxy/upstream-provenance.json. Refresh these OCI manifest-list digests with:
# docker buildx imagetools inspect <tag> --format '{{json .Manifest.Digest}}'
FROM mcr.microsoft.com/dotnet/sdk:10.0@sha256:e1ffd2a92ae84c1291bc1b6887501f8af98e6331e7af6d4c8d37168c5e87a64c AS build-env
WORKDIR /app

# Copy restore inputs first so the locked restore is cached independently of source changes.
COPY Directory.Build.props ./
COPY Shared/Shared.csproj ./Shared/
COPY Shared/packages.lock.json ./Shared/
COPY Shared-parser/Shared-parser.csproj ./Shared-parser/
COPY Shared-parser/packages.lock.json ./Shared-parser/
COPY SimpleL7Proxy/SimpleL7Proxy.csproj ./SimpleL7Proxy/
COPY SimpleL7Proxy/global.json ./SimpleL7Proxy/
COPY SimpleL7Proxy/packages.lock.json ./SimpleL7Proxy/
WORKDIR /app/SimpleL7Proxy
RUN dotnet restore --locked-mode

# Copy the rest of the source and publish.
WORKDIR /app
COPY Shared/ ./Shared/
COPY Shared-parser/ ./Shared-parser/
COPY SimpleL7Proxy/ ./SimpleL7Proxy/
WORKDIR /app/SimpleL7Proxy
RUN dotnet publish -c Release -o /app/out --no-restore

FROM mcr.microsoft.com/dotnet/aspnet:10.0-noble-chiseled@sha256:0839314d08bb65da369135389a5d8291f75ace587fbb0488f469eb92c62eef68
WORKDIR /app
COPY --from=build-env /app/out .
# Default per-user policy + Entra App-ID allowlist (overridable via App Config).
COPY SimpleL7Proxy/config.json .
COPY SimpleL7Proxy/auth.json .

# Main L7 listener (Port env, default 8000 -> we set 8080), including
# /readiness, /startup, and /liveness.
EXPOSE 8080

# Workstation GC returns memory to the OS more aggressively under bursty load.
ENV DOTNET_GCConserveMemory=9
ENV DOTNET_gcServer=0

# The proxy is a Worker (no Kestrel web host); the HTTP listener binds the `Port`
# env var, so no ASPNETCORE_URLS / --urls is needed.
ENTRYPOINT ["dotnet", "SimpleL7Proxy.dll"]
