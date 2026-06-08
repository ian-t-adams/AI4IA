# Vendored from microsoft/SimpleL7Proxy (MIT) @ 72244ac3e779587ae81e25de3ece4653cfbf2ab1
# Build context is ./proxy (the azd "proxy" service project dir). This differs
# from the upstream Dockerfile (whose context is the repo's src/ dir) only in
# that it ALSO copies Shared-parser.csproj before `dotnet restore` — the proxy
# project references it, so the upstream file fails restore without it.
FROM mcr.microsoft.com/dotnet/sdk:10.0 AS build-env
WORKDIR /app

# Copy project files first so restore is cached independently of source changes.
COPY Shared/Shared.csproj ./Shared/
COPY Shared-parser/Shared-parser.csproj ./Shared-parser/
COPY SimpleL7Proxy/SimpleL7Proxy.csproj ./SimpleL7Proxy/
COPY SimpleL7Proxy/global.json ./SimpleL7Proxy/
WORKDIR /app/SimpleL7Proxy
RUN dotnet restore

# Copy the rest of the source and publish.
WORKDIR /app
COPY Shared/ ./Shared/
COPY Shared-parser/ ./Shared-parser/
COPY SimpleL7Proxy/ ./SimpleL7Proxy/
WORKDIR /app/SimpleL7Proxy
RUN dotnet publish -c Release -o /app/out

FROM mcr.microsoft.com/dotnet/aspnet:10.0-noble-chiseled
WORKDIR /app
COPY --from=build-env /app/out .
# Default per-user policy + Entra App-ID allowlist (overridable via App Config).
COPY SimpleL7Proxy/config.json .
COPY SimpleL7Proxy/auth.json .

# Main L7 listener (Port env, default 8000 -> we set 8080) and the probe server
# (Kestrel on 9000: /health, /readiness, /startup, /liveness).
EXPOSE 8080
EXPOSE 9000

# Workstation GC returns memory to the OS more aggressively under bursty load.
ENV DOTNET_GCConserveMemory=9
ENV DOTNET_gcServer=0

# The proxy is a Worker (no Kestrel web host); the HTTP listener binds the `Port`
# env var, so no ASPNETCORE_URLS / --urls is needed.
ENTRYPOINT ["dotnet", "SimpleL7Proxy.dll"]
