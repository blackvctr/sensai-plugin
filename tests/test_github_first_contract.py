from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_public_repository_exposes_native_codex_and_claude_marketplaces() -> None:
    codex = json.loads((ROOT / ".agents/plugins/marketplace.json").read_text(encoding="utf-8"))
    claude = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text(encoding="utf-8"))

    assert codex["plugins"][0]["source"] == {
        "source": "local",
        "path": "./plugins/sensai",
    }
    assert claude["owner"] == {
        "name": "Black Vector",
        "email": "sergey@black-vector.com",
    }
    assert claude["plugins"][0]["source"] == "./plugins/sensai"
    plugin = ROOT / "plugins/sensai"
    assert (plugin / ".codex-plugin/plugin.json").is_file()
    assert (plugin / ".claude-plugin/plugin.json").is_file()


def test_remote_mcp_uses_native_oauth_discovery_without_static_credentials() -> None:
    expected = {
        "mcpServers": {
            "sensai": {
                "type": "http",
                "url": "https://black-vector.com/sensai/mcp",
            }
        }
    }
    source = json.loads((ROOT / "payload-src/shared/.mcp.json").read_text(encoding="utf-8"))
    public = json.loads((ROOT / "plugins/sensai/.mcp.json").read_text(encoding="utf-8"))

    assert source == expected
    assert public == expected


def test_public_mcp_contract_has_no_client_conversation_identifier() -> None:
    contract = json.loads((ROOT / "contracts" / "mcp-surface-v1.json").read_text(encoding="utf-8"))

    tool = contract["tools"][0]
    schema = tool["inputSchema"]
    assert tool["name"] == "tell_sensai"
    assert schema["required"] == ["message", "request_id"]
    assert set(schema["properties"]) == {"message", "request_id", "guidance_request"}
    assert schema["properties"]["guidance_request"]["default"] is None


def test_public_plugin_contains_no_executable_server_package() -> None:
    executable_suffixes = {".bat", ".cmd", ".exe", ".ps1", ".py", ".sh"}
    payload_root = ROOT / "plugins" / "sensai"
    executable_files = [
        path.relative_to(payload_root).as_posix()
        for path in payload_root.rglob("*")
        if path.is_file() and path.suffix.lower() in executable_suffixes
    ]

    assert executable_files == []
