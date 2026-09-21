from __future__ import annotations

from types import SimpleNamespace

from app.collectors import agent_reach


def test_decode_cli_output_prefers_utf8_for_opencli_unicode():
    text = "[OK] Extension: connected (v1.0.23) → v1.0.24 available\n[OK] Connectivity: connected in 0.0s\n小红书"
    assert agent_reach._decode_cli_output(text.encode("utf-8")) == text


def test_opencli_doctor_handles_utf8_bytes_without_windows_text_decoder(monkeypatch):
    monkeypatch.setattr(agent_reach.shutil, "which", lambda name: f"C:/fake/{name}.cmd" if name == "opencli" else None)

    output = (
        "opencli v1.8.7 doctor (node v20.19.0)\n"
        "[OK] Daemon: running on port 19825 (v1.8.7)\n"
        "[OK] Extension: connected (v1.0.23) → v1.0.24 available\n"
        "Profiles:\n  • profile: connected v1.0.23\n"
        "[OK] Connectivity: connected in 0.0s\n"
    ).encode("utf-8")

    def fake_run(cmd, capture_output, timeout, check):
        # Important regression assertion: text=True/encoding must not be passed,
        # because that lets Windows choose GBK before our code sees the bytes.
        return SimpleNamespace(returncode=0, stdout=output, stderr=b"")

    monkeypatch.setattr(agent_reach.subprocess, "run", fake_run)
    result = agent_reach._opencli_doctor()
    assert result["installed"] is True
    assert result["connected"] is True
    assert result["extension_connected"] is True
    assert result["version"] == "1.8.7"


def test_agent_reach_doctor_promotes_p0_when_opencli_is_connected(monkeypatch):
    def fake_which(name):
        if name in {"agent-reach", "opencli"}:
            return f"C:/fake/{name}.cmd"
        return None

    monkeypatch.setattr(agent_reach.shutil, "which", fake_which)

    agent_json = (
        '{"reddit":{"status":"off","name":"Reddit 帖子和评论","active_backend":null},'
        '"xiaohongshu":{"status":"off","name":"小红书笔记","active_backend":null}}'
    ).encode("utf-8")
    opencli_output = (
        "opencli v1.8.7 doctor (node v20.19.0)\n"
        "[OK] Extension: connected (v1.0.23) → v1.0.24 available\n"
        "[OK] Connectivity: connected in 0.0s\n"
    ).encode("utf-8")

    calls = []
    def fake_run(cmd, capture_output, timeout, check):
        calls.append(cmd)
        if cmd[0].endswith("agent-reach.cmd"):
            return SimpleNamespace(returncode=0, stdout=agent_json, stderr=b"")
        return SimpleNamespace(returncode=0, stdout=opencli_output, stderr=b"")

    monkeypatch.setattr(agent_reach.subprocess, "run", fake_run)
    result = agent_reach.doctor()
    assert result["status"] == "OK"
    assert result["opencli"]["connected"] is True
    assert result["channels"]["reddit"]["effective_backend"] == "OpenCLI Browser Bridge"
    assert result["channels"]["xiaohongshu"]["effective_backend"] == "OpenCLI Browser Bridge"


def test_real_collection_resolves_bare_opencli_to_windows_cmd_shim(monkeypatch):
    """Regression: doctor could find opencli.cmd while search used bare opencli and hit WinError 2."""
    monkeypatch.setattr(
        agent_reach.shutil,
        "which",
        lambda name: "C:/Users/test/AppData/Roaming/npm/opencli.cmd" if name == "opencli" else None,
    )

    calls = []

    def fake_run(cmd, capture_output, timeout, check):
        calls.append(cmd)
        return SimpleNamespace(
            returncode=0,
            stdout=(
                "- id: abc123\n"
                "  title: Need an Excel template\n"
                "  author: buyer\n"
                "  url: https://www.reddit.com/r/excel/comments/abc123/example/\n"
                "  created_utc: 1780000000\n"
            ).encode("utf-8"),
            stderr=b"",
        )

    monkeypatch.setattr(agent_reach.subprocess, "run", fake_run)
    body = agent_reach._run(["opencli", "reddit", "search", "excel template", "-f", "yaml"])

    assert calls
    assert calls[0][0].lower() == "cmd"
    assert calls[0][1].lower() == "/c"
    assert calls[0][2].lower().endswith("opencli.cmd")
    assert isinstance(body, list)
    assert body[0]["id"] == "abc123"


def test_resolve_cli_command_keeps_arguments_unchanged(monkeypatch):
    monkeypatch.setattr(
        agent_reach.shutil,
        "which",
        lambda name: "C:/Program Files/nodejs/opencli.cmd" if name == "opencli" else None,
    )
    original = ["opencli", "xiaohongshu", "search", "求 Excel 模板 & 自动化", "-f", "yaml"]
    resolved = agent_reach._resolve_cli_command(original)
    assert resolved[0].lower() == "cmd"
    assert resolved[1].lower() == "/c"
    assert resolved[2] == "C:/Program Files/nodejs/opencli.cmd"
    assert resolved[3:] == original[1:]
    assert original[0] == "opencli"


def test_xiaohongshu_most_liked_search_builds_ranked_opencli_command(monkeypatch):
    monkeypatch.setattr(
        agent_reach.shutil,
        "which",
        lambda name: "C:/fake/opencli.cmd" if name == "opencli" else None,
    )

    commands = agent_reach._commands(
        "xiaohongshu",
        "Excel模板",
        30,
        "OpenCLI Browser Bridge",
        sort="most-liked",
    )

    assert commands == [[
        "opencli",
        "xiaohongshu",
        "search",
        "Excel模板",
        "--limit",
        "30",
        "--sort",
        "most-liked",
        "-f",
        "json",
    ]]


def test_xiaohongshu_real_like_text_is_preserved_as_engagement():
    item = agent_reach._normalize(
        "xiaohongshu",
        {
            "id": "note-1",
            "title": "Excel模板",
            "url": "https://www.xiaohongshu.com/search_result/note-1",
            "author": "real-author",
            "likes": "6.7万",
            "published_at": "2026-09-20",
        },
    )

    assert item is not None
    assert item.engagement["likes"] == "6.7万"
    assert item.source_item_id == "note-1"
