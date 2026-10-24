"""Tests for the native PowerShell query/action tools."""

from __future__ import annotations

from pathlib import Path

from tools.system_tools import run_powershell_action, run_powershell_query


def test_query_runs_read_only_command() -> None:
    result = run_powershell_query(
        "Get-Process -Id $PID | Select-Object Id, ProcessName, WorkingSet64"
    )

    assert result["ok"] is True
    assert result["mode"] == "query"
    assert result["exit_code"] == 0
    assert "powershell" in result["stdout"].casefold()
    assert result["timeout"] is False


def test_query_rejects_state_changing_commands() -> None:
    commands = (
        "Stop-Process -Name notepad",
        "Remove-Item C:\\temp\\x -Force",
        "Set-ItemProperty HKCU:\\Software\\X -Name Y -Value Z",
        "Get-Process | ForEach-Object { $_.Kill() }",
        "taskkill /IM notepad.exe /F",
        "Get-Content x > C:\\temp\\out.txt",
        "iex 'Get-Process'",
    )

    for command in commands:
        result = run_powershell_query(command)
        assert result["ok"] is False, command
        assert "run_powershell_action" in result["error"], command


def test_query_truncates_large_output() -> None:
    result = run_powershell_query("'-' * 20000", max_output_chars=1000)

    assert result["ok"] is True
    assert result["truncated"] is True
    assert result["stdout_truncated"] is True
    assert "[truncated" in result["stdout"]
    assert len(result["stdout"]) < 20000


def test_query_timeout_kills_the_process(tmp_path: Path) -> None:
    target = tmp_path / "wait.txt"
    target.write_text("first line\n", encoding="utf-8")

    result = run_powershell_query(
        f"Get-Content -LiteralPath '{target}' -Wait",
        timeout=0.5,
    )

    assert result["ok"] is False
    assert result["timeout"] is True
    assert "timed out" in result["message"]


def test_action_failure_includes_exit_code_and_stderr() -> None:
    result = run_powershell_action(
        "Stop-Process -Id 999999 -Force",
        description="Stop a stale process ID.",
    )

    assert result["ok"] is False
    assert result["exit_code"] == 1
    assert "exit code 1" in result["message"]
    assert "Cannot find a process" in result["stderr"]
    assert "stale or wrong" in result["hint"]


def test_action_runs_without_the_read_only_guard() -> None:
    result = run_powershell_action(
        "Write-Output 'action-ok'",
        description="Print a test message.",
    )

    assert result["ok"] is True
    assert result["mode"] == "action"
    assert "action-ok" in result["stdout"]


def test_action_requires_a_description() -> None:
    result = run_powershell_action("Write-Output 'should-not-run'")

    assert result["ok"] is False
    assert "description" in result["error"]


def test_empty_command_is_rejected() -> None:
    assert run_powershell_query("   ")["ok"] is False
    assert run_powershell_action("")["ok"] is False
