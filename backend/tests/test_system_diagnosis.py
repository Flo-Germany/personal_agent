"""Tests for the read-only system diagnosis and chart generation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from settings import ToolSettings
from tools import system_diagnosis
from tools.executor import ToolExecutor


def sample_data() -> dict[str, Any]:
    return {
        "cpu": {
            "per_core_percent": [12.0, 25.0, 5.0, 40.0],
            "average_percent": 20.5,
        },
        "timeline": [
            {"seconds": 0.5, "cpu": 20.0, "memory": 50.0},
            {"seconds": 1.0, "cpu": 30.0, "memory": 52.0},
        ],
        "memory": {
            "total_gb": 64.0,
            "used_gb": 38.4,
            "available_gb": 25.6,
            "percent": 60.0,
        },
        "top_processes": {
            "by_memory": [
                {
                    "pid": 10,
                    "name": "chrome",
                    "memory_mb": 2048.0,
                    "cpu_percent": 3.0,
                    "cpu_system_percent": 0.2,
                }
            ],
            "by_cpu": [
                {
                    "pid": 11,
                    "name": "compiler",
                    "memory_mb": 512.0,
                    "cpu_percent": 45.0,
                    "cpu_system_percent": 2.8,
                }
            ],
        },
        "volumes": [
            {
                "device": "C:\\",
                "mountpoint": "C:\\",
                "total_gb": 1000.0,
                "used_gb": 600.0,
                "free_gb": 400.0,
                "percent": 60.0,
            }
        ],
        "benchmarks": {
            "cpu_single_thread_mbps": 500.0,
            "cpu_multi_thread_mbps": 1800.0,
            "memory_copy_gbps": 20.0,
        },
        "gpu_processes": [
            {"pid": 10, "name": "chrome", "utilization": 12.5}
        ],
    }


def test_charts_are_created(tmp_path: Path) -> None:
    charts = system_diagnosis._make_charts(sample_data(), tmp_path)

    assert len(charts) == 6
    for chart in charts:
        path = Path(chart["path"])
        assert path.is_file()
        assert path.suffix == ".png"
        assert path.stat().st_size > 1000
        assert chart["title"]
        assert chart["description"]


def test_findings_flag_critical_resources_and_health() -> None:
    data = sample_data()
    data["memory"]["percent"] = 95.0
    data["volumes"][0]["percent"] = 96.0
    data.update(
        {
            "physical_disks": [
                {
                    "FriendlyName": "Disk 0",
                    "HealthStatus": "Warning",
                    "OperationalStatus": "OK",
                }
            ],
            "defender": {
                "antivirus_enabled": False,
                "realtime_enabled": False,
            },
            "services": {
                "automatic_stopped": [
                    {"Name": f"Service{index}"} for index in range(6)
                ]
            },
            "recent_events": [
                {"Id": index, "LevelDisplayName": "Error"}
                for index in range(12)
            ],
            "pending_reboot": True,
            "uptime_hours": 24 * 40,
        }
    )

    findings = system_diagnosis._build_findings(data)
    titles = {finding["title"] for finding in findings}

    assert "Memory pressure is very high" in titles
    assert any("nearly full" in title for title in titles)
    assert any("reports Warning" in title for title in titles)
    assert any("Defender" in title for title in titles)
    assert system_diagnosis._health_score(findings) < 50


def test_busy_gpu_is_reported_as_a_slowdown_cause() -> None:
    data = sample_data()
    data["gpu_processes"] = [
        {"name": "SomeGame.exe", "utilization": 97.4},
        {"name": "llama-server.exe", "utilization": 80.0},
    ]

    findings = system_diagnosis._build_findings(data)

    gpu = [item for item in findings if "GPU is busy" in item["title"]]
    assert gpu
    assert gpu[0]["severity"] == "info"
    assert "SomeGame.exe" in gpu[0]["detail"]
    assert "97%" in gpu[0]["detail"]


def test_enabled_firewall_flags_are_not_flagged() -> None:
    data = sample_data()
    data["firewall"] = [
        {"Name": "Domain", "Enabled": 1},
        {"Name": "Private", "Enabled": True},
        {"Name": "Public", "Enabled": "True"},
    ]

    findings = system_diagnosis._build_findings(data)

    assert not [item for item in findings if "Firewall" in item["title"]]


def test_disabled_firewall_is_a_critical_finding() -> None:
    data = sample_data()
    data["firewall"] = [
        {"Name": "Domain", "Enabled": 0},
        {"Name": "Private", "Enabled": False},
        {"Name": "Public", "Enabled": "False"},
    ]

    findings = system_diagnosis._build_findings(data)

    firewall = [item for item in findings if "Firewall" in item["title"]]
    assert firewall
    assert firewall[0]["severity"] == "critical"
    assert "0 of 3" in firewall[0]["detail"]
    assert system_diagnosis._health_score(findings) <= 80


def test_sample_seconds_are_clamped() -> None:
    assert system_diagnosis._clamp_sample_seconds(1) == 2.0
    assert system_diagnosis._clamp_sample_seconds(99) == 10.0
    assert system_diagnosis._clamp_sample_seconds("bad") == 4.0


def test_run_diagnosis_writes_a_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        system_diagnosis,
        "_run_powershell_json",
        lambda script, timeout=45: {
            "os": {"caption": "Windows 11"},
            "computer": {"manufacturer": "Test"},
            "physical_disks": [],
            "services": {"total": 10, "running": 10, "stopped": 0},
            "defender": {
                "antivirus_enabled": True,
                "realtime_enabled": True,
            },
            # PowerShell may serialize these flags as integers.
            "firewall": [
                {"Name": "Domain", "Enabled": 1},
                {"Name": "Private", "Enabled": 1},
                {"Name": "Public", "Enabled": 0},
            ],
            "reliability": [
                {
                    "disk": "Test SSD",
                    "temperature": 41,
                    "wear": 3,
                    "power_on_hours": 1200,
                    "read_errors": 0,
                    "write_errors": 0,
                }
            ],
            "events": [],
            "pending_reboot": False,
        },
    )
    live = sample_data()
    live.update(
        {
            "network_rate": {"sent_mbps": 1.0, "received_mbps": 2.0},
            "disk_rate": {"read_mbps": 3.0, "write_mbps": 4.0},
            "boot_time": "2026-10-25T10:00:00",
            "uptime_hours": 12.0,
            "users": [],
        }
    )
    monkeypatch.setattr(
        system_diagnosis,
        "_collect_live",
        lambda seconds: live,
    )

    result = system_diagnosis.run_diagnosis(
        sample_seconds=2,
        include_benchmark=False,
        output_dir=tmp_path,
    )

    assert result["ok"] is True
    assert result["report_dir"] == str(tmp_path)
    assert result["files"]
    assert all(Path(path).is_file() for path in result["files"])
    assert "Health score" in result["message"]
    assert result["summary"]["os"]["caption"] == "Windows 11"
    # The chat draws its charts from these compact numeric series.
    assert result["summary"]["per_core_percent"] == [12.0, 25.0, 5.0, 40.0]
    assert result["summary"]["timeline"] == [
        {"seconds": 0.5, "cpu": 20.0, "memory": 50.0},
        {"seconds": 1.0, "cpu": 30.0, "memory": 52.0},
    ]
    assert result["summary"]["security"]["firewall_state"] == "partial"
    assert result["summary"]["security"]["firewall_enabled"] == 2
    assert result["summary"]["smart_available"] is True
    assert result["summary"]["smart"][0]["temperature_c"] == 41
    assert result["summary"]["smart"][0]["wear_percent"] == 3
    assert any(
        "Firewall" in finding["title"] for finding in result["findings"]
    )


def test_executor_dispatches_system_diagnosis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_run(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"ok": True, "message": "diagnosed"}

    monkeypatch.setattr(
        "tools.executor.system_diagnosis.run_diagnosis",
        fake_run,
    )
    executor = ToolExecutor(ToolSettings())

    result = executor.execute(
        "system_diagnosis",
        {"sample_seconds": 3, "include_benchmark": False},
    )

    assert result["ok"] is True
    assert calls == [
        {"sample_seconds": 3, "include_benchmark": False}
    ]
