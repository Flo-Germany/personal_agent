"""Tests for full-filesystem tools, protection rules, and HITL flags."""

from __future__ import annotations

import os
import zipfile
from pathlib import Path

import pytest

from tools import os_tools
from tools.os_tools import ToolError


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    path = tmp_path / "work"
    path.mkdir()
    return path


def test_requires_confirmation_flags() -> None:
    assert os_tools.requires_confirmation("run_powershell_query") is False
    assert os_tools.requires_confirmation("run_powershell_action") is True
    assert os_tools.requires_confirmation("create_folder") is True
    assert os_tools.requires_confirmation("create_file") is True
    assert os_tools.requires_confirmation("copy_file") is True
    assert os_tools.requires_confirmation("move_file_or_folder") is True
    assert os_tools.requires_confirmation("delete_file_or_folder") is True
    assert os_tools.requires_confirmation("unzip_file") is True
    assert os_tools.requires_confirmation("start_program") is True
    assert os_tools.requires_confirmation("open_file") is True
    assert os_tools.requires_confirmation("read_file_content") is False
    assert os_tools.requires_confirmation("search_system") is False
    assert os_tools.requires_confirmation("semantic_image_search") is False
    assert os_tools.requires_confirmation("get_open_windows") is False
    assert os_tools.requires_confirmation("read_window_content") is False
    assert os_tools.requires_confirmation("analyze_screen") is False
    assert os_tools.requires_confirmation("propose_plan") is False
    # Unknown tools fail closed.
    assert os_tools.requires_confirmation("unknown_tool") is True


def test_create_folder_outside_a_sandbox(workdir: Path) -> None:
    target = workdir / "nested" / "reports"

    result = os_tools.create_folder(str(target))

    assert result["ok"] is True
    assert target.is_dir()


