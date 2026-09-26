"""Execute App Configuration sentinel reconciliation with every Azure edge stubbed.

The postprovision hook PUTs ``Warm:Sentinel`` through the App Configuration
data-plane REST API with a Microsoft Entra token minted on every attempt: azd
first, the Azure CLI only as a fallback. ``azure/login`` hands the Azure CLI one
GitHub OIDC assertion, so after a slow ``azd provision`` every CLI token request
for App Configuration failed with AADSTS700024, while azd, which fetches a fresh
assertion for each token, kept working.

The tests load the real functions through PowerShell's parser and replace Azure,
the CLIs and the clock with process-local stubs. The only socket they open is a
loopback HTTP fixture that records what the real request helper sends.
"""
from __future__ import annotations

import http.server
import json
import os
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from scripts.tests._postprovision import _ps_literal, _run_pwsh, _run_pwsh_with_output

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "postprovision.ps1"
ENDPOINT = "https://appcs-example.azconfig.io"
API_VERSION = "api-version=2023-11-01"
SENTINEL_URL = f"{ENDPOINT}/kv/Warm%3ASentinel?{API_VERSION}"
BODY = '{"value":"ready"}'
KV_MEDIA_TYPE = "application/vnd.microsoft.appconfig.kv+json"
AZD_ARGS = ["auth", "token", "--scope", "https://appconfig.azure.com/.default"]
AZ_ARGS = [
    "account",
    "get-access-token",
    "--resource",
    "https://appconfig.azure.com",
    "--query",
    "accessToken",
    "--output",
    "tsv",
]
# Every bearer value a stub mints starts with this marker, so a test can prove
# that none of them reached any output stream. The values are made at runtime.
ISSUED_PREFIX = "fake-appconfig-bearer-"
BODY_MARKER = "response-body-" + secrets.token_hex(8)


def _fail_detail(attempts: int) -> str:
    return (
        "Entra-authenticated set failed within the 900-second budget "
        f"after {attempts} attempt(s)"
    )


def _load(*names: str) -> str:
    """PowerShell that defines the named script-level functions verbatim."""
    wanted = ", ".join(_ps_literal(name) for name in names)
    return rf"""
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
  {_ps_literal(str(SCRIPT))}, [ref]$tokens, [ref]$errors
)
if ($errors.Count -gt 0) {{ throw ($errors | Out-String) }}
$wanted = @({wanted})
$found = @($ast.EndBlock.Statements | Where-Object {{
  $_ -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $wanted -contains $_.Name
}})
if ($found.Count -ne $wanted.Count) {{ throw "missing function among: $($wanted -join ', ')" }}
foreach ($definition in $found) {{ Invoke-Expression $definition.Extent.Text }}
"""


def _scenario(
    *,
    endpoint: str | None = ENDPOINT,
    label: str | None = None,
    fallback_label: str | None = None,
    principal_id: str | None = "deploy-principal",
    statuses: tuple[int, ...] = (200,),
    token_failures: int = 0,
    token_available: bool = True,
    token_seconds: int = 0,
    request_seconds: int = 0,
    leak: bool = False,
) -> dict[str, object]:
    """One sentinel run. A status of -1 makes the request stub throw; -2 makes it
    emit a stray value before returning 200."""
    return {
        "env": {
            "AZURE_APP_CONFIG_ENDPOINT": endpoint,
            "AZURE_APP_CONFIG_LABEL": label,
            "AI4IA_PROXY_APPCONFIG_LABEL": fallback_label,
            "AZURE_PRINCIPAL_ID": principal_id,
        },
        "statuses": list(statuses),
        "tokenFailures": token_failures,
        "tokenAvailable": token_available,
        "tokenSeconds": token_seconds,
        "requestSeconds": request_seconds,
        "leak": leak,
    }


