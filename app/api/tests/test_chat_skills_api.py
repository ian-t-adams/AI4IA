from __future__ import annotations

import json

from fastapi.testclient import TestClient

from ai4ia_api.agents.mcp_client import FakeMcpConnector, McpResourceResult
from ai4ia_api.agents.mcp_servers import DiscoveredResource
from ai4ia_api.agents.official_mcp_service import OfficialMcpService
from ai4ia_api.main import create_app
from ai4ia_api.official_mcp_catalog import OfficialMcpCatalog, OfficialMcpServer
from tests.conftest import make_settings

_URI = "skill://evidence-review/SKILL.md?version=7"
_SKILL = "# Evidence review\n\nSeparate direct evidence from inference."
_PUBLIC_RESOLVER = lambda _host: ["93.184.216.34"]  # noqa: E731


class _SkillThenAnswerGateway:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def complete(
        self,
        *,
        deployment,
        messages,
        params=None,
        correlation_id=None,
        api="chat",
    ):
        self.calls.append({"messages": messages, "params": params or {}})
        if len(self.calls) == 1:
            names = {
                item["function"]["name"]
                for item in (params or {}).get("tools", [])
            }
            assert "load_skill" in names
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "skill-1",
                                    "type": "function",
                                    "function": {
                                        "name": "load_skill",
                                        "arguments": json.dumps(
                                            {"name": "evidence-review"}
                                        ),
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        assert any(
            message.get("role") == "tool"
            and "Separate direct evidence" in message.get("content", "")
            for message in messages
        )
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "The evidence has been reviewed.",
                    }
                }
            ]
        }


def test_curated_skill_loads_progressively_and_is_captured_in_receipt():
    app = create_app(
        make_settings(
            official_mcp_enabled=True,
            official_mcp_gateway_url="https://mcp.example.com",
            official_mcp_subscription_key="subscription-key",
        )
    )
    client = TestClient(app)
    client.__enter__()
    try:
        connector = FakeMcpConnector(
            resources=[
                DiscoveredResource(
                    uri=_URI,
                    name="evidence-review",
                    description="Review evidence transparently.",
                    mimeType="text/markdown",
                )
            ],
            resource_results={
                _URI: McpResourceResult(
                    uri=_URI,
                    text=_SKILL,
                    mime_type="text/markdown",
                )
            },
        )
        client.app.state.official_mcp_service = OfficialMcpService(
            OfficialMcpCatalog(
                servers=[
                    OfficialMcpServer(
                        id="ai4ia-toolbox",
                        displayName="AI4IA Toolbox",
                        path="ai4ia-toolbox/mcp",
                        resourcesEnabled=True,
                    )
                ]
            ),
            gateway_url="https://mcp.example.com",
            subscription_key="subscription-key",
            connector=connector,
            resolver=_PUBLIC_RESOLVER,
        )
        client.app.state.gateway = _SkillThenAnswerGateway()

        created = client.post(
            "/api/agents",
            json={
                "name": "reviewer",
                "systemPrompt": "Review evidence.",
                "tools": [],
            },
        )
        assert created.status_code == 201, created.text
        session = client.post(
            "/api/sessions",
            json={"title": "Skill test", "model": "gpt-5.2"},
        )
        assert session.status_code == 201, session.text

        response = client.post(
            "/api/chat",
            json={
                "sessionId": session.json()["id"],
                "content": "@reviewer inspect this",
                "stream": False,
            },
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["message"]["content"] == "The evidence has been reviewed."
        assert body.get("approvals", []) == []
        receipt = body["message"]["executionReceipt"]
        assert {offer["name"] for offer in receipt["toolsOffered"]} == {
            "load_skill"
        }
        [call] = receipt["toolCalls"]
        assert call["tool"] == "load_skill"
        result = json.loads(call["result"]["text"])
        assert result["version"] == "7"
        assert result["source"]["uri"] == _URI
        assert result["instructions"] == _SKILL
        assert connector.resource_reads[0][1] == _URI
    finally:
        client.__exit__(None, None, None)
