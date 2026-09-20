"""Synthetic directory/ARM projections; never load workstation configuration."""
from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from uuid import UUID
from xml.etree import ElementTree as ET

import _claude_binding as claude

ROOT = Path(__file__).resolve().parents[2]


def binding() -> claude.Binding:
    values = {key: str(UUID(int=index + 1)) for index, key in enumerate(sorted(claude.GUID_FIELDS))}
    values.update(
        environment="fixture", workload="ai4ia", targetResourceGroup="rg-claude-fixture",
        targetAccountName="mf-claude-fixture", federatedCredentialName="runtime-uami",
        networkMode="public-keyless",
    )
    prefix = f"/subscriptions/{values['sourceSubscriptionId']}/resourceGroups/rg-source-fixture/providers/"
    values["sourceIdentityResourceId"] = prefix + "Microsoft.ManagedIdentity/userAssignedIdentities/id-claude-fixture"
    values["sourceApimResourceId"] = prefix + "Microsoft.ApiManagement/service/apim-fixture"
    values["targetInferenceRoleDefinitionId"] = (
        f"/subscriptions/{values['targetSubscriptionId']}/resourceGroups/{values['targetResourceGroup']}"
        f"/providers/Microsoft.Authorization/roleDefinitions/{UUID(int=999)}"
    )
    return claude.parse_binding(json.dumps(values))


def environment(value: claude.Binding) -> dict[str, str]:
    return {
        "AI4IA_CLAUDE_ENABLED": "true", "AI4IA_CLAUDE_EXTERNAL_ENABLED": "true",
        "AI4IA_CLAUDE_BINDING_JSON": json.dumps(value.values),
        "AZURE_SUBSCRIPTION_ID": value["sourceSubscriptionId"],
        "AZURE_TENANT_ID": value["sourceTenantId"], "AZURE_ENV_NAME": value["environment"],
        "AI4IA_WORKLOAD": value["workload"],
    }


def models() -> dict:
    return json.loads((ROOT / "infra" / "models.json").read_text(encoding="utf-8"))