def _run_sentinel(scenarios: list[dict[str, object]]) -> tuple[list[dict[str, object]], str]:
    """Run each scenario through the real sentinel, Add-Result and output streams."""
    command = _load("Register-AppConfigurationSentinel", "Add-Result") + rf"""
$scenarios = @(ConvertFrom-Json -InputObject {_ps_literal(json.dumps(scenarios))})
$script:IssuedPrefix = {_ps_literal(ISSUED_PREFIX)}
$script:BodyMarker = {_ps_literal(BODY_MARKER)}
function Get-EnvValue {{
  param([Parameter(Mandatory)][string]$Name)
  $property = $script:Scenario.env.PSObject.Properties[$Name]
  if ($null -eq $property) {{ return $null }}
  return $property.Value
}}
function Start-Sleep {{
  param($Seconds)
  $script:SleepSeconds.Add([int]$Seconds)
  $script:NowSeconds += [int]$Seconds
}}
function Get-MonotonicTime {{ return [double]$script:NowSeconds }}
function Get-AppConfigurationToken {{
  param([Parameter(Mandatory)][int]$TimeoutSec)
  $script:TokenTimeouts.Add($TimeoutSec)
  $call = $script:TokenTimeouts.Count
  $script:NowSeconds += [Math]::Min([int]$script:Scenario.tokenSeconds, $TimeoutSec)
  if (-not $script:Scenario.tokenAvailable -or $call -le [int]$script:Scenario.tokenFailures) {{
    return $null
  }}
  return '{{0}}{{1}}-{{2}}' -f $script:IssuedPrefix, $call, [guid]::NewGuid().ToString('N')
}}
function Invoke-AppConfigKeyValuePut {{
  param(
    [Parameter(Mandatory)][uri]$Uri,
    [Parameter(Mandatory)][string]$Token,
    [Parameter(Mandatory)][string]$Body,
    [Parameter(Mandatory)][int]$TimeoutSec
  )
  $statuses = @($script:Scenario.statuses)
  $status = [int]$statuses[[Math]::Min($script:Puts.Count, $statuses.Count - 1)]
  $issuedBy = 0
  if ($Token.StartsWith($script:IssuedPrefix)) {{
    $issuedBy = [int]$Token.Substring($script:IssuedPrefix.Length).Split('-')[0]
  }}
  $script:Puts.Add([pscustomobject]@{{
    uri = $Uri.OriginalString
    absoluteUri = $Uri.AbsoluteUri
    body = $Body
    timeoutSec = $TimeoutSec
    startedAt = $script:NowSeconds
    tokenIssuedBy = $issuedBy
    tokenCallsSoFar = $script:TokenTimeouts.Count
  }})
  if ($script:Scenario.leak) {{ Write-Host "diagnostic: $Token $script:BodyMarker" }}
  $script:NowSeconds += [Math]::Min([int]$script:Scenario.requestSeconds, $TimeoutSec)
  if ($status -eq -1) {{
    throw ('simulated transport failure for {{0}}: {{1}}' -f $Token, $script:BodyMarker)
  }}
  if ($status -eq -2) {{
    Write-Output 0
    return 200
  }}
  return $status
}}
$runs = [System.Collections.Generic.List[object]]::new()
foreach ($scenario in $scenarios) {{
  $script:Scenario = $scenario
  $script:Results = [System.Collections.Generic.List[object]]::new()
  $script:TokenTimeouts = [System.Collections.Generic.List[int]]::new()
  $script:Puts = [System.Collections.Generic.List[object]]::new()
  $script:SleepSeconds = [System.Collections.Generic.List[int]]::new()
  $script:NowSeconds = 0
  # Verbose and pipeline output are printed, never captured, so the leak
  # assertions see everything an operator would see in the workflow log.
  $VerbosePreference = 'Continue'
  Register-AppConfigurationSentinel | Out-Host
  $VerbosePreference = 'SilentlyContinue'
  $runs.Add([pscustomobject]@{{
    results = @($script:Results)
    tokenTimeouts = @($script:TokenTimeouts)
    puts = @($script:Puts)
    sleepSeconds = @($script:SleepSeconds)
    elapsedSeconds = $script:NowSeconds
  }})
}}
[pscustomobject]@{{ runs = @($runs) }} | ConvertTo-Json -Depth 8 -Compress
"""
    payload, output = _run_pwsh_with_output(command, cwd=REPO)
    return payload["runs"], output


