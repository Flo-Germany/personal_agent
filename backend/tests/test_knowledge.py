"""Tests for the user-editable knowledge base."""

from __future__ import annotations

from pathlib import Path

from memory.knowledge import LEARNED_HEADING, KnowledgeBase


def section_body(path: Path, heading: str) -> str:
    text = path.read_text(encoding="utf-8")
    start = text.index(heading) + len(heading)
    rest = text[start:]
    next_heading = rest.find("\n## ")
    return rest if next_heading == -1 else rest[:next_heading]


def test_ensure_file_creates_template(tmp_path) -> None:
    path = tmp_path / "knowledge.md"
    base = KnowledgeBase(path)

    base.ensure_file()

    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "## Identity / name" in text
    assert "## Interests" in text
    assert "## Projects" in text
    assert "## Preferences" in text
    assert "## Habits and routines" in text
    assert "## Communication style" in text
    assert LEARNED_HEADING in text


def test_read_hides_editor_comments(tmp_path) -> None:
    base = KnowledgeBase(tmp_path / "knowledge.md")
    base.ensure_file()

    text = base.read()

    assert "<!--" not in text
    assert "## Interests" in text


def test_facts_are_written_to_matching_sections(tmp_path) -> None:
    path = tmp_path / "knowledge.md"
    base = KnowledgeBase(path)

    added = base.add_facts(
        {
            "identity": ["Name is Flo", "Is 32 years old"],
            "interests": ["Likes cycling"],
            "projects": ["Building a personal agent"],
            "preferences": ["Prefers short answers"],
            "habits": ["Wakes up at 5:30"],
            "communication": ["Prefers bullet points"],
        }
    )

    assert ("## Identity / name", "Name is Flo") in added
    assert "Name is Flo" in section_body(path, "## Identity / name")
    assert "Is 32 years old" in section_body(path, "## Identity / name")
    assert "Likes cycling" in section_body(path, "## Interests")
    assert "Building a personal agent" in section_body(path, "## Projects")
    assert "Prefers short answers" in section_body(path, "## Preferences")
    assert "Wakes up at 5:30" in section_body(path, "## Habits and routines")
    assert "Prefers bullet points" in section_body(path, "## Communication style")
    assert "- Likes cycling" not in section_body(path, LEARNED_HEADING)


def test_facts_accumulate_by_section(tmp_path) -> None:
    path = tmp_path / "knowledge.md"
    base = KnowledgeBase(path)

    base.add_facts({"interests": ["Likes cycling"]})
    base.add_facts({"interests": ["Bakes sourdough"]})

    body = section_body(path, "## Interests")
    assert "Likes cycling" in body
    assert "Bakes sourdough" in body


def test_added_facts_persist_and_deduplicate(tmp_path) -> None:
    path = tmp_path / "knowledge.md"
    base = KnowledgeBase(path)
    base.add_facts({"identity": ["Name is Flo"]})

    assert base.add_facts({"identity": ["Name is Flo."]}) == []
    assert base.add_facts({"interests": ["name is flo"]}) == []
    assert KnowledgeBase(path).read().count("Name is Flo") == 1


def test_added_facts_deduplicate_leading_articles(tmp_path) -> None:
    base = KnowledgeBase(tmp_path / "knowledge.md")

    assert base.add_facts({"identity": ["The user's name is Flo."]}) == [
        ("## Identity / name", "The user's name is Flo.")
    ]
    assert base.add_facts({"identity": ["User's name is Flo"]}) == []


def test_unknown_category_falls_back_to_learned_notes(tmp_path) -> None:
    path = tmp_path / "knowledge.md"
    base = KnowledgeBase(path)

    added = base.add_facts({"favourite teas": ["Likes oolong tea"]})

    assert added == [(LEARNED_HEADING, "Likes oolong tea")]
    assert "Likes oolong tea" in section_body(path, LEARNED_HEADING)


def test_category_heading_alias_is_accepted(tmp_path) -> None:
    path = tmp_path / "knowledge.md"
    base = KnowledgeBase(path)

    base.add_facts({"Communication Style": ["Prefers concise replies"]})

    assert "Prefers concise replies" in section_body(path, "## Communication style")


def test_new_name_replaces_old_name(tmp_path) -> None:
    path = tmp_path / "knowledge.md"
    base = KnowledgeBase(path)
    base.add_facts({"identity": ["Name is Alex"]})

    base.add_facts({"identity": ["Name is Flo"]})

    text = path.read_text(encoding="utf-8")
    assert "Name is Flo" in text
    assert "Name is Alex" not in text