class FixtureReader:
    def __init__(self, value: claude.Binding, *, enabled: bool = True):
        self.binding = value
        self.calls = []
        self.responses = {}
        self.contexts = {
            target: {
                "id": value["targetSubscriptionId" if target else "sourceSubscriptionId"],
                "tenantId": value["targetTenantId" if target else "sourceTenantId"],
                "user": {"type": "servicePrincipal", "name": value["targetReaderClientId"]},
            }
            for target in (False, True)
        }

        def add(target, path, body):
            self.responses[(target, path)] = body

        add(False, value["sourceIdentityResourceId"], {
            "id": value["sourceIdentityResourceId"],
            "properties": {
                "tenantId": value["sourceTenantId"], "clientId": value["sourceIdentityClientId"],
                "principalId": value["sourceIdentityPrincipalId"],
            },
        })
        add(False, value["sourceApimResourceId"], {
            "id": value["sourceApimResourceId"],
            "identity": {
                "type": "SystemAssigned, UserAssigned", "tenantId": value["sourceTenantId"],
                "principalId": value["sourceApimPrincipalId"],
                "userAssignedIdentities": {
                    value["sourceIdentityResourceId"]: {
                        "clientId": value["sourceIdentityClientId"], "principalId": value["sourceIdentityPrincipalId"],
                    },
                },
            },
        })
        add(False, "/applications/" + value["applicationObjectId"], {
            "id": value["applicationObjectId"], "appId": value["applicationClientId"],
            "signInAudience": "AzureADMultipleOrgs", "passwordCredentials": [], "keyCredentials": [],
        })
        add(False, "/applications/" + value["applicationObjectId"] + "/federatedIdentityCredentials", {"value": [{
            "name": value["federatedCredentialName"],
            "issuer": f"https://login.microsoftonline.com/{value['sourceTenantId']}/v2.0",
            "subject": value["sourceIdentityPrincipalId"], "audiences": ["api://AzureADTokenExchange"],
        }]})
        add(True, "/servicePrincipals/" + value["targetPrincipalId"], {
            "id": value["targetPrincipalId"], "appId": value["applicationClientId"],
            "appOwnerOrganizationId": value["sourceTenantId"],
            "accountEnabled": True, "servicePrincipalType": "Application",
        })
        add(True, value.account_id, {
            "id": value.account_id, "name": value["targetAccountName"], "kind": "AIServices", "location": "eastus2",
            "tags": {"ai4ia-target": "external-claude", "azd-env-name": value["environment"]},
            "properties": {
                "provisioningState": "Succeeded", "disableLocalAuth": True,
                "publicNetworkAccess": "Enabled",
                "customSubDomainName": value["targetAccountName"],
            },
        })
        add(True, value.account_id + "/deployments", {"value": [
            {
                "id": value.account_id + "/deployments/" + row["name"], "name": row["name"],
                "sku": {"name": row["sku"], "capacity": row["capacity"]},
                "properties": {
                    "model": row["model"], "provisioningState": "Succeeded", "versionUpgradeOption": "NoAutoUpgrade",
                },
            }
            for row in claude.requirements(models())
        ]})
        add(True, value.account_id + "/providers/Microsoft.Authorization/roleAssignments", {"value": [{
            "properties": {
                "principalId": value["targetPrincipalId"], "principalType": "ServicePrincipal",
                "roleDefinitionId": value["targetInferenceRoleDefinitionId"], "scope": value.account_id,
            },
        }]})
        add(True, value["targetInferenceRoleDefinitionId"], {
            "id": value["targetInferenceRoleDefinitionId"],
            "properties": {
                "type": "CustomRole", "assignableScopes": [value.account_id.rsplit("/providers/", 1)[0]],
                "permissions": [{"actions": [], "notActions": [], "dataActions": claude.INFERENCE_ACTIONS[:], "notDataActions": []}],
            },
        })
        policies = ROOT / "infra" / "policies"
        wrapper = (policies / "simplel7proxy-priority-policy.xml").read_text(encoding="utf-8")
        for node in ET.fromstring(wrapper).iter("include-fragment"):
            base = node.attrib["fragment-id"]
            reference = base + "-fixture000000"
            wrapper = wrapper.replace(f'"{base}"', f'"{reference}"')
            filename = base + ".xml"
            if base == "claude_auth_v1":
                filename = "claude-federated-auth.xml" if enabled else "claude-disabled.xml"
            elif base == "endpoint_selection_setup_32":
                filename = "simplel7proxy-endpoints.xml"
            elif base.startswith("endpoint_selection_catalog_"):
                filename = "simplel7proxy-endpoints-catalog-" + base.split("_")[3] + ".xml"
            assert re.fullmatch(re.escape(base) + "-[a-z0-9]{13}", reference)
            add(False, value["sourceApimResourceId"] + "/policyFragments/" + reference, {
                "properties": {"value": (policies / filename).read_text(encoding="utf-8")},
            })
        add(False, value["sourceApimResourceId"] + "/apis/openai/policies/policy", {"properties": {"value": wrapper}})
        add(False, value["sourceApimResourceId"] + "/namedValues", {"value": [
            {"name": key, "properties": {"value": text, "secret": False}} for key, text in value.named_values.items()
        ]})
        add(False, value["sourceApimResourceId"] + "/subscriptions/ai4ia-proxy-models", {
            "properties": {
                "scope": value["sourceApimResourceId"] + "/apis/openai", "state": "active", "allowTracing": False,
            },
        })

    def command(self, target, args):
        self.calls.append((target, tuple(args)))
        assert args == ["account", "show"]
        return deepcopy(self.contexts[target])

    def get(self, target, path, version, *, graph=False):
        self.calls.append((target, path, version, graph))
        assert graph == path.startswith(("/applications/", "/servicePrincipals/"))
        assert graph or version
        return deepcopy(self.responses[(target, path)])