class AppConfigurationSentinelTests(unittest.TestCase):
    output = ""

    def _run(self, *scenarios: dict[str, object]) -> list[dict[str, object]]:
        runs, self.output = _run_sentinel(list(scenarios))
        # No minted bearer value or response text may reach any stream.
        self.assertNotIn(ISSUED_PREFIX, self.output)
        self.assertNotIn(BODY_MARKER, self.output)
        return runs

    def _status(self, run: dict[str, object]) -> tuple[str, str]:
        (result,) = run["results"]
        return result["Status"], result["Detail"]

    def _assert_untouched(self, run: dict[str, object]) -> None:
        self.assertEqual(run["tokenTimeouts"], [])
        self.assertEqual(run["puts"], [])
        self.assertEqual(run["sleepSeconds"], [])

    def test_unlabeled_set_is_one_documented_put_with_a_fresh_token(self) -> None:
        (run,) = self._run(_scenario())
        self.assertEqual(self._status(run), ("PASS", "Warm:Sentinel=ready (unlabeled)"))
        self.assertEqual(run["tokenTimeouts"], [60])
        (put,) = run["puts"]
        self.assertEqual(put["uri"], SENTINEL_URL)
        self.assertEqual(put["absoluteUri"], SENTINEL_URL)
        self.assertEqual(put["body"], BODY)
        self.assertEqual(put["timeoutSec"], 60)
        self.assertEqual(put["tokenIssuedBy"], 1)
        self.assertEqual(run["sleepSeconds"], [])

    def test_label_is_one_exact_percent_encoded_query_value(self) -> None:
        cases = {
            "Production Blue": "Production%20Blue",
            "blue&green=1/2+3": "blue%26green%3D1%2F2%2B3",
        }
        runs = self._run(*(_scenario(label=label) for label in cases))
        for (label, encoded), run in zip(cases.items(), runs):
            with self.subTest(label=label):
                expected = f"{ENDPOINT}/kv/Warm%3ASentinel?label={encoded}&{API_VERSION}"
                self.assertEqual(
                    self._status(run), ("PASS", "Warm:Sentinel=ready (configured label)")
                )
                self.assertEqual(run["puts"][0]["uri"], expected)
                self.assertEqual(run["puts"][0]["absoluteUri"], expected)

    def test_legacy_label_output_is_only_a_fallback(self) -> None:
        cases = (
            ({"label": None, "fallback_label": "Legacy"}, "label=Legacy&"),
            ({"label": "", "fallback_label": "Legacy"}, "label=Legacy&"),
            ({"label": "Explicit", "fallback_label": "Legacy"}, "label=Explicit&"),
            ({"label": "   ", "fallback_label": None}, ""),
            ({"label": None, "fallback_label": "  "}, ""),
        )
        runs = self._run(*(_scenario(**kwargs) for kwargs, _ in cases))
        for (kwargs, query), run in zip(cases, runs):
            with self.subTest(**kwargs):
                scope = "configured label" if query else "unlabeled"
                self.assertEqual(self._status(run), ("PASS", f"Warm:Sentinel=ready ({scope})"))
                self.assertEqual(
                    run["puts"][0]["uri"],
                    f"{ENDPOINT}/kv/Warm%3ASentinel?{query}{API_VERSION}",
                )

    def test_missing_endpoint_fails_closed_before_any_credential_use(self) -> None:
        for run in self._run(_scenario(endpoint=None), _scenario(endpoint="  ")):
            self.assertEqual(
                self._status(run),
                ("FAIL", "required output AZURE_APP_CONFIG_ENDPOINT not set"),
            )
            self._assert_untouched(run)

    def test_bearer_token_only_goes_to_an_https_store_origin(self) -> None:
        with_credentials = (
            "https://operator:" + secrets.token_hex(4) + "@appcs-example.azconfig.io"
        )
        rejected = (
            "http://appcs-example.azconfig.io",
            "https://appcs-example.azconfig.io/kv",
            "https://appcs-example.azconfig.io/?label=x",
            "https://appcs-example.azconfig.io/#fragment",
            with_credentials,
            "appcs-example.azconfig.io",
            "/kv/Warm:Sentinel",
        )
        runs = self._run(
            *(_scenario(endpoint=endpoint) for endpoint in rejected),
            # Validation precedes the local-provision skip, like the missing output.
            _scenario(endpoint=rejected[0], principal_id=None),
            # Control: a normalizable https origin is accepted in the same process.
            _scenario(endpoint="https://APPCS-Example.azconfig.io:443/"),
        )
        for endpoint, run in zip(rejected + (rejected[0],), runs[:-1]):
            with self.subTest(endpoint=endpoint):
                self.assertEqual(
                    self._status(run),
                    (
                        "FAIL",
                        "AZURE_APP_CONFIG_ENDPOINT must be an https origin without a "
                        "path, query, fragment or credentials",
                    ),
                )
                self._assert_untouched(run)
        control = runs[-1]
        self.assertEqual(self._status(control)[0], "PASS")
        self.assertEqual(control["puts"][0]["uri"], SENTINEL_URL)

    def test_local_provision_without_oidc_principal_leaves_existing_sentinel(self) -> None:
        for run in self._run(_scenario(principal_id=None), _scenario(principal_id=" ")):
            self.assertEqual(
                self._status(run),
                ("SKIP", "AZURE_PRINCIPAL_ID not set; workflow-owned sentinel left unchanged"),
            )
            self._assert_untouched(run)

    def test_role_propagation_retries_with_a_fresh_token_each_attempt(self) -> None:
        (run,) = self._run(_scenario(statuses=(403, 401, 200)))
        self.assertEqual(self._status(run), ("PASS", "Warm:Sentinel=ready (unlabeled)"))
        self.assertEqual(run["tokenTimeouts"], [60, 60, 60])
        self.assertEqual([put["tokenIssuedBy"] for put in run["puts"]], [1, 2, 3])
        self.assertEqual([put["tokenCallsSoFar"] for put in run["puts"]], [1, 2, 3])
        self.assertEqual(run["sleepSeconds"], [30, 30])

    def test_every_2xx_status_passes(self) -> None:
        statuses = (200, 201, 204)
        runs = self._run(*(_scenario(statuses=(status,)) for status in statuses))
        for status, run in zip(statuses, runs):
            with self.subTest(status=status):
                self.assertEqual(self._status(run), ("PASS", "Warm:Sentinel=ready (unlabeled)"))
                self.assertEqual(len(run["puts"]), 1)

    def test_non_2xx_or_missing_response_fails_after_the_rbac_budget(self) -> None:
        # 0 is the request helper's "no response" (timeout, DNS, TLS) result.
        statuses = (0, 199, 300, 307, 401, 403, 404, 409, 429, 500, 503)
        runs = self._run(*(_scenario(statuses=(status,)) for status in statuses))
        for status, run in zip(statuses, runs):
            with self.subTest(status=status):
                self.assertEqual(self._status(run), ("FAIL", _fail_detail(30)))
                # The last attempt starts at 870 s, so both bounds shrink to fit.
                self.assertEqual(run["tokenTimeouts"], [60] * 29 + [30])
                self.assertEqual(
                    [put["timeoutSec"] for put in run["puts"]], [60] * 29 + [30]
                )
                self.assertEqual(
                    [put["tokenIssuedBy"] for put in run["puts"]], list(range(1, 31))
                )
                self.assertEqual(run["sleepSeconds"], [30] * 30)
                self.assertEqual(run["elapsedSeconds"], 900)

    def test_request_exceptions_are_retried_without_echoing_their_text(self) -> None:
        (run,) = self._run(_scenario(statuses=(-1, 200)))
        self.assertEqual(self._status(run), ("PASS", "Warm:Sentinel=ready (unlabeled)"))
        self.assertEqual([put["tokenIssuedBy"] for put in run["puts"]], [1, 2])
        self.assertEqual(run["sleepSeconds"], [30])
        # The catch path ran and its verbose line reached the captured output, so
        # the absence of the exception text is not an artefact of a silent stream.
        self.assertIn("retrying without emitting request details", self.output)

    def test_a_missing_token_skips_the_request_and_retries(self) -> None:
        (run,) = self._run(_scenario(token_failures=2))
        self.assertEqual(self._status(run), ("PASS", "Warm:Sentinel=ready (unlabeled)"))
        self.assertEqual(run["tokenTimeouts"], [60, 60, 60])
        self.assertEqual([put["tokenIssuedBy"] for put in run["puts"]], [3])
        self.assertEqual(run["sleepSeconds"], [30, 30])

    def test_no_token_for_the_whole_budget_fails_without_any_request(self) -> None:
        (run,) = self._run(_scenario(token_available=False))
        self.assertEqual(self._status(run), ("FAIL", _fail_detail(30)))
        self.assertEqual(run["tokenTimeouts"], [60] * 29 + [30])
        self.assertEqual(run["puts"], [])
        self.assertEqual(run["sleepSeconds"], [30] * 30)
        self.assertEqual(run["elapsedSeconds"], 900)

    def test_slow_token_and_request_calls_never_exceed_the_budget(self) -> None:
        cases = {
            "slow request": (_scenario(statuses=(503,), request_seconds=60), 10, 10),
            "slow token": (_scenario(statuses=(503,), token_seconds=60), 10, 10),
            "both slow": (
                _scenario(statuses=(503,), token_seconds=60, request_seconds=60),
                6,
                6,
            ),
            # The eighth attempt starts at 840 s and its token call uses the
            # remaining 60 s, so no request may start once the budget is spent.
            "token exhausts the budget": (
                _scenario(statuses=(503,), token_seconds=60, request_seconds=30),
                8,
                7,
            ),
        }
        runs = self._run(*(scenario for scenario, _, _ in cases.values()))
        for (name, (_, attempts, requests)), run in zip(cases.items(), runs):
            with self.subTest(case=name):
                self.assertEqual(self._status(run), ("FAIL", _fail_detail(attempts)))
                self.assertEqual(run["elapsedSeconds"], 900)
                self.assertEqual(len(run["tokenTimeouts"]), attempts)
                self.assertEqual(len(run["puts"]), requests)
                for put in run["puts"]:
                    self.assertLessEqual(put["startedAt"] + put["timeoutSec"], 900)

    def test_a_helper_that_emits_stray_output_is_not_a_pass(self) -> None:
        # -2 makes the request stub emit 0 before returning 200. A stray pipeline
        # value must fail the attempt, not turn a comparison into a filter.
        (run,) = self._run(_scenario(statuses=(-2, 200)))
        self.assertEqual(self._status(run), ("PASS", "Warm:Sentinel=ready (unlabeled)"))
        self.assertEqual([put["tokenIssuedBy"] for put in run["puts"]], [1, 2])
        self.assertEqual(run["sleepSeconds"], [30])

    def test_leak_detector_sees_values_printed_through_the_same_streams(self) -> None:
        # Control for every absence assertion above: the identical harness makes a
        # printed bearer value and response marker visible to the same check.
        runs, output = _run_sentinel([_scenario(leak=True)])
        self.assertEqual(runs[0]["results"][0]["Status"], "PASS")
        self.assertIn(ISSUED_PREFIX, output)
        self.assertIn(BODY_MARKER, output)


