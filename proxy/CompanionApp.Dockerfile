# AI4IA-owned image for the vendored SimpleL7Proxy CompanionApp telemetry console
# at b0066b0e53f89abb5e84cfeacda2fdcaca8b081e; the file-level provenance, including
# every upstream page AI4IA does not vendor, is in proxy/upstream-provenance.json.
# Build context = ./proxy. Refresh these OCI manifest-list digests with:
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
COPY SimpleL7Proxy/packages.lock.json ./SimpleL7Proxy/
COPY CompanionApp/CompanionApp.csproj ./CompanionApp/
COPY CompanionApp/packages.lock.json ./CompanionApp/
WORKDIR /app/CompanionApp
RUN dotnet restore --locked-mode

# Copy the rest of the source and publish. The key-ring directory is created here
# because the chiseled runtime has no shell.
WORKDIR /app
COPY Shared/ ./Shared/
COPY Shared-parser/ ./Shared-parser/
COPY SimpleL7Proxy/ ./SimpleL7Proxy/
COPY CompanionApp/ ./CompanionApp/
WORKDIR /app/CompanionApp
RUN dotnet publish -c Release -o /app/out --no-restore && mkdir -p /app/state/keys

FROM mcr.microsoft.com/dotnet/aspnet:10.0-noble-chiseled@sha256:9651fa59abcdf177c30392cb44a820605ca5d618429ab37acbf6e7c644510b02
WORKDIR /app
COPY --from=build-env /app/out .
# The only writable path is the ephemeral Data Protection key ring of the single
# replica; application files stay root-owned.
COPY --from=build-env --chown=1654:1654 /app/state/keys /var/lib/companion/keys

# Container Apps terminates TLS, so honor its forwarded scheme and client address.
ENV ASPNETCORE_HTTP_PORTS=8080 \
    ASPNETCORE_FORWARDEDHEADERS_ENABLED=true \
    CompanionApp__DataProtectionKeysPath=/var/lib/companion/keys

USER 1654
EXPOSE 8080
ENTRYPOINT ["dotnet", "CompanionApp.dll"]
