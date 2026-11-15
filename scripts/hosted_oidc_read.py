"""Source-only, one-GET candidate. Not approved for live execution."""

import base64
import binascii
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

logging.disable(logging.CRITICAL)

TENANT = "6907d2a4-685a-4aea-92ab-d930217467f1"
SUBSCRIPTION = "e852113b-6cb5-441c-ac68-26cff884e479"
CLIENT = "ac8d62b9-904f-4c84-9444-c41512acf74f"
REPOSITORY = "ian-t-adams/AI4IA"
RESOURCE = (
    f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-ai4ia-slurmfactory"
    "/providers/Microsoft.App/containerApps/ca-web-slurmfactory"
)
ARM_URL = "https://management.azure.com" + RESOURCE + "?api-version=2025-01-01"
TOKEN_URL = f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token"
AUDIENCE = "api://AzureADTokenExchange"
SUBJECT = f"repo:{REPOSITORY}:environment:production"
MAX_BODY = 1024 * 1024
PHASES = {
    "admission", "environment", "assertion", "assertion-url", "assertion-runtime-token",
    "assertion-transport", "assertion-response", "assertion-claims",
    "credential", "arm", "response", "timeout", "carrier",
}


class Refused(Exception):
    def __init__(self, phase, status=None):
        self.phase = phase
        self.status = status if type(status) is int and 100 <= status <= 599 else None


def result(phase, status=None, digest=None):
    if phase not in PHASES and phase != "complete":
        raise ValueError("invalid phase")
    return {"phase": phase, "status": status, "sha256": digest}


def validate_environment(env):
    expected = {
        "AI4IA_HOSTED_WEB_READ_ENABLED": "true",
        "AI4IA_HOSTED_WEB_READ_ADMIT": "true",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_REPOSITORY": REPOSITORY,
        "AZURE_TENANT_ID": TENANT,
        "AZURE_SUBSCRIPTION_ID": SUBSCRIPTION,
        "AZURE_CLIENT_ID": CLIENT,
    }
    if any(env.get(k) != v for k, v in expected.items()):
        raise Refused("admission")
    sha = env.get("GITHUB_SHA", "")
    if not re.fullmatch(r"[0-9a-f]{40}", sha) or env.get("AI4IA_REVIEWED_SHA") != sha:
        raise Refused("admission")
    denied = {
        "http_proxy", "https_proxy", "all_proxy", "no_proxy",
        "requests_ca_bundle", "curl_ca_bundle", "ssl_cert_file", "ssl_cert_dir", "sslkeylogfile",
        "pythonhttpsverify", "azure_authority_host", "azure_regional_authority_name",
    }
    if any(k.lower() in denied and value for k, value in env.items()):
        raise Refused("environment")


def oidc_url(value):
    if (not isinstance(value, str) or len(value) > 4096
            or any(ord(char) <= 32 or ord(char) == 127 for char in value)):
        raise Refused("assertion-url")
    try:
        p = urlsplit(value)
        # GitHub documents *.actions.githubusercontent.com for OIDC retrieval.
        if (p.scheme != "https"
                or not re.fullmatch(
                    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.actions\.githubusercontent\.com",
                    p.hostname or "",
                )
                or p.port not in (None, 443) or p.username or p.password or p.fragment
                or not p.path.startswith("/") or "\\" in value):
            raise Refused("assertion-url")
        query = parse_qsl(p.query, keep_blank_values=True, strict_parsing=True)
        if any(k.lower() == "audience" for k, _ in query):
            raise Refused("assertion-url")
        query.append(("audience", AUDIENCE))
        return urlunsplit((p.scheme, p.netloc, p.path, urlencode(query), ""))
    except ValueError:
        raise Refused("assertion-url") from None


def check_assertion(value, sha, now):
    if not isinstance(value, str) or len(value) > 32768:
        raise Refused("assertion-claims")
    try:
        parts = value.split(".")
        if len(parts) != 3 or not all(parts):
            raise ValueError()
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        expected = {
            "iss": "https://token.actions.githubusercontent.com",
            "sub": SUBJECT,
            "aud": AUDIENCE,
            "repository": REPOSITORY,
            "ref": "refs/heads/main",
            "sha": sha,
            "environment": "production",
            "event_name": "workflow_dispatch",
        }
        if not isinstance(claims, dict) or any(claims.get(k) != v for k, v in expected.items()):
            raise ValueError()
        if type(claims.get("exp")) is not int or claims["exp"] <= now:
            raise ValueError()
        if type(claims.get("nbf")) is not int or claims["nbf"] > now:
            raise ValueError()
    except (ValueError, TypeError, UnicodeError, binascii.Error):
        raise Refused("assertion-claims") from None
    return value