def _run_token(
    *, azd: tuple[int, str, int], az: tuple[int, str, int], budget: int = 60
) -> dict[str, object]:
    """Run the real token helper; each CLI fixture is (exit code, stdout, seconds)."""
    fixtures = {
        name: {"exit": exit_code, "output": output, "seconds": seconds}
        for name, (exit_code, output, seconds) in (("azd", azd), ("az", az))
    }
    command = _load("Get-AppConfigurationToken") + rf"""
$fixtures = ConvertFrom-Json -InputObject {_ps_literal(json.dumps(fixtures))}
$script:NowSeconds = 0
$script:Calls = [System.Collections.Generic.List[object]]::new()
function Get-MonotonicTime {{ return [double]$script:NowSeconds }}
function Invoke-NativeWithTimeout {{
  param(
    [Parameter(Mandatory)][string]$Command,
    [Parameter(Mandatory)][string[]]$Arguments,
    [Parameter(Mandatory)][int]$TimeoutSec
  )
  $script:Calls.Add([pscustomobject]@{{
    command = $Command
    arguments = [string[]]@($Arguments)
    timeoutSec = $TimeoutSec
  }})
  $fixture = $fixtures.$Command
  if ([int]$fixture.seconds -ge $TimeoutSec) {{
    $script:NowSeconds += $TimeoutSec
    return [pscustomobject]@{{ ExitCode = 124; Output = ''; TimedOut = $true }}
  }}
  $script:NowSeconds += [int]$fixture.seconds
  return [pscustomobject]@{{
    ExitCode = [int]$fixture.exit
    Output = [string]$fixture.output
    TimedOut = $false
  }}
}}
$value = Get-AppConfigurationToken -TimeoutSec {budget}
[pscustomobject]@{{ token = $value; calls = @($script:Calls) }} |
  ConvertTo-Json -Depth 5 -Compress
"""
    return _run_pwsh(command, cwd=REPO)


