"""Guard globally-unique resource names against omitting the uniqueness suffix.

Most Azure resource names only have to be unique inside their resource group.
A handful are unique across *all of Azure*, because they back a public DNS name:
APIM (`<name>.azure-api.net`), API Center, Cognitive Services/Foundry accounts
(`<name>.services.ai.azure.com`), Key Vault, storage, ACR, Cosmos, Search,
Event Hubs, App Configuration, and PostgreSQL flexible servers.

Omitting a per-subscription suffix from one of those produces a template that
deploys perfectly into the subscription that already owns the name, and fails
nowhere else until someone stands the stack up in a *different* subscription --
at which point Azure rejects it as already taken by the original environment.
That is the tenant-migration path, so the defect surfaces at the worst moment,
and it surfaces late: APIM and Foundry are created well after the resource
group, data tier, and monitoring already exist.

This is not hypothetical. `apim-mcp-*`, `apim-*`, `apic-*`, and the `mf-*`
Foundry accounts all shipped without the suffix that every other global resource
here already carried, and the first deploy into the new subscription failed with
`ServiceAlreadyExists` / "name is already taken" against the old tenant's stack.

The test resolves each name expression through `var`s in the same file and, for
module parameters, through the caller's argument, so it sees the value that
actually reaches ARM rather than the local identifier. An expression it cannot
resolve fails: silence would defeat the point.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INFRA = ROOT / "infra"

# Resource types whose names must be unique across all of Azure. Each entry is
# matched against the start of the type string (the API version is ignored).
GLOBALLY_UNIQUE = (
    "Microsoft.ApiManagement/service",
    "Microsoft.ApiCenter/services",
    "Microsoft.CognitiveServices/accounts",
    "Microsoft.KeyVault/vaults",
    "Microsoft.Storage/storageAccounts",
    "Microsoft.ContainerRegistry/registries",
    "Microsoft.DocumentDB/databaseAccounts",
    "Microsoft.Search/searchServices",
    "Microsoft.EventHub/namespaces",
    "Microsoft.ServiceBus/namespaces",
    "Microsoft.AppConfiguration/configurationStores",
    "Microsoft.DBforPostgreSQL/flexibleServers",
    # The Durable Task Scheduler's data plane is public DNS
    # (https://<name>.<region>.durabletask.io), so its name is globally unique.
    # Its taskHubs child is excluded automatically by the `parent:` check below.
    "Microsoft.DurableTask/schedulers",
)

# Anything that makes a name vary per subscription is acceptable evidence.
UNIQUENESS_MARKERS = ("uniqueSuffix", "uniqueString(")

RESOURCE_RE = re.compile(r"^resource\s+(\w+)\s+'([^']+)'\s*(existing\s*)?=?", re.MULTILINE)


def _bicep_files() -> list[Path]:
    return sorted(INFRA.rglob("*.bicep"))


def _blocks(text: str) -> list[tuple[str, str, str]]:
    """Yield (symbol, type, body) for every top-level resource declaration."""
    out = []
    matches = list(RESOURCE_RE.finditer(text))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        out.append((m.group(1), m.group(2), text[m.start() : end]))
    return out


def _first_name_expr(body: str) -> str | None:
    for line in body.splitlines()[1:]:
        stripped = line.strip()
        if stripped.startswith("name:"):
            return stripped[len("name:") :].strip()
        # Stop at the end of the resource's own property block.
        if stripped.startswith("resource "):
            break
    return None


def _assignments(text: str, keyword: str) -> dict[str, str]:
    """Map `var X = <expr>` (or `param X ...`) to its expression."""
    found = {}
    for line in text.splitlines():
        stripped = line.strip()
        m = re.match(rf"^{keyword}\s+(\w+)\s*(?:\w+)?\s*=\s*(.+)$", stripped)
        if m:
            found[m.group(1)] = m.group(2)
        elif keyword == "param":
            p = re.match(r"^param\s+(\w+)\s+\w+", stripped)
            if p:
                found.setdefault(p.group(1), "")
    return found


def _module_args(caller_text: str, module_filename: str, param: str) -> str | None:
    """Find the value a caller passes for `param` when invoking `module_filename`."""
    for m in re.finditer(rf"module\s+\w+\s+'[^']*{re.escape(module_filename)}'", caller_text):
        segment = caller_text[m.start() : m.start() + 4000]
        hit = re.search(rf"^\s*{re.escape(param)}:\s*(.+)$", segment, re.MULTILINE)
        if hit:
            return hit.group(1).strip()
    return None


def _resolve(expr: str, path: Path, depth: int = 0) -> str:
    """Expand identifiers until the expression shows how the name is built."""
    if depth > 6 or not expr:
        return expr
    if any(marker in expr for marker in UNIQUENESS_MARKERS):
        return expr
    text = path.read_text(encoding="utf-8")
    bare = expr.strip()
    # Index into an array var, e.g. foundryAccountNames[i].
    bare = re.sub(r"\[[^\]]*\]$", "", bare).strip()
    if not re.fullmatch(r"\w+", bare):
        return expr

    local_vars = _assignments(text, "var")
    if bare in local_vars:
        return _resolve(local_vars[bare], path, depth + 1)

    if bare in _assignments(text, "param"):
        # A module parameter: resolve against whatever the caller supplies.
        for caller in _bicep_files():
            if caller == path:
                continue
            supplied = _module_args(caller.read_text(encoding="utf-8"), path.name, bare)
            if supplied is not None:
                return _resolve(supplied, caller, depth + 1)
    return expr


class GloballyUniqueNamingTests(unittest.TestCase):
    def test_every_globally_unique_resource_name_varies_per_subscription(self) -> None:
        checked = 0
        failures = []
        for path in _bicep_files():
            text = path.read_text(encoding="utf-8")
            for symbol, rtype, body in _blocks(text):
                if not rtype.startswith(GLOBALLY_UNIQUE):
                    continue
                # `existing` references and child resources do not create a
                # globally unique name of their own.
                header = body.splitlines()[0]
                if "existing" in header:
                    continue
                if re.search(r"^\s*parent:\s*\w+", body, re.MULTILINE):
                    continue
                expr = _first_name_expr(body)
                if expr is None:
                    failures.append(f"{path.name}: {symbol} has no name: property")
                    continue
                checked += 1
                resolved = _resolve(expr, path)
                if not any(marker in resolved for marker in UNIQUENESS_MARKERS):
                    failures.append(
                        f"{path.name}: {symbol} ({rtype}) name {expr!r} resolves to "
                        f"{resolved!r}, which has no per-subscription uniqueness. "
                        "It will collide when deployed into another subscription."
                    )
        self.assertGreaterEqual(checked, 10, "resource discovery regressed; test is not looking")
        self.assertEqual(failures, [], "\n" + "\n".join(failures))

    def test_the_names_that_actually_collided_are_covered(self) -> None:
        """Pin the specific regressions, so a refactor cannot quietly drop them."""
        expectations = {
            "apimcore.bicep": "apim-mcp-",
            "apicenter.bicep": "apic-",
            "main.bicep": "mf-",
        }
        for filename, prefix in expectations.items():
            candidates = [INFRA / filename, INFRA / "modules" / filename]
            path = next((c for c in candidates if c.exists()), None)
            self.assertIsNotNone(path, f"{filename} not found under infra/")
            text = path.read_text(encoding="utf-8")
            hit = re.search(rf"'{re.escape(prefix)}[^']*\$\{{uniqueSuffix\}}[^']*'", text)
            self.assertIsNotNone(
                hit,
                f"{filename}: no '{prefix}...' name interpolates uniqueSuffix; "
                "this is the exact defect that broke the tenant migration.",
            )

        # gateway.bicep used to be a fourth entry here, for the Consumption APIM it
        # created as a migration rollback plane. That service has been deleted and
        # the module now creates no APIM service at all, so the expectation is not
        # dropped silently -- it is asserted gone.
        gateway = (INFRA / "modules" / "gateway.bicep").read_text(encoding="utf-8")
        self.assertNotIn("Microsoft.ApiManagement/service@2024-05-01' = {", gateway)
        self.assertNotIn("uniqueSuffix", gateway)

    def test_foundry_project_is_not_suffixed(self) -> None:
        """A project is a child of the account, so it needs no global uniqueness.

        Suffixing it would push the name past the 60-character cap and truncate
        the suffix off the account endpoint it is paired with.
        """
        text = (INFRA / "main.bicep").read_text(encoding="utf-8")
        hit = re.search(r"proj-default-[^']*'", text)
        self.assertIsNotNone(hit)
        self.assertNotIn("uniqueSuffix", hit.group(0))

    def test_account_and_endpoint_names_come_from_one_expression(self) -> None:
        """The Foundry account name is used twice: by the module loop and by the
        hand-computed primary project endpoint. They must not be able to drift,
        or the endpoint would point at an account that does not exist."""
        text = (INFRA / "main.bicep").read_text(encoding="utf-8")
        # Exactly one place may build the 'mf-' name.
        builders = re.findall(r"take\('mf-", text)
        self.assertEqual(
            len(builders), 1, "the 'mf-' account name is built in more than one place"
        )
        self.assertIn("foundryAccountNames[primaryFoundryIndex]", text)
        self.assertIn("accountName: foundryAccountNames[i]", text)

    def test_length_capped_names_preserve_the_complete_suffix(self) -> None:
        targets = (
            (INFRA / "modules" / "keyvault.bicep", "keyVaultName"),
            (INFRA / "modules" / "proxyasync.bicep", "storageName"),
            (INFRA / "modules" / "proxyasync.bicep", "serviceBusName"),
        )
        for path, variable in targets:
            text = path.read_text(encoding="utf-8")
            match = re.search(rf"^var {variable} = (.+)$", text, re.MULTILINE)
            self.assertIsNotNone(match, f"{path.name}: missing {variable}")
            expression = match.group(1)
            self.assertRegex(
                expression,
                r"^'\$\{take\(\w+,\s*\d+\)\}\$\{uniqueSuffix\}'$",
                f"{path.name}: truncate the prefix before appending uniqueSuffix",
            )
            self.assertNotRegex(
                expression,
                r"take\([^)]*uniqueSuffix",
                f"{path.name}: take() would truncate the uniqueness suffix",
            )


# APIM child entities (products, subscriptions) are NOT globally unique -- they only
# have to be unique inside their APIM service. That is precisely the problem: the
# shared Basic v2 plane is deliberately built to front more than one workload, so a
# second workload deploying the same hardcoded child names does not fail loudly. It
# adopts and overwrites the first workload's product/subscription, silently rotating
# the key the running proxy authenticates with.
#
# The class above cannot catch this: it skips anything with a `parent:`, because such
# names carry no global uniqueness requirement. So this is a separate property with a
# separate rule -- per-workload, not per-subscription.
#
# APIs are deliberately excluded. A model/realtime API is shared infrastructure that a
# second workload should reuse; only the credentials scoped to it must be per-workload.
APIM_CHILD_CREDENTIALS = (
    "Microsoft.ApiManagement/service/products",
    "Microsoft.ApiManagement/service/subscriptions",
)


class ApimChildNamingTests(unittest.TestCase):
    def test_apim_child_credential_names_are_workload_derived(self) -> None:
        checked = 0
        failures = []
        for path in _bicep_files():
            text = path.read_text(encoding="utf-8")
            for symbol, rtype, body in _blocks(text):
                # products/apis is a link table, not a credential; match exactly.
                if not any(rtype.startswith(f"{t}@") for t in APIM_CHILD_CREDENTIALS):
                    continue
                if "existing" in body.splitlines()[0]:
                    continue
                expr = _first_name_expr(body)
                if expr is None:
                    failures.append(f"{path.name}: {symbol} has no name: property")
                    continue
                checked += 1
                resolved = _resolve(expr, path)
                if "workload" not in resolved:
                    failures.append(
                        f"{path.name}: {symbol} ({rtype}) name {expr!r} resolves to "
                        f"{resolved!r}, which does not vary by workload. A second "
                        "workload on the shared APIM plane would adopt and overwrite "
                        "this credential, rotating the key its proxy is using."
                    )
        self.assertGreaterEqual(
            checked, 6, "APIM child discovery regressed; the test is not looking"
        )
        self.assertEqual(failures, [], "\n" + "\n".join(failures))

    def test_default_workload_preserves_the_deployed_child_names(self) -> None:
        """The derived names must still emit the live 'ai4ia-*' names today.

        Deriving these from ${workload} is only safe because the default workload is
        unchanged. If someone edits that default, ARM does not rename an APIM
        subscription in place -- it creates a new one and the primary key changes,
        which breaks the running proxy's APIM hop until it picks up the new secret.
        """
        main = (INFRA / "main.bicep").read_text(encoding="utf-8")
        self.assertIn(
            "param workload string = 'ai4ia'",
            main,
            "the default workload changed; every APIM child credential would be "
            "recreated under a new name and re-keyed on the next deploy",
        )


if __name__ == "__main__":
    unittest.main()