def _bounded(response, limit, phase):
    status = response.status_code
    if type(status) is not int or not 100 <= status <= 599:
        raise Refused(phase)
    if 300 <= status <= 399:
        raise Refused(phase, status)
    content_encoding = response.headers.get("Content-Encoding", "").strip().lower()
    if content_encoding not in ("", "identity"):
        raise Refused(phase, status)
    content_length = response.headers.get("Content-Length")
    if content_length is not None:
        if not content_length.isascii() or not content_length.isdigit() or int(content_length) > limit:
            raise Refused(phase, status)
    body = bytearray()
    for chunk in response.iter_content(chunk_size=16384):
        body.extend(chunk)
        if len(body) > limit:
            raise Refused(phase, status)
    if content_length is not None and len(body) != int(content_length):
        raise Refused(phase, status)
    return status, bytes(body)


def _run_owned_child(command: Sequence[str], timeout: float = 45, max_output: int = 4096):
    child = subprocess.Popen(
        list(command), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, bufsize=0,
    )
    output = bytearray()
    overflow = threading.Event()
    drain_failed = threading.Event()

    def drain():
        try:
            assert child.stdout is not None
            while chunk := child.stdout.read(4096):
                room = max_output + 1 - len(output)
                if room > 0:
                    output.extend(chunk[:room])
                if len(output) > max_output:
                    overflow.set()
        except Exception:
            drain_failed.set()

    reader = None
    try:
        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        deadline = time.monotonic() + timeout
        timed_out = False
        while child.poll() is None:
            if drain_failed.is_set():
                raise RuntimeError("child output read failed")
            if overflow.is_set() or time.monotonic() >= deadline:
                timed_out = not overflow.is_set()
                break
            time.sleep(0.01)
    finally:
        try:
            try:
                if child.returncode is None:
                    child.kill()
            finally:
                child.wait(timeout=5)
        finally:
            try:
                if reader is not None and reader.ident is not None:
                    reader.join(timeout=5)
                    if reader.is_alive():
                        raise RuntimeError("child output reader did not stop")
            finally:
                if child.stdout is not None:
                    child.stdout.close()
    if drain_failed.is_set():
        raise RuntimeError("child output read failed")
    code = child.returncode
    return code, bytes(output), timed_out, overflow.is_set()