class AppConfigurationTokenTests(unittest.TestCase):
    def setUp(self) -> None:
        self.azd_value = "azd-" + secrets.token_hex(8)
        self.az_value = "az-" + secrets.token_hex(8)

    def _call(self, command: str, arguments: list[str], timeout: int) -> dict[str, object]:
        return {"command": command, "arguments": arguments, "timeoutSec": timeout}

    def test_azd_mints_the_token_for_the_documented_audience_first(self) -> None:
        payload = _run_token(azd=(0, f"  {self.azd_value}\n", 0), az=(0, self.az_value, 0))
        self.assertEqual(payload["token"], self.azd_value)
        self.assertEqual(payload["calls"], [self._call("azd", AZD_ARGS, 60)])

    def test_azure_cli_is_the_fallback_only_when_azd_yields_no_token(self) -> None:
        azd_failures = {
            "nonzero exit": (1, self.azd_value, 0),
            "empty output": (0, "", 0),
            "blank output": (0, "  \n", 0),
        }
        for name, azd in azd_failures.items():
            with self.subTest(azd=name):
                payload = _run_token(azd=azd, az=(0, self.az_value, 0))
                self.assertEqual(payload["token"], self.az_value)
                self.assertEqual(
                    payload["calls"],
                    [self._call("azd", AZD_ARGS, 60), self._call("az", AZ_ARGS, 60)],
                )

    def test_fallback_receives_only_the_remaining_shared_budget(self) -> None:
        payload = _run_token(azd=(1, "", 25), az=(0, self.az_value, 0))
        self.assertEqual(payload["token"], self.az_value)
        self.assertEqual(
            payload["calls"],
            [self._call("azd", AZD_ARGS, 60), self._call("az", AZ_ARGS, 35)],
        )
        payload = _run_token(azd=(1, "", 59), az=(0, self.az_value, 0))
        self.assertEqual(payload["calls"][1], self._call("az", AZ_ARGS, 1))

    def test_azd_timing_out_on_the_whole_budget_skips_the_fallback(self) -> None:
        payload = _run_token(azd=(0, self.azd_value, 60), az=(0, self.az_value, 0))
        self.assertIsNone(payload["token"])
        self.assertEqual(payload["calls"], [self._call("azd", AZD_ARGS, 60)])

    def test_no_token_when_both_credentials_fail(self) -> None:
        payload = _run_token(azd=(1, "", 0), az=(1, self.az_value, 0))
        self.assertIsNone(payload["token"])
        self.assertEqual(
            payload["calls"],
            [self._call("azd", AZD_ARGS, 60), self._call("az", AZ_ARGS, 60)],
        )


class _RecordingHandler(http.server.BaseHTTPRequestHandler):
    """Record every request.

    `/s/<status>` answers that status, `/d/<n>` waits n seconds before answering,
    and `/b/<n>` sends its headers at once but stalls the body for n seconds.
    """

    def do_PUT(self) -> None:
        self._answer()

    def do_GET(self) -> None:
        self._answer()

    def _answer(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8") if length else ""
        self.server.requests.append(  # type: ignore[attr-defined]
            {
                "method": self.command,
                "path": self.path,
                "headers": {name.lower(): value for name, value in self.headers.items()},
                "body": body,
            }
        )
        parts = self.path.split("/")
        route = parts[1] if len(parts) > 2 else ""
        status = int(parts[2]) if route == "s" else 200
        if route == "d":
            time.sleep(float(parts[2]))
        payload = b"" if status in (204, 304) else BODY_MARKER.encode("ascii")
        try:
            self.send_response(status)
            if 300 <= status < 400:
                self.send_header("Location", "/redirected")
            self.send_header("Content-Type", "application/problem+json")
            if route == "b":
                self.send_header("Content-Length", "1000")
                self.end_headers()
                time.sleep(float(parts[2]))
                return
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except OSError:
            pass  # The client gave up first, as the timeout tests intend.

    def log_message(self, format: str, *args: object) -> None:
        return


class _WireServer:
    def __init__(self) -> None:
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RecordingHandler)
        self.httpd.requests = []  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> "_WireServer":
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=10)

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    @property
    def requests(self) -> list[dict[str, object]]:
        return list(self.httpd.requests)  # type: ignore[attr-defined]


def _read_body_control(url: str) -> str:
    """A client that does read the body, so its content reaches the output."""
    return rf"""
$reader = [System.Net.Http.HttpClient]::new()
try {{
  Write-Host $reader.GetStringAsync({_ps_literal(url)}).GetAwaiter().GetResult()
}} finally {{ $reader.Dispose() }}
"""


def _default_put_control(url: str) -> str:
    """A default client: it follows redirects and buffers the whole body."""
    return rf"""
$plainClient = [System.Net.Http.HttpClient]::new()
$plainClient.Timeout = [TimeSpan]::FromSeconds(10)
$watch = [System.Diagnostics.Stopwatch]::StartNew()
$completed = $false
try {{
  $content = [System.Net.Http.StringContent]::new({_ps_literal(BODY)})
  $null = $plainClient.PutAsync({_ps_literal(url)}, $content).GetAwaiter().GetResult()
  $completed = $true
}} catch {{
  $completed = $false
}} finally {{ $plainClient.Dispose() }}
$control = [pscustomobject]@{{ completed = $completed; elapsedSeconds = $watch.Elapsed.TotalSeconds }}
"""