def test_create_folder_refuses_protected_tree(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    protected = tmp_path / "Windows"
    protected.mkdir()
    monkeypatch.setattr(os_tools, "_protected_tree_roots", lambda: [protected])

    with pytest.raises(ToolError):
        os_tools.create_folder(str(protected / "new"))


def test_create_file_and_refuse_overwrite(workdir: Path) -> None:
    target = workdir / "hello.txt"

    result = os_tools.create_file(str(target), "hello")

    assert result["ok"] is True
    assert target.read_text(encoding="utf-8") == "hello"

    with pytest.raises(ToolError):
        os_tools.create_file(str(target), "replacement")

    overwritten = os_tools.create_file(
        str(target),
        "replacement",
        overwrite=True,
    )
    assert overwritten["ok"] is True
    assert target.read_text(encoding="utf-8") == "replacement"


def test_create_file_refuses_protected_tree(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    protected = tmp_path / "Windows"
    protected.mkdir()
    monkeypatch.setattr(os_tools, "_protected_tree_roots", lambda: [protected])

    with pytest.raises(ToolError):
        os_tools.create_file(str(protected / "evil.txt"), "x")


def test_create_file_size_limit(workdir: Path) -> None:
    with pytest.raises(ToolError):
        os_tools.create_file(
            str(workdir / "big.txt"),
            "x" * (os_tools._MAX_WRITE_BYTES + 1),
        )


def test_copy_file_and_refuse_overwrite(workdir: Path) -> None:
    source = workdir / "source.txt"
    source.write_text("hello", encoding="utf-8")
    destination = workdir / "backup" / "copy.txt"

    result = os_tools.copy_file(str(source), str(destination))

    assert result["ok"] is True
    assert destination.read_text(encoding="utf-8") == "hello"

    with pytest.raises(ToolError):
        os_tools.copy_file(str(source), str(destination))


def test_move_file_or_folder(workdir: Path) -> None:
    source = workdir / "old.txt"
    source.write_text("hello", encoding="utf-8")
    destination = workdir / "new" / "moved.txt"

    result = os_tools.move_file_or_folder(str(source), str(destination))

    assert result["ok"] is True
    assert not source.exists()
    assert destination.read_text(encoding="utf-8") == "hello"


def test_move_refuses_existing_destination(workdir: Path) -> None:
    source = workdir / "old.txt"
    source.write_text("hello", encoding="utf-8")
    destination = workdir / "existing.txt"
    destination.write_text("taken", encoding="utf-8")

    with pytest.raises(ToolError):
        os_tools.move_file_or_folder(str(source), str(destination))


def test_delete_uses_recycle_bin(
    monkeypatch: pytest.MonkeyPatch,
    workdir: Path,
) -> None:
    target = workdir / "trash-me.txt"
    target.write_text("bye", encoding="utf-8")
    recycled: list[str] = []

    def fake_send2trash(path: str) -> None:
        recycled.append(path)
        Path(path).unlink()

    monkeypatch.setattr(os_tools, "send2trash", fake_send2trash)

    result = os_tools.delete_file_or_folder(str(target))

    assert result["ok"] is True
    assert recycled == [str(target)]
    assert not target.exists()


def test_delete_refuses_protected_tree(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    protected = tmp_path / "Program Files"
    protected.mkdir()
    target = protected / "app.exe"
    target.write_bytes(b"")
    monkeypatch.setattr(os_tools, "_protected_tree_roots", lambda: [protected])
    monkeypatch.setattr(os_tools, "send2trash", lambda path: None)

    with pytest.raises(ToolError):
        os_tools.delete_file_or_folder(str(target))


def test_read_text_file(workdir: Path) -> None:
    target = workdir / "notes.txt"
    target.write_text("hello world", encoding="utf-8")

    result = os_tools.read_file_content(str(target))

    assert result["ok"] is True
    assert result["binary"] is False
    assert result["content"] == "hello world"


def test_read_binary_file_returns_metadata(workdir: Path) -> None:
    target = workdir / "pixel.png"
    target.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00")

    result = os_tools.read_file_content(str(target))

    assert result["ok"] is True
    assert result["binary"] is True
    assert result["is_image"] is True
    assert result["content"] is None


def test_read_file_size_limit(workdir: Path) -> None:
    target = workdir / "big.txt"
    target.write_text("hello", encoding="utf-8")

    with pytest.raises(ToolError):
        os_tools.read_file_content(str(target), max_bytes=3)


def test_read_file_redacts_obvious_secrets(workdir: Path) -> None:
    target = workdir / "config.txt"
    token = "sk-" + "a" * 24
    target.write_text(f"api_key = {token}\nname = Flo", encoding="utf-8")

    result = os_tools.read_file_content(str(target))

    assert result["ok"] is True
    assert token not in result["content"]
    assert "[REDACTED]" in result["content"]
    assert result["redacted"] == 1
    assert "Redacted 1 secret" in result["message"]


def test_read_file_can_disable_secret_redaction(workdir: Path) -> None:
    target = workdir / "config.txt"
    token = "sk-" + "b" * 24
    target.write_text(f"api_key = {token}", encoding="utf-8")

    result = os_tools.read_file_content(str(target), redact_secrets=False)

    assert token in result["content"]
    assert "redacted" not in result


def test_search_system_by_name_extension_and_metadata(workdir: Path) -> None:
    (workdir / "report.txt").write_text("x" * 10, encoding="utf-8")
    (workdir / "2026-report.pdf").write_text("x" * 100, encoding="utf-8")
    (workdir / "notes.md").write_text("x", encoding="utf-8")

    by_name = os_tools.search_system(str(workdir), name_contains="report")
    assert by_name["ok"] is True
    assert {item["name"] for item in by_name["results"]} == {
        "report.txt",
        "2026-report.pdf",
    }

    by_extension = os_tools.search_system(
        str(workdir),
        name_contains="report",
        extension="pdf",
    )
    assert [item["name"] for item in by_extension["results"]] == ["2026-report.pdf"]

    by_size = os_tools.search_system(str(workdir), min_size=50)
    assert [item["name"] for item in by_size["results"]] == ["2026-report.pdf"]

    future = os_tools.search_system(str(workdir), modified_after="2999-01-01")
    assert future["results"] == []


def test_search_system_hidden_and_result_caps(workdir: Path) -> None:
    (workdir / ".hidden.txt").write_text("x", encoding="utf-8")
    for index in range(3):
        (workdir / f"match-{index}.txt").write_text("x", encoding="utf-8")

    without_hidden = os_tools.search_system(str(workdir), name_contains="hidden")
    assert without_hidden["results"] == []

    with_hidden = os_tools.search_system(
        str(workdir),
        name_contains="hidden",
        include_hidden=True,
    )
    assert len(with_hidden["results"]) == 1

    capped = os_tools.search_system(str(workdir), name_contains="match", max_results=2)
    assert len(capped["results"]) == 2
    assert capped["limited"] is True


def test_search_system_returns_newest_first(workdir: Path) -> None:
    oldest = workdir / "oldest.txt"
    middle = workdir / "middle.txt"
    newest = workdir / "newest.txt"
    for path in (oldest, middle, newest):
        path.write_text("x", encoding="utf-8")

    os.utime(oldest, (1_000_000, 1_000_000))
    os.utime(middle, (1_500_000, 1_500_000))
    os.utime(newest, (2_000_000, 2_000_000))

    result = os_tools.search_system(
        str(workdir),
        sort_by="modified_desc",
        max_results=2,
    )

    assert [item["name"] for item in result["results"]] == [
        "newest.txt",
        "middle.txt",
    ]
    assert result["limited"] is True
    assert result["truncated"] is False


def test_search_system_ignores_zero_max_size(workdir: Path) -> None:
    (workdir / "file.txt").write_text("content", encoding="utf-8")

    result = os_tools.search_system(str(workdir), max_size=0)

    assert [item["name"] for item in result["results"]] == ["file.txt"]


def test_search_system_recursive_false_returns_direct_children(
    workdir: Path,
) -> None:
    (workdir / "top.txt").write_text("top", encoding="utf-8")
    nested = workdir / "nested"
    nested.mkdir()
    (nested / "deep.txt").write_text("deep", encoding="utf-8")

    result = os_tools.search_system(str(workdir), recursive=False)

    # Folders are searchable results too, but the nested file must not leak.
    assert [item["name"] for item in result["results"]] == ["nested", "top.txt"]
    assert [item["type"] for item in result["results"]] == ["folder", "file"]
    assert result["recursive"] is False


def test_search_system_finds_folders_files_or_both(workdir: Path) -> None:
    archive = workdir / "Archive 2026"
    archive.mkdir()
    (archive / "memo.txt").write_text("x", encoding="utf-8")
    (workdir / "archive.txt").write_text("x", encoding="utf-8")

    folders = os_tools.search_system(
        str(workdir),
        name_contains="archive",
        kind="folders",
    )
    assert [item["name"] for item in folders["results"]] == ["Archive 2026"]
    folder = folders["results"][0]
    assert folder["type"] == "folder"
    assert folder["relative_path"] == "Archive 2026"
    assert folder["depth"] == 1
    assert folder["size"] is None
    assert folder["has_children"] is True
    assert folder["children_count"] == 1
    assert folders["summary"]["folders"] == 1
    assert folders["summary"]["files"] == 0

    files = os_tools.search_system(
        str(workdir),
        name_contains="archive",
        kind="files",
    )
    assert [item["name"] for item in files["results"]] == ["archive.txt"]

    both = os_tools.search_system(str(workdir), name_contains="archive")
    assert sorted(item["name"] for item in both["results"]) == [
        "Archive 2026",
        "archive.txt",
    ]
    assert both["summary"]["folders"] == 1
    assert both["summary"]["files"] == 1


def test_search_system_rejects_impossible_folder_filters(workdir: Path) -> None:
    with pytest.raises(ToolError):
        os_tools.search_system(str(workdir), kind="folders", extension="pdf")
    with pytest.raises(ToolError):
        os_tools.search_system(str(workdir), kind="folders", min_size=10)
    with pytest.raises(ToolError):
        os_tools.search_system(str(workdir), kind="sockets")


def test_search_system_extension_filter_implies_files_only(workdir: Path) -> None:
    (workdir / "report.pdf").write_text("x", encoding="utf-8")
    (workdir / "report").mkdir()

    result = os_tools.search_system(
        str(workdir),
        name_contains="report",
        extension="pdf",
    )

    assert result["kind"] == "files"
    assert [item["name"] for item in result["results"]] == ["report.pdf"]


def test_search_system_builds_a_tree_with_context_folders(workdir: Path) -> None:
    deep = workdir / "Projects" / "2026"
    deep.mkdir(parents=True)
    (deep / "report.pdf").write_text("x", encoding="utf-8")
    (workdir / "Projects" / "notes.txt").write_text("x", encoding="utf-8")

    result = os_tools.search_system(
        str(workdir),
        name_contains="report",
    )

    tree = result["tree"]
    assert len(tree) == 1
    root = tree[0]
    assert root["path"] == str(workdir)
    assert root["is_root"] is True
    assert root["matched"] is False

    projects = next(child for child in root["children"] if child["name"] == "Projects")
    assert projects["type"] == "folder"
    assert projects["matched"] is False
    assert projects["path"].endswith("Projects")

    year = projects["children"][0]
    assert year["name"] == "2026"
    assert year["matched"] is False

    leaf = year["children"][0]
    assert leaf["name"] == "report.pdf"
    assert leaf["type"] == "file"
    assert leaf["matched"] is True
    assert leaf["path"] == str(deep / "report.pdf")
    assert leaf["extension"] == ".pdf"
    assert leaf["size"] == 1


def test_search_system_max_depth_limits_results(workdir: Path) -> None:
    deep = workdir / "a" / "b" / "c"
    deep.mkdir(parents=True)
    (deep / "target.txt").write_text("x", encoding="utf-8")

    shallow = os_tools.search_system(
        str(workdir),
        name_contains="target",
        max_depth=2,
    )
    assert shallow["results"] == []
    assert shallow["max_depth"] == 2

    matched = os_tools.search_system(
        str(workdir),
        name_contains="target",
        max_depth=4,
    )
    assert [item["name"] for item in matched["results"]] == ["target.txt"]
    assert matched["results"][0]["depth"] == 4


def test_search_system_skips_dependency_folders_but_still_finds_them(
    workdir: Path,
) -> None:
    modules = workdir / "node_modules" / "left-pad"
    modules.mkdir(parents=True)
    (modules / "index.js").write_text("x", encoding="utf-8")
    (workdir / "app.js").write_text("x", encoding="utf-8")

    by_name = os_tools.search_system(
        str(workdir),
        name_contains="node_modules",
        kind="folders",
    )
    assert [item["name"] for item in by_name["results"]] == ["node_modules"]

    inner = os_tools.search_system(str(workdir), name_contains="index.js")
    assert inner["results"] == []

    with_junk = os_tools.search_system(
        str(workdir),
        name_contains="index.js",
        include_junk=True,
    )
    assert [item["name"] for item in with_junk["results"]] == ["index.js"]
    assert with_junk["query"]["include_junk"] is True


def test_search_system_folder_counts_stay_cheap(workdir: Path, monkeypatch) -> None:
    busy = workdir / "busy"
    busy.mkdir()
    for index in range(5):
        (busy / f"item-{index}.txt").write_text("x", encoding="utf-8")

    monkeypatch.setattr(os_tools, "_MAX_FOLDER_SAMPLE", 2)
    result = os_tools.search_system(str(workdir), name_contains="busy")

    folder = result["results"][0]
    assert folder["children_count"] == 2
    assert folder["has_children"] is True


def test_documents_alias_maps_to_known_folder(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    known = tmp_path / "OneDrive" / "Dokumente"
    known.mkdir(parents=True)
    monkeypatch.setattr(
        os_tools,
        "_known_folder",
        lambda alias: known if alias in {"documents", "dokumente"} else None,
    )

    assert os_tools._absolute_path(str(Path.home() / "Documents")) == known
    assert (
        os_tools._absolute_path(str(Path.home() / "Documents" / "note.txt"))
        == known / "note.txt"
    )


def test_search_system_rejects_missing_root(workdir: Path) -> None:
    with pytest.raises(ToolError):
        os_tools.search_system(str(workdir / "missing"))


def test_unzip_file_extracts(workdir: Path) -> None:
    archive = workdir / "bundle.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("docs/readme.txt", "hello")

    result = os_tools.unzip_file(str(archive), str(workdir / "out"))

    assert result["ok"] is True
    assert (workdir / "out" / "docs" / "readme.txt").read_text(
        encoding="utf-8"
    ) == "hello"


def test_unzip_rejects_zip_slip(workdir: Path, tmp_path: Path) -> None:
    archive = workdir / "evil.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("../escaped.txt", "escaped")

    with pytest.raises(ToolError):
        os_tools.unzip_file(str(archive), str(workdir / "out"))

    assert not (workdir / "escaped.txt").exists()
    assert not (tmp_path / "escaped.txt").exists()


def test_unzip_size_limit(workdir: Path) -> None:
    archive = workdir / "big.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("big.txt", "x" * 100)

    with pytest.raises(ToolError):
        os_tools.unzip_file(str(archive), str(workdir / "out"), max_bytes=10)


def test_start_program_resolves_known_alias(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    system32 = tmp_path / "System32"
    system32.mkdir()
    (system32 / "notepad.exe").write_bytes(b"")
    monkeypatch.setattr(os_tools, "_system32", lambda: system32)

    launches: list[tuple[list[str], dict[str, object]]] = []

    def fake_popen(args: list[str], **kwargs: object) -> None:
        launches.append((args, kwargs))

    monkeypatch.setattr(os_tools.subprocess, "Popen", fake_popen)

    result = os_tools.start_program("notepad")

    assert result["ok"] is True
    assert launches == [([str(system32 / "notepad.exe")], {"shell": False})]


def test_start_program_accepts_any_absolute_executable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    program = tmp_path / "custom app.exe"
    program.write_bytes(b"")
    launches: list[list[str]] = []

    monkeypatch.setattr(
        os_tools.subprocess,
        "Popen",
        lambda args, **kwargs: launches.append(args),
    )

    result = os_tools.start_program(str(program))

    assert result["ok"] is True
    assert result["program"] == str(program.resolve())
    assert launches == [[str(program.resolve())]]


def test_start_program_rejects_unknown_program(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os_tools.shutil, "which", lambda _name: None)
    monkeypatch.setattr(os_tools, "_registry_program_path", lambda _name: None)
    monkeypatch.setattr(os_tools, "_start_menu_shortcut", lambda _name: None)

    with pytest.raises(ToolError):
        os_tools.start_program("definitely-not-an-installed-program-xyz")


def test_open_file_requires_an_explicit_program(tmp_path: Path) -> None:
    document = tmp_path / "notes.txt"
    document.write_text("hello", encoding="utf-8")

    with pytest.raises(ToolError) as error:
        os_tools.open_file(str(document), "")

    assert "Ask the user" in str(error.value)


def test_open_file_launches_selected_editor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    document = tmp_path / "notes.txt"
    document.write_text("hello", encoding="utf-8")
    editor = tmp_path / "editor.exe"
    editor.write_bytes(b"")
    launches: list[list[str]] = []

    monkeypatch.setattr(
        os_tools.subprocess,
        "Popen",
        lambda args, **kwargs: launches.append(args),
    )

    result = os_tools.open_file(str(document), str(editor))

    assert result["ok"] is True
    assert result["path"] == str(document)
    assert result["program"] == str(editor.resolve())
    assert launches == [[str(editor.resolve()), str(document)]]


def test_open_file_rejects_executables(tmp_path: Path) -> None:
    executable = tmp_path / "notepad.exe"
    executable.write_bytes(b"MZ")

    with pytest.raises(os_tools.ToolError, match="start_program"):
        os_tools.open_file(str(executable), "notepad")


def test_open_file_rejects_missing_file(tmp_path: Path) -> None:
    editor = tmp_path / "editor.exe"
    editor.write_bytes(b"")

    with pytest.raises(ToolError):
        os_tools.open_file(str(tmp_path / "missing.txt"), str(editor))


def test_path_resolution_expands_relative_paths_to_home() -> None:
    path = os_tools._absolute_path("some-relative-file.txt")

    assert path.is_absolute()
    assert path.parent == Path(os.path.abspath(Path.home()))