def execute(env, credential_class=None, transport_class=None):
    validate_environment(env)
    import requests
    from requests.adapters import HTTPAdapter

    if credential_class is None:
        from azure.identity import ClientAssertionCredential as credential_class
    if transport_class is None:
        from azure.core.pipeline.transport import RequestsTransport as transport_class

    phase = "assertion"
    counts = {"assertion": 0, "credential": 0, "arm": 0}
    seen_status: dict[str, int | None] = {"credential": None}
    assertion_failure: dict[str, str | int | None] = {"phase": None, "status": None}
    asserted = []

    def new_session():
        session = requests.Session()
        session.trust_env = False
        session.mount("https://", HTTPAdapter(max_retries=0))
        return session

    class TokenSession(requests.Session):
        def __init__(self):
            super().__init__()
            self.trust_env = False
            self.mount("https://", HTTPAdapter(max_retries=0))

        def request(self, method, url, params=None, data=None, headers=None, cookies=None,
                    files=None, auth=None, timeout=None, allow_redirects=True, proxies=None,
                    hooks=None, stream=None, verify=None, cert=None, json=None):
            counts["credential"] += 1
            if counts["credential"] != 1 or method != "POST" or url != TOKEN_URL:
                raise Refused("credential")
            if any(value is not None for value in (params, cookies, files, auth, hooks, cert, json)):
                raise Refused("credential")
            if not asserted or data != {
                "client_assertion": asserted[0],
                "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                "client_id": CLIENT,
                "grant_type": "client_credentials",
                "scope": "https://management.azure.com/.default",
            }:
                raise Refused("credential")
            token_headers = dict(headers or {})
            token_headers["Accept-Encoding"] = "identity"
            response = super().request(method, url, data=data, headers=token_headers, cookies=cookies,
                                       hooks=hooks, allow_redirects=False, stream=True,
                                       verify=True, timeout=(5, 5), proxies={})
            try:
                seen_status["credential"], body = _bounded(response, 32768, "credential")
                if seen_status["credential"] != 200:
                    raise Refused("credential", seen_status["credential"])
                response._content = body
                response._content_consumed = True
                return response
            finally:
                response.close()

    def assertion():
        try:
            counts["assertion"] += 1
            if counts["assertion"] != 1:
                raise Refused("assertion")
            url = oidc_url(env.get("ACTIONS_ID_TOKEN_REQUEST_URL"))
            runtime_token = env.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
            if not runtime_token or len(runtime_token) > 16384:
                raise Refused("assertion-runtime-token")
            with new_session() as session:
                with session.get(url, headers={"Authorization": "Bearer " + runtime_token,
                                               "Accept-Encoding": "identity"},
                                 timeout=(5, 5), allow_redirects=False, stream=True, verify=True) as response:
                    status, body = _bounded(response, 65536, "assertion-response")
                    if status != 200:
                        raise Refused("assertion-response", status)
            try:
                payload = json.loads(body)
                value = payload["value"]
            except (ValueError, TypeError, KeyError):
                raise Refused("assertion-response", status) from None
            value = check_assertion(value, env["GITHUB_SHA"], int(time.time()))
            asserted.append(value)
            return value
        except Refused as e:
            assertion_failure.update(phase=e.phase, status=e.status)
            raise
        except Exception:
            assertion_failure.update(phase="assertion-transport", status=None)
            raise

    try:
        phase = "credential"
        token_session = TokenSession()
        transport = transport_class(session=token_session, session_owner=False)
        credential = credential_class(TENANT, CLIENT, assertion, authority="login.microsoftonline.com",
                                      transport=transport, retry_total=0)
        try:
            access = credential.get_token("https://management.azure.com/.default")
        finally:
            credential.close()
            token_session.close()
        phase = "arm"
        counts["arm"] += 1
        with new_session() as session:
            with session.get(ARM_URL, headers={"Authorization": "Bearer " + access.token,
                                               "Accept-Encoding": "identity"},
                             timeout=(5, 5), allow_redirects=False, stream=True, verify=True) as response:
                status, body = _bounded(response, MAX_BODY, "arm")
        if status != 200:
            raise Refused("arm", status)
        phase = "response"
        try:
            obj = json.loads(body)
            if (not isinstance(obj, dict) or not isinstance(obj.get("id"), str)
                    or obj["id"].lower() != RESOURCE.lower()
                    or obj.get("name") != "ca-web-slurmfactory"
                    or str(obj.get("type", "")).lower() != "microsoft.app/containerapps"):
                raise ValueError()
        except (ValueError, UnicodeError):
            raise Refused("response", status) from None
        return result("complete", status, hashlib.sha256(body).hexdigest())
    except Refused as e:
        return result(e.phase, e.status)
    except Exception:
        if assertion_failure["phase"]:
            return result(assertion_failure["phase"], assertion_failure["status"])
        return result(phase, seen_status["credential"] if phase == "credential" else None)


def main():
    if len(sys.argv) == 2 and sys.argv[1] == "--child":
        try:
            answer = execute(os.environ)
        except Refused as e:
            answer = result(e.phase, e.status)
        except Exception:
            answer = result("admission")
        print(json.dumps(answer, separators=(",", ":")))
        return 0 if answer["phase"] == "complete" else 1
    if len(sys.argv) != 1:
        print(json.dumps(result("admission")))
        return 1
    try:
        validate_environment(os.environ)
    except Refused as e:
        print(json.dumps(result(e.phase, e.status)))
        return 1
    try:
        code, raw, timed_out, overflow = _run_owned_child(
            [sys.executable, "-I", "-u", __file__, "--child"],
        )
    except Exception:
        print(json.dumps(result("carrier")))
        return 1
    if timed_out:
        print(json.dumps(result("timeout")))
        return 1
    if overflow:
        print(json.dumps(result("admission")))
        return 1
    try:
        parsed = json.loads(raw)
        if (code not in (0, 1) or len(raw) > 4096 or set(parsed) != {"phase", "status", "sha256"}
                or parsed["phase"] not in PHASES | {"complete"}
                or ((code == 0) != (parsed["phase"] == "complete"))
                or (parsed["status"] is not None and
                    (type(parsed["status"]) is not int or not 100 <= parsed["status"] <= 599))
                or (parsed["sha256"] is not None and
                    not re.fullmatch(r"[0-9a-f]{64}", parsed["sha256"]))
                or ((parsed["phase"] == "complete") != (parsed["sha256"] is not None))
                or (parsed["phase"] == "complete" and parsed["status"] != 200)):
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        parsed = result("admission")
    print(json.dumps(parsed, separators=(",", ":")))
    return 0 if parsed["phase"] == "complete" else 1


if __name__ == "__main__":
    sys.exit(main())