def _run_wire(
    calls: list[dict[str, object]], *, controls: str = ""
) -> tuple[list[dict[str, object]], dict[str, object] | None, str]:
    """Send each call through the real request helper, then run optional controls."""
    command = _load("Invoke-AppConfigKeyValuePut") + rf"""
$calls = @(ConvertFrom-Json -InputObject {_ps_literal(json.dumps(calls))})
$runs = [System.Collections.Generic.List[object]]::new()
foreach ($call in $calls) {{
  $watch = [System.Diagnostics.Stopwatch]::StartNew()
  $status = Invoke-AppConfigKeyValuePut -Uri $call.uri -Token $call.token -Body $call.body `
    -TimeoutSec ([int]$call.timeoutSec)
  $runs.Add([pscustomobject]@{{ status = $status; elapsedSeconds = $watch.Elapsed.TotalSeconds }})
}}
$control = $null
{controls}
[pscustomobject]@{{ runs = @($runs); control = $control }} | ConvertTo-Json -Depth 5 -Compress
"""
    payload, output = _run_pwsh_with_output(command, cwd=REPO)
    return payload["runs"], payload["control"], output


def _wire_call(uri: str, *, timeout: int = 10, token: str | None = None) -> dict[str, object]:
    return {
        "uri": uri,
        "token": token or "wire-" + secrets.token_hex(8),
        "body": BODY,
        "timeoutSec": timeout,
    }


class AppConfigurationRequestWireTests(unittest.TestCase):
    """Drive the real HttpClient request against a loopback recorder."""

    def test_request_is_the_documented_set_key_value_call(self) -> None:
        bearer = "wire-" + secrets.token_hex(16)
        paths = (
            f"/kv/Warm%3ASentinel?label=Production%20Blue&{API_VERSION}",
            f"/kv/Warm%3ASentinel?{API_VERSION}",
        )
        with _WireServer() as server:
            runs, _, output = _run_wire(
                [_wire_call(server.origin + path, token=bearer) for path in paths]
            )
            requests = server.requests
        self.assertEqual([run["status"] for run in runs], [200, 200])
        self.assertEqual([request["path"] for request in requests], list(paths))
        for request in requests:
            headers = request["headers"]
            self.assertEqual(request["method"], "PUT")
            self.assertEqual(headers["authorization"], f"Bearer {bearer}")
            self.assertEqual(headers["content-type"], KV_MEDIA_TYPE)
            self.assertEqual(
                [part.strip() for part in headers["accept"].split(",")],
                [KV_MEDIA_TYPE, "application/problem+json"],
            )
            self.assertEqual(headers["content-length"], str(len(BODY)))
            self.assertEqual(request["body"], BODY)
        self.assertNotIn(bearer, output)

    def test_status_is_returned_verbatim_and_redirects_are_not_followed(self) -> None:
        statuses = [201, 204, 307, 401, 403, 409, 429, 500, 503]
        with _WireServer() as server:
            runs, control, _ = _run_wire(
                [_wire_call(f"{server.origin}/s/{status}") for status in statuses],
                controls=_default_put_control(f"{server.origin}/s/307"),
            )
            paths = [request["path"] for request in server.requests]
        self.assertEqual([run["status"] for run in runs], statuses)
        self.assertEqual(paths[: len(statuses)], [f"/s/{status}" for status in statuses])
        # Control: a default client in the same process follows the same Location.
        self.assertTrue(control["completed"])
        self.assertEqual(paths[len(statuses) :], ["/s/307", "/redirected"])

    def test_timeout_returns_no_status_within_the_bound(self) -> None:
        with _WireServer() as server:
            runs, _, _ = _run_wire(
                [
                    _wire_call(f"{server.origin}/d/3", timeout=1),
                    # Control: the identical slow answer arrives when the bound allows.
                    _wire_call(f"{server.origin}/d/3", timeout=10),
                ]
            )
        self.assertEqual(runs[0]["status"], 0)
        self.assertLess(runs[0]["elapsedSeconds"], 2.5)
        self.assertEqual(runs[1]["status"], 200)
        self.assertGreaterEqual(runs[1]["elapsedSeconds"], 2.5)

    def test_status_arrives_without_waiting_for_the_response_body(self) -> None:
        with _WireServer() as server:
            runs, control, _ = _run_wire(
                [_wire_call(f"{server.origin}/b/3")],
                # Control: a client that buffers the body waits on the same stall.
                controls=_default_put_control(f"{server.origin}/b/3"),
            )
        self.assertEqual(runs[0]["status"], 200)
        self.assertLess(runs[0]["elapsedSeconds"], 2.5)
        self.assertFalse(control["completed"])
        self.assertGreaterEqual(control["elapsedSeconds"], 2.5)

    def test_unreachable_store_returns_no_status(self) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        runs, _, _ = _run_wire(
            [_wire_call(f"http://127.0.0.1:{port}/kv/Warm%3ASentinel?{API_VERSION}")]
        )
        self.assertEqual(runs[0]["status"], 0)

    def test_response_body_never_reaches_the_output(self) -> None:
        with _WireServer() as server:
            calls = [_wire_call(f"{server.origin}/s/{status}") for status in (200, 403, 500)]
            runs, _, silent = _run_wire(calls)
            # Control: the same fixture's body is visible once something reads it.
            _, _, loud = _run_wire(
                calls[:1], controls=_read_body_control(f"{server.origin}/s/200")
            )
        self.assertEqual([run["status"] for run in runs], [200, 403, 500])
        self.assertNotIn(BODY_MARKER, silent)
        self.assertIn(BODY_MARKER, loud)


