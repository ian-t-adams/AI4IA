"""Contracts for proxy telemetry, image evidence, and scanner scope."""

from __future__ import annotations

import json
import re
import unittest
from datetime import date
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
GATEWAY = ROOT / "infra" / "modules" / "gateway.bicep"
CONTAINER_CONFIG = ROOT / "infra" / "proxy-container-config.json"
DOCKER_BUILD = ROOT / ".github" / "workflows" / "docker-build.yml"
CODEQL = ROOT / ".github" / "workflows" / "codeql.yml"
SECURITY_SCAN = ROOT / ".github" / "workflows" / "security-scan.yml"
GITLEAKS = ROOT / ".gitleaks.toml"
TRIVY_EXCEPTIONS = ROOT / "proxy" / ".trivyignore"
TRIVY_MISCONFIG_EXCEPTIONS = ROOT / ".trivyignore.yaml"
GITLEAKS_IGNORE = ROOT / ".gitleaksignore"
UPSTREAM_PROVENANCE = ROOT / "proxy" / "upstream-provenance.json"
APP_CONFIG_SERVICE = (
    ROOT / "proxy" / "SimpleL7Proxy" / "Config" / "AppConfigService.cs"
)


class ProxyTelemetryContracts(unittest.TestCase):
    def setUp(self) -> None:
        self.config = json.loads(CONTAINER_CONFIG.read_text(encoding="utf-8"))
        self.logging = self.config["logging"]

    def test_successful_probe_events_are_suppressed_at_every_proxy_sink(self) -> None:
        self.assertIn("-probe", self.logging["logToConsole"])
        self.assertIn("-probe", self.logging["logToAI"])
        self.assertNotIn("probe", self.logging["logToEvents"])

    def test_failure_signals_and_platform_metrics_remain_enabled(self) -> None:
        for signal in ("circuitbreaker", "exception"):
            self.assertIn(signal, self.logging["logToEvents"])
        gateway = GATEWAY.read_text(encoding="utf-8")
        self.assertEqual(gateway.count("type: 'Startup'"), 1)
        self.assertEqual(gateway.count("type: 'Liveness'"), 1)
        self.assertEqual(gateway.count("type: 'Readiness'"), 1)
        self.assertIn("{ category: 'AllMetrics', enabled: true }", gateway)

    def test_generated_container_env_consumes_the_reviewed_config(self) -> None:
        gateway = GATEWAY.read_text(encoding="utf-8")
        self.assertIn(
            "var proxyContainerConfig = "
            "loadJsonContent('../proxy-container-config.json')",
            gateway,
        )
        for env_name, property_name in (
            ("LogToConsole", "logToConsole"),
            ("LogToAI", "logToAI"),
            ("LogToEvents", "logToEvents"),
        ):
            self.assertRegex(
                gateway,
                rf"name: '{env_name}'[\s\S]{{0,100}}"
                rf"value: string\(proxyContainerConfig\.logging\.{property_name}\)",
            )


class ProxyRuntimeContracts(unittest.TestCase):
    def test_failed_app_config_download_is_checked_before_result_value(self) -> None:
        source = APP_CONFIG_SERVICE.read_text(encoding="utf-8")
        start = source.index("private async Task ProcessRefreshAsync")
        end = source.index("private async Task<string?> ReadSentinelAsync", start)
        refresh = source[start:end]
        self.assertEqual(
            refresh.count("if (result == null)"),
            1,
            "ProcessRefreshAsync must have one explicit failed-download guard",
        )
        null_guard = refresh.index("if (result == null)")
        dereference = refresh.index("var (warm, cold) = result.Value")
        self.assertLess(
            null_guard,
            dereference,
            "a transient App Configuration failure must return before result.Value",
        )