def test_repeating_the_current_name_removes_conflicting_old_name(tmp_path) -> None:
    path = tmp_path / "knowledge.md"
    path.write_text(
        "# Notes\n\n## Identity / name\n- Name is Alex\n- Name is Flo\n",
        encoding="utf-8",
    )
    base = KnowledgeBase(path)

    added = base.add_facts({"identity": ["Name is Flo"]})

    text = path.read_text(encoding="utf-8")
    assert added == []
    assert "Name is Flo" in text
    assert "Name is Alex" not in text


def test_age_and_location_are_single_valued(tmp_path) -> None:
    path = tmp_path / "knowledge.md"
    base = KnowledgeBase(path)
    base.add_facts({"identity": ["Is 30 years old", "Lives in Berlin"]})

    base.add_facts({"identity": ["Is 31 years old", "Lives in Hamburg"]})

    text = path.read_text(encoding="utf-8")
    assert "Is 31 years old" in text
    assert "Lives in Hamburg" in text
    assert "Is 30 years old" not in text
    assert "Lives in Berlin" not in text


def test_reverse_name_phrasing_replaces_old_name(tmp_path) -> None:
    path = tmp_path / "knowledge.md"
    base = KnowledgeBase(path)
    base.add_facts({"identity": ["Alex is the user's name."]})

    base.add_facts({"identity": ["Flo is the user's name."]})

    text = path.read_text(encoding="utf-8")
    assert "Flo is the user's name." in text
    assert "Alex is the user's name." not in text


def test_verbose_age_and_location_are_single_valued(tmp_path) -> None:
    path = tmp_path / "knowledge.md"
    base = KnowledgeBase(path)
    base.add_facts(
        {
            "identity": [
                "The user is 30 years old.",
                "The user lives in Berlin.",
            ]
        }
    )

    base.add_facts(
        {
            "identity": [
                "The user is now 31 years old.",
                "The user lives in Hamburg.",
            ]
        }
    )

    text = path.read_text(encoding="utf-8")
    assert "The user is now 31 years old." in text
    assert "The user lives in Hamburg." in text
    assert "The user is 30 years old." not in text
    assert "The user lives in Berlin." not in text


def test_control_prefix_is_never_stored_as_a_fact(tmp_path) -> None:
    path = tmp_path / "knowledge.md"
    base = KnowledgeBase(path)

    added = base.add_facts({"identity": ["DELETE: Name is Alex"]})

    assert added == []
    assert "DELETE:" not in path.read_text(encoding="utf-8")


def test_explicit_deletion_removes_an_existing_fact(tmp_path) -> None:
    path = tmp_path / "knowledge.md"
    base = KnowledgeBase(path)
    base.add_facts({"interests": ["Listens to jazz"]})

    base.add_facts(
        {"preferences": ["Prefers classical music"]},
        deletions=["Listens to jazz"],
    )

    text = path.read_text(encoding="utf-8")
    assert "Listens to jazz" not in text
    assert "Prefers classical music" in text


def test_unmatched_deletion_keeps_existing_facts(tmp_path) -> None:
    path = tmp_path / "knowledge.md"
    base = KnowledgeBase(path)
    base.add_facts({"interests": ["Listens to jazz"]})

    base.add_facts({}, deletions=["Listens to reggae"])

    assert "Listens to jazz" in path.read_text(encoding="utf-8")


def test_add_facts_creates_missing_section(tmp_path) -> None:
    path = tmp_path / "knowledge.md"
    path.write_text("# My notes\n\nSomething I wrote.\n", encoding="utf-8")
    base = KnowledgeBase(path)

    base.add_facts({"interests": ["Likes cycling"]})

    text = path.read_text(encoding="utf-8")
    assert "Something I wrote." in text
    assert f"## Interests\n- Likes cycling" in text


def test_user_content_in_section_is_preserved(tmp_path) -> None:
    path = tmp_path / "knowledge.md"
    path.write_text(
        "# Notes\n\n## Interests\n\n- Manual entry\n\n## Learned notes\n",
        encoding="utf-8",
    )
    base = KnowledgeBase(path)

    base.add_facts({"interests": ["Learned entry"]})

    body = section_body(path, "## Interests")
    assert "- Manual entry" in body
    assert "- Learned entry" in body
    assert body.index("Manual entry") < body.index("Learned entry")


def test_invalid_facts_are_ignored(tmp_path) -> None:
    base = KnowledgeBase(tmp_path / "knowledge.md")

    added = base.add_facts(
        {"identity": ["", "-", "x" * 301]}
    )

    assert added == []