def _run_timeout_helper(*, mode: str, exit_code: int = 0) -> tuple[int, float]:
    if shutil.which("pwsh") is None:
        raise unittest.SkipTest("pwsh is required for executable postprovision tests")
    with tempfile.TemporaryDirectory() as tmp:
        stub_dir = Path(tmp)
        cli = "az"
        if mode == "missing":
            # A name nothing on PATH can provide, rather than hoping az is absent.
            cli = "ai4ia-missing-cli-" + secrets.token_hex(4)
        elif os.name == "nt":
            stub = stub_dir / "az.cmd"
            stub.write_text(
                "@echo off\r\n"
                'if "%AZ_STUB_MODE%"=="hang" pwsh -NoProfile -Command '
                '"Start-Sleep -Seconds 5"\r\n'
                "exit /b %AZ_STUB_EXIT%\r\n",
                encoding="ascii",
            )
        else:
            stub = stub_dir / "az"
            stub.write_text(
                '#!/usr/bin/env sh\n'
                'if [ "$AZ_STUB_MODE" = "hang" ]; then sleep 5; fi\n'
                'exit "$AZ_STUB_EXIT"\n',
                encoding="ascii",
            )
            stub.chmod(0o755)

        command = _load("Invoke-NativeWithTimeout") + rf"""
$result = Invoke-NativeWithTimeout -Command {_ps_literal(cli)} `
  -Arguments @('account', 'get-access-token') -TimeoutSec 1
Write-Output $result.ExitCode
"""
        env = dict(os.environ)
        env["AZ_STUB_MODE"] = mode
        env["AZ_STUB_EXIT"] = str(exit_code)
        env["PATH"] = str(stub_dir) + os.pathsep + env.get("PATH", "")
        started = time.monotonic()
        proc = subprocess.run(
            ["pwsh", "-NoProfile", "-NonInteractive", "-Command", command],
            cwd=REPO,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
        elapsed = time.monotonic() - started
        if proc.returncode != 0:
            raise AssertionError(
                f"PowerShell failed ({proc.returncode}):\n{proc.stderr}\n{proc.stdout}"
            )
        return int(proc.stdout.strip().splitlines()[-1]), elapsed


class NativeCommandTimeoutTests(unittest.TestCase):
    """The bound every azd/az token request runs under."""

    def test_native_success_is_propagated(self) -> None:
        result, _ = _run_timeout_helper(mode="exit", exit_code=0)
        self.assertEqual(result, 0)

    def test_native_nonzero_exit_is_propagated(self) -> None:
        result, _ = _run_timeout_helper(mode="exit", exit_code=23)
        self.assertEqual(result, 23)

    def test_missing_cli_is_not_success(self) -> None:
        result, _ = _run_timeout_helper(mode="missing")
        self.assertNotEqual(result, 0)

    def test_hung_cli_is_terminated_at_timeout(self) -> None:
        result, elapsed = _run_timeout_helper(mode="hang")
        self.assertEqual(result, 124)
        self.assertLess(elapsed, 4)


_AZD_STUB_POSIX = r"""#!/bin/sh
printf 'azd %s\n' "$*" >> "$STUB_LOG"
if [ "$SENTINEL_STUB_AZD_MODE" = "ok" ] &&
   [ "$*" = "auth token --scope https://appconfig.azure.com/.default" ]; then
  printf '%s\n' "$SENTINEL_STUB_AZD_OUTPUT"
  exit 0
fi
exit 1
"""
_AZD_STUB_CMD = (
    "@echo off",
    '>>"%STUB_LOG%" echo azd %*',
    'if not "%SENTINEL_STUB_AZD_MODE%"=="ok" exit /b 1',
    'if not "%*"=="auth token --scope https://appconfig.azure.com/.default" exit /b 1',
    "echo %SENTINEL_STUB_AZD_OUTPUT%",
    "exit /b 0",
)
# The Azure CLI whose one-time OIDC assertion has expired: every token request
# fails, and it says why on both streams.
_AZ_STUB_POSIX = r"""#!/bin/sh
printf 'az %s\n' "$*" >> "$STUB_LOG"
echo 'AADSTS700024: Client assertion is not within its valid time range.'
echo 'AADSTS700024: Client assertion is not within its valid time range.' >&2
exit 1
"""
_AZ_STUB_CMD = (
    "@echo off",
    '>>"%STUB_LOG%" echo az %*',
    "echo AADSTS700024: Client assertion is not within its valid time range.",
    ">&2 echo AADSTS700024: Client assertion is not within its valid time range.",
    "exit /b 1",
)

_LOAD_ALL = rf"""
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
  {_ps_literal(str(SCRIPT))}, [ref]$tokens, [ref]$errors
)
if ($errors.Count -gt 0) {{ throw ($errors | Out-String) }}
foreach ($statement in $ast.EndBlock.Statements) {{
  if ($statement -is [System.Management.Automation.Language.FunctionDefinitionAst]) {{
    Invoke-Expression $statement.Extent.Text
  }}
}}
"""

# Everything below the sentinel is real: token acquisition, the bounded child-job
# runner and the executables it launches. Only the environment, the clock and the
# HTTP send are replaced.
_INCIDENT_COMMAND = _LOAD_ALL + rf"""
$script:Results = [System.Collections.Generic.List[object]]::new()
$script:Puts = [System.Collections.Generic.List[object]]::new()
$script:NowSeconds = 0
function Get-EnvValue {{
  param([Parameter(Mandatory)][string]$Name)
  switch ($Name) {{
    'AZURE_APP_CONFIG_ENDPOINT' {{ return {_ps_literal(ENDPOINT)} }}
    'AZURE_PRINCIPAL_ID' {{ return 'deploy-principal' }}
    default {{ return $null }}
  }}
}}
function Get-MonotonicTime {{ return [double]$script:NowSeconds }}
# Compress the 900-second budget: each 30-second retry wait moves the fake clock
# ten times as far, so exhausting the budget costs three real attempts.
function Start-Sleep {{
  param($Seconds)
  $script:NowSeconds += 10 * [int]$Seconds
}}
function Invoke-AppConfigKeyValuePut {{
  param(
    [Parameter(Mandatory)][uri]$Uri,
    [Parameter(Mandatory)][string]$Token,
    [Parameter(Mandatory)][string]$Body,
    [Parameter(Mandatory)][int]$TimeoutSec
  )
  $fromAzd = $Token -ceq $env:SENTINEL_STUB_AZD_OUTPUT
  $script:Puts.Add([pscustomobject]@{{ uri = $Uri.AbsoluteUri; body = $Body; tokenFromAzd = $fromAzd }})
  if ($fromAzd) {{ return 200 }}
  return 401
}}
$VerbosePreference = 'Continue'
Register-AppConfigurationSentinel | Out-Host
$VerbosePreference = 'SilentlyContinue'
[pscustomobject]@{{ results = @($script:Results); puts = @($script:Puts) }} |
  ConvertTo-Json -Depth 6 -Compress
"""


def _write_stub(directory: Path, name: str, *, posix: str, windows: tuple[str, ...]) -> None:
    if os.name == "nt":
        (directory / f"{name}.cmd").write_bytes(("\r\n".join(windows) + "\r\n").encode("ascii"))
        return
    path = directory / name
    path.write_bytes(posix.encode("ascii"))
    path.chmod(0o755)


def _run_incident(*, azd_succeeds: bool) -> tuple[dict[str, object], str, list[str]]:
    if shutil.which("pwsh") is None:
        raise unittest.SkipTest("pwsh is required for executable postprovision tests")
    with tempfile.TemporaryDirectory() as tmp:
        stub_dir = Path(tmp) / "bin"
        stub_dir.mkdir()
        log = Path(tmp) / "calls.log"
        log.write_bytes(b"")
        _write_stub(stub_dir, "azd", posix=_AZD_STUB_POSIX, windows=_AZD_STUB_CMD)
        _write_stub(stub_dir, "az", posix=_AZ_STUB_POSIX, windows=_AZ_STUB_CMD)
        env = dict(os.environ)
        env["PATH"] = str(stub_dir) + os.pathsep + env.get("PATH", "")
        env["STUB_LOG"] = str(log)
        env["SENTINEL_STUB_AZD_MODE"] = "ok" if azd_succeeds else "fail"
        env["SENTINEL_STUB_AZD_OUTPUT"] = ISSUED_PREFIX + "azd-" + secrets.token_hex(16)
        payload, output = _run_pwsh_with_output(
            _INCIDENT_COMMAND, cwd=REPO, env=env, timeout=240
        )
        calls = [line.strip() for line in log.read_text(encoding="ascii").splitlines()]
    return payload, output, calls


class DeployIncidentRegressionTests(unittest.TestCase):
    """The failed release: the CLI's assertion has expired, azd's credential works."""

    def test_expired_cli_assertion_no_longer_blocks_the_sentinel(self) -> None:
        payload, output, calls = _run_incident(azd_succeeds=True)
        self.assertEqual(
            [(result["Status"], result["Detail"]) for result in payload["results"]],
            [("PASS", "Warm:Sentinel=ready (unlabeled)")],
        )
        self.assertEqual(
            payload["puts"], [{"uri": SENTINEL_URL, "body": BODY, "tokenFromAzd": True}]
        )
        self.assertEqual(calls, ["azd " + " ".join(AZD_ARGS)])
        self.assertNotIn(ISSUED_PREFIX, output)
        self.assertNotIn("AADSTS700024", output)

    def test_control_the_same_fixture_fails_when_azd_has_no_token_either(self) -> None:
        payload, output, calls = _run_incident(azd_succeeds=False)
        self.assertEqual(
            [(result["Status"], result["Detail"]) for result in payload["results"]],
            [("FAIL", _fail_detail(3))],
        )
        self.assertEqual(payload["puts"], [])
        # azd first on every attempt, the CLI only as its fallback.
        self.assertEqual(calls, ["azd " + " ".join(AZD_ARGS), "az " + " ".join(AZ_ARGS)] * 3)
        self.assertNotIn(ISSUED_PREFIX, output)
        self.assertNotIn("AADSTS700024", output)


class LegacyCliPathTests(unittest.TestCase):
    def test_the_cli_key_value_write_is_gone(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        for fragment in (
            "'appconfig', 'kv'",
            "appconfig kv",
            "Invoke-AppConfigSet",
            "--auth-mode",
            "--connection-string",
        ):
            with self.subTest(fragment=fragment):
                self.assertNotIn(fragment, source)


if __name__ == "__main__":
    unittest.main()
