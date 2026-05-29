"""Tests for buddy.hook decision protocol."""

import json
import sys
from io import StringIO

import pytest

import buddy.hook as hook

PRE_TOOL_PAYLOAD = {
    "hook_event_name": "PreToolUse",
    "permission_mode": "default",
    "tool_name": "Bash",
    "tool_input": {"command": "rm -rf build"},
}

BYPASS_PAYLOAD = {
    **PRE_TOOL_PAYLOAD,
    "permission_mode": "bypassPermissions",
}

STOP_PAYLOAD = {"hook_event_name": "Stop"}

POST_TOOL_PAYLOAD = {
    "hook_event_name": "PostToolUse",
    "tool_name": "Bash",
    "tool_input": {"command": "ls"},
}


def _run(event, stdin_obj, *, talk_return, approval_timeout=30.0, monkeypatch, capsys):
    """Invoke hook.main() with _talk mocked; return (stdout, exit_code)."""
    monkeypatch.setattr(hook, "APPROVAL_TIMEOUT", approval_timeout)
    monkeypatch.setattr(hook, "_talk", lambda *a, **kw: talk_return)
    monkeypatch.setattr(sys, "argv", ["buddy-hook", event])
    monkeypatch.setattr(sys, "stdin", StringIO(json.dumps(stdin_obj)))
    with pytest.raises(SystemExit) as exc_info:
        hook.main()
    out = capsys.readouterr().out
    return out, exc_info.value.code if exc_info.value.code is not None else 0


# ── pre-tool: button B (deny) ────────────────────────────────────────────────


def test_deny_emits_deny_json(monkeypatch, capsys):
    out, code = _run(
        "pre-tool",
        PRE_TOOL_PAYLOAD,
        talk_return={"decision": "deny"},
        monkeypatch=monkeypatch,
        capsys=capsys,
    )
    assert code == 0
    obj = json.loads(out)
    hs = obj["hookSpecificOutput"]
    assert hs["hookEventName"] == "PreToolUse"
    assert hs["permissionDecision"] == "deny"
    assert "permissionDecisionReason" in hs


def test_deny_stdout_is_valid_json_only(monkeypatch, capsys):
    """stdout must contain exactly one JSON object — no stray text."""
    out, _ = _run(
        "pre-tool",
        PRE_TOOL_PAYLOAD,
        talk_return={"decision": "deny"},
        monkeypatch=monkeypatch,
        capsys=capsys,
    )
    assert out.strip() != ""
    json.loads(out)  # must not raise


# ── pre-tool: button A (allow) ───────────────────────────────────────────────


def test_allow_emits_allow_json(monkeypatch, capsys):
    out, code = _run(
        "pre-tool",
        PRE_TOOL_PAYLOAD,
        talk_return={"decision": "once"},
        monkeypatch=monkeypatch,
        capsys=capsys,
    )
    assert code == 0
    hs = json.loads(out)["hookSpecificOutput"]
    assert hs["permissionDecision"] == "allow"


# ── pre-tool: timeout (BUDDY_TIMEOUT > 0, bridge returns "once") ─────────────


def test_timeout_emits_allow_json(monkeypatch, capsys):
    out, code = _run(
        "pre-tool",
        PRE_TOOL_PAYLOAD,
        talk_return={"decision": "once"},
        approval_timeout=5.0,
        monkeypatch=monkeypatch,
        capsys=capsys,
    )
    assert code == 0
    hs = json.loads(out)["hookSpecificOutput"]
    assert hs["permissionDecision"] == "allow"


# ── pre-tool: bridge unreachable ─────────────────────────────────────────────


def test_unreachable_is_silent_exit_0(monkeypatch, capsys):
    out, code = _run(
        "pre-tool",
        PRE_TOOL_PAYLOAD,
        talk_return=None,
        monkeypatch=monkeypatch,
        capsys=capsys,
    )
    assert code == 0
    assert out.strip() == ""  # no decision JSON → defer to Claude's own flow


# ── pre-tool: BUDDY_TIMEOUT=0 (display-only) ─────────────────────────────────


def test_display_only_is_silent_exit_0(monkeypatch, capsys):
    out, code = _run(
        "pre-tool",
        PRE_TOOL_PAYLOAD,
        talk_return={"decision": "once"},
        approval_timeout=0.0,
        monkeypatch=monkeypatch,
        capsys=capsys,
    )
    assert code == 0
    assert out.strip() == ""  # observe-only, no decision emitted


def test_display_only_deny_still_silent(monkeypatch, capsys):
    """With BUDDY_TIMEOUT=0 even a deny response is ignored (display-only)."""
    out, code = _run(
        "pre-tool",
        PRE_TOOL_PAYLOAD,
        talk_return={"decision": "deny"},
        approval_timeout=0.0,
        monkeypatch=monkeypatch,
        capsys=capsys,
    )
    assert code == 0
    assert out.strip() == ""


# ── pre-tool: bypassPermissions fast path ────────────────────────────────────


def test_bypass_permissions_is_silent_exit_0(monkeypatch, capsys):
    out, code = _run(
        "pre-tool",
        BYPASS_PAYLOAD,
        talk_return={"decision": "deny"},  # would deny if consulted
        monkeypatch=monkeypatch,
        capsys=capsys,
    )
    assert code == 0
    assert out.strip() == ""


def test_bypass_permissions_skips_device(monkeypatch, capsys):
    """_talk must not be called when bypassPermissions is set."""
    calls: list = []

    def _talk_spy(*a, **kw):
        calls.append(a)
        return {"decision": "once"}

    monkeypatch.setattr(hook, "APPROVAL_TIMEOUT", 30.0)
    monkeypatch.setattr(hook, "_talk", _talk_spy)
    monkeypatch.setattr(sys, "argv", ["buddy-hook", "pre-tool"])
    monkeypatch.setattr(sys, "stdin", StringIO(json.dumps(BYPASS_PAYLOAD)))

    with pytest.raises(SystemExit):
        hook.main()

    assert calls == []


# ── post-tool / stop: always silent exit 0 ───────────────────────────────────


def test_post_tool_is_silent_exit_0(monkeypatch, capsys):
    out, code = _run(
        "post-tool",
        POST_TOOL_PAYLOAD,
        talk_return=None,
        monkeypatch=monkeypatch,
        capsys=capsys,
    )
    assert code == 0
    assert out.strip() == ""


def test_stop_never_blocks(monkeypatch, capsys):
    out, code = _run(
        "stop", STOP_PAYLOAD, talk_return=None, monkeypatch=monkeypatch, capsys=capsys
    )
    assert code == 0
    assert out.strip() == ""


# ── fail-open: internal error must not block ─────────────────────────────────


def test_exception_in_talk_is_silent_exit_0(monkeypatch, capsys):
    """Any unhandled exception must result in exit 0, not exit 1."""

    def _talk_boom(*a, **kw):
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(hook, "APPROVAL_TIMEOUT", 30.0)
    monkeypatch.setattr(hook, "_talk", _talk_boom)
    monkeypatch.setattr(sys, "argv", ["buddy-hook", "pre-tool"])
    monkeypatch.setattr(sys, "stdin", StringIO(json.dumps(PRE_TOOL_PAYLOAD)))

    with pytest.raises(SystemExit) as exc_info:
        hook.main()

    out = capsys.readouterr().out
    assert exc_info.value.code == 0
    assert out.strip() == ""