class ProxySupplyChainContracts(unittest.TestCase):
    def test_pr_build_scans_final_proxy_image_and_retains_evidence(self) -> None:
        workflow = yaml.safe_load(DOCKER_BUILD.read_text(encoding="utf-8"))
        api_steps = workflow["jobs"]["api"]["steps"]
        by_name = {step["name"]: step for step in api_steps}
        build = by_name["Build proxy image (proxy/Dockerfile)"]
        self.assertEqual(build["with"]["context"], "proxy")
        self.assertTrue(build["with"]["load"])
        self.assertEqual(build["with"]["tags"], "ai4ia-proxy:ci")

        scan = by_name["Scan final proxy image for HIGH/CRITICAL vulnerabilities"]
        self.assertEqual(scan["with"]["scan-type"], "image")
        self.assertEqual(
            scan["with"]["image-ref"],
            "${{ steps.proxy-image.outputs.image_id }}",
        )
        self.assertEqual(scan["with"]["exit-code"], "1")
        self.assertEqual(scan["with"]["severity"], "HIGH,CRITICAL")
        self.assertEqual(scan["with"]["trivyignores"], "proxy/.trivyignore")

        sbom = by_name["Generate proxy SPDX SBOM"]
        self.assertEqual(sbom["with"]["format"], "spdx-json")
        self.assertEqual(
            sbom["with"]["image-ref"],
            "${{ steps.proxy-image.outputs.image_id }}",
        )
        upload = by_name["Retain proxy SBOM and build metadata"]
        self.assertIn("proxy-sbom.spdx.json", upload["with"]["path"])
        self.assertIn("proxy-build-provenance.json", upload["with"]["path"])
        metadata = by_name["Record proxy build metadata"]
        self.assertEqual(
            metadata["env"]["IMAGE_ID"],
            "${{ steps.proxy-image.outputs.image_id }}",
        )
        self.assertNotIn("steps.proxy-build.outputs.digest", metadata["run"])

    def test_csharp_and_patched_proxy_files_are_not_blanket_excluded(self) -> None:
        codeql = yaml.safe_load(CODEQL.read_text(encoding="utf-8"))
        matrix = codeql["jobs"]["analyze"]["strategy"]["matrix"]["include"]
        self.assertIn({"language": "csharp", "build-mode": "manual"}, matrix)
        self.assertIn(
            "dotnet restore proxy/AI4IA.Proxy.Tests/AI4IA.Proxy.Tests.csproj "
            "--locked-mode",
            CODEQL.read_text(encoding="utf-8"),
        )
        self.assertNotIn("skip-dirs: proxy/SimpleL7Proxy", SECURITY_SCAN.read_text())
        self.assertNotRegex(
            GITLEAKS.read_text(encoding="utf-8"),
            r"paths\s*=\s*\[[^\]]*proxy/SimpleL7Proxy",
        )
        fingerprints = [
            line.strip()
            for line in GITLEAKS_IGNORE.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertEqual(
            fingerprints,
            [
                "f80974f2aaeab53a3b329a7b518aea43a5248cb6:"
                "proxy/SimpleL7Proxy/Config/ProxyConfig.cs:generic-api-key:80"
            ],
        )

    def test_vulnerability_exceptions_are_exact_cves(self) -> None:
        lines = TRIVY_EXCEPTIONS.read_text(encoding="utf-8").splitlines()
        exceptions = []
        for index, line in enumerate(lines):
            exception = line.strip()
            if not exception or exception.startswith("#"):
                continue
            exceptions.append(exception)
            self.assertRegex(exception, r"^CVE-\d{4}-\d{4,}$")
            metadata: dict[str, str] = {}
            cursor = index - 1
            while cursor >= 0 and lines[cursor].lstrip().startswith("#"):
                comment = lines[cursor].lstrip()[1:].strip()
                if ":" in comment:
                    key, value = comment.split(":", 1)
                    metadata[key.strip()] = value.strip()
                cursor -= 1
            self.assertRegex(metadata.get("owner", ""), r"^@[A-Za-z0-9-]+$")
            self.assertRegex(metadata.get("tracking", ""), r"^https://")
            self.assertGreater(len(metadata.get("rationale", "")), 20)
            expiry = date.fromisoformat(metadata.get("expires", "0001-01-01"))
            self.assertGreaterEqual(expiry, date.today())
        self.assertNotIn("*", exceptions)

    def test_vendored_misconfig_exceptions_cover_only_unpatched_blobs(self) -> None:
        policy = yaml.safe_load(
            TRIVY_MISCONFIG_EXCEPTIONS.read_text(encoding="utf-8")
        )
        provenance = json.loads(UPSTREAM_PROVENANCE.read_text(encoding="utf-8"))
        vendored_paths = {
            path
            for exception in policy["misconfigurations"]
            for path in exception.get("paths", [])
            if path.startswith("proxy/SimpleL7Proxy/")
        }
        self.assertEqual(
            vendored_paths,
            {
                "proxy/SimpleL7Proxy/Dockerfile",
                "proxy/SimpleL7Proxy/sample-deployment.yaml",
            },
        )
        for path in vendored_paths:
            relative = path.removeprefix("proxy/")
            self.assertEqual(
                provenance["files"][relative]["disposition"],
                "upstream-equivalent",
            )

    def test_proxy_docker_restore_is_locked_and_publish_does_not_reresolve(self) -> None:
        dockerfile = (ROOT / "proxy" / "Dockerfile").read_text(encoding="utf-8")
        for lockfile in (
            "Shared/packages.lock.json",
            "Shared-parser/packages.lock.json",
            "SimpleL7Proxy/packages.lock.json",
        ):
            self.assertIn(f"COPY {lockfile}", dockerfile)
        self.assertIn("RUN dotnet restore --locked-mode", dockerfile)
        self.assertIn("RUN dotnet publish -c Release -o /app/out --no-restore", dockerfile)


if __name__ == "__main__":
    unittest.main()
