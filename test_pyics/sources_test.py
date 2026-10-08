"""
Course libraries (sources): where they are configured, adding and removing them, their
indexes, and finding courses in them.
"""

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from pyics.distribution import archive, sources
from pyics.distribution.sources import Source, SourceError

SHA = "0" * 64


def _index(
    path: Path, courses: dict[str, dict] | None = None, name: str = "Demo library"
) -> Path:
    """Write an index of ``courses`` (slug: title) to ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    courses = {"adding": {"title": "Adding"}} if courses is None else courses
    data = {
        "pyics_index": 1,
        "name": name,
        "courses": {
            slug: {"url": f"{slug}.zip", "sha256": SHA, **entry}
            for slug, entry in courses.items()
        },
    }
    path.write_text(json.dumps(data))
    return path


# --- configuration ---


def test_config_dir_can_be_overridden(isolated_config: Path) -> None:
    assert sources.config_dir() == isolated_config
    assert sources.cache_dir() == isolated_config / "cache"
    assert sources.site_config_dirs() == []


def test_config_dir_defaults_to_the_platforms(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(sources.CONFIG_DIR_ENV)
    assert sources.config_dir().name == "pyics"
    assert sources.site_config_dirs()


def test_sites_sources_come_after_the_users(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source_adder: Callable[..., Source]
) -> None:
    mine = source_adder(_index(tmp_path / "mine/index.json"), "lab")
    site = tmp_path / "etc"
    site.mkdir()
    (site / "sources.json").write_text(
        json.dumps(
            {
                "version": 1,
                "sources": [
                    {"name": "lab", "url": "https://example.org/other.json"},
                    {"name": "uni", "url": "https://example.org/index.json"},
                ],
            }
        )
    )
    monkeypatch.setattr(sources, "site_config_dirs", lambda: [site])
    assert sources.load_sources() == [
        mine,  # rather than the site's source of the same name
        Source("uni", "https://example.org/index.json", site=site / "sources.json"),
    ]
    with pytest.raises(SourceError, match="for all users"):
        sources.remove_source("uni")


def test_unreadable_sources_are_reported(isolated_config: Path) -> None:
    (isolated_config / "sources.json").write_text("{")
    with pytest.raises(SourceError, match="can't be read"):
        sources.load_sources()


# --- adding and removing ---


def test_add_and_remove_a_source(tmp_path: Path) -> None:
    index = _index(tmp_path / "library/index.json")
    source, parsed, data = sources.prepare_source(str(index))
    assert source == Source("demo-library", index.as_uri())
    assert list(parsed.courses) == ["adding"]
    assert sources.load_sources() == []  # not stored yet

    sources.save_source(source, data)
    assert sources.load_sources() == [source]
    assert sources.cached_index(source) == parsed

    sources.remove_source("demo-library")
    assert sources.load_sources() == []
    assert sources.cached_index(source) is None
    with pytest.raises(SourceError, match="no source named"):
        sources.remove_source("demo-library")


def test_sources_are_named(tmp_path: Path, source_adder: Callable[..., Source]) -> None:
    index = _index(tmp_path / "a/index.json")
    with pytest.raises(SourceError, match="not a valid source name"):
        sources.prepare_source(str(index), "Not/valid")
    source_adder(index, "a")
    with pytest.raises(SourceError, match="is added already, as 'a'"):
        sources.prepare_source(str(index), "b")
    with pytest.raises(SourceError, match="named 'a' already"):
        sources.prepare_source(str(_index(tmp_path / "b/index.json")), "a")


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("http://example.org/index.json", "isn't secure"),
        ("ftp://example.org/index.json", "only https URLs"),
    ],
)
def test_only_secure_sources_are_added(url: str, message: str) -> None:
    with pytest.raises(SourceError, match=message):
        sources.prepare_source(url)


def test_missing_indexes_are_reported(tmp_path: Path) -> None:
    with pytest.raises(SourceError, match="Can't fetch"):
        sources.prepare_source(str(tmp_path / "missing.json"))
    (tmp_path / "other.json").write_text('{"a": 1}')
    with pytest.raises(SourceError, match="isn't a pyics index"):
        sources.prepare_source(str(tmp_path / "other.json"))


# --- indexes ---


def test_parse_index() -> None:
    data = json.loads(_index_text({"adding": {"title": "Adding", "requires": ["x"]}}))
    index = sources.parse_index(data, "https://example.org/lib/index.json")
    entry = index.courses["adding"]
    assert entry.url == "https://example.org/lib/adding.zip"
    assert (entry.title, entry.requires, entry.version) == ("Adding", ["x"], "")


def _index_text(courses: dict[str, dict]) -> str:
    entries = {
        slug: {"url": f"{slug}.zip", "sha256": SHA, **entry}
        for slug, entry in courses.items()
    }
    return json.dumps({"pyics_index": 1, "name": "x", "courses": entries})


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"pyics_index": 2, "courses": {}}, "needs a newer pyics"),
        ({"pyics_index": 1}, "has no courses"),
        ({"pyics_index": 1, "courses": {"Bad Slug": {}}}, "not a valid course slug"),
        ({"pyics_index": 1, "courses": {"a": {"url": "a.zip"}}}, "no sha256"),
        ({"pyics_index": 1, "courses": {"a": {"sha256": SHA}}}, "no url"),
        (
            {
                "pyics_index": 1,
                "courses": {"a": {"url": "a", "sha256": SHA, "tags": "x"}},
            },
            "tags must be a list",
        ),
    ],
)
def test_invalid_indexes_are_refused(data: dict, message: str) -> None:
    with pytest.raises(SourceError, match=message):
        sources.parse_index(data, "https://example.org/index.json")


def test_unreachable_indexes_fall_back_to_the_cache(
    tmp_path: Path, source_adder: Callable[..., Source]
) -> None:
    index = _index(tmp_path / "index.json")
    source = source_adder(index)
    index.unlink()
    (result,) = sources.load_indexes()
    assert result.cached and result.index is not None
    assert result.error is not None and "Can't fetch" in result.error

    courses, warnings = sources.search()
    assert [entry.slug for _, entry in courses] == ["adding"]
    assert warnings == [f"{source.name}: {result.error} (using its cached index)"]


def test_cached_courses_need_no_connection(
    tmp_path: Path, source_adder: Callable[..., Source]
) -> None:
    index = _index(tmp_path / "index.json")
    source_adder(index)
    _index(index, {"subtracting": {}})  # not fetched again
    assert [entry.slug for _, entry in sources.cached_courses()] == ["adding"]


# --- finding courses ---


@pytest.fixture
def two_sources(tmp_path: Path, source_adder: Callable[..., Source]) -> None:
    source_adder(_index(tmp_path / "a/index.json", {"adding": {}, "graphs": {}}), "a")
    source_adder(
        _index(
            tmp_path / "b/index.json",
            {"graphs": {"description": "BFS and DFS", "tags": ["search"]}},
        ),
        "b",
    )


def test_resolve(two_sources: None) -> None:
    resolved = sources.resolve("adding")
    assert (resolved.source.name, resolved.entry.slug) == ("a", "adding")
    assert sources.resolve("b/graphs").source.name == "b"


def test_resolve_ambiguous_courses(two_sources: None) -> None:
    with pytest.raises(SourceError, match="pull one of a/graphs, b/graphs"):
        sources.resolve("graphs")


def test_resolve_missing_courses(two_sources: None) -> None:
    with pytest.raises(SourceError, match="no course 'x' in your sources"):
        sources.resolve("x")
    with pytest.raises(SourceError, match="no source named 'c'"):
        sources.resolve("c/x")


def test_resolve_without_sources() -> None:
    with pytest.raises(SourceError, match="pyics add_source"):
        sources.resolve("adding")


def test_search(two_sources: None) -> None:
    def found(term: str | None) -> list[str]:
        courses, _ = sources.search(term)
        return [f"{source.name}/{entry.slug}" for source, entry in courses]

    assert found(None) == ["a/adding", "a/graphs", "b/graphs"]
    assert found("BFS") == ["b/graphs"]  # in its description
    assert found("search") == ["b/graphs"]  # in its tags
    assert found("add") == ["a/adding"]


# --- writing indexes ---


def test_build_index_lists_the_newest_versions(
    tmp_path: Path,
    course_writer: Callable[..., Path],
    publisher: Callable[..., Path],
) -> None:
    library = tmp_path / "library"
    manifest = course_writer(tmp_path / "author", metadata={"version": "1.9.0"})
    publisher(manifest, library / "old")
    sources.build_index(library)  # replaced below, keeping its name
    (library / "index.json").write_text(
        json.dumps({"pyics_index": 1, "name": "Kept", "courses": {}})
    )
    archive.update_manifest(manifest, {"version": "1.10.0", "requires": ["numpy"]})
    publisher(manifest, library)
    fp, index = sources.build_index(library)

    assert fp == library / "index.json"
    assert index["name"] == "Kept"
    entry = index["courses"]["adding"]
    assert entry["version"] == "1.10.0"
    assert entry["url"] == "adding-1.10.0.zip"
    assert entry["requires"] == ["numpy"]
    parsed = sources.parse_index(json.loads(fp.read_text()), fp.as_uri())
    assert parsed.courses["adding"].url == (library / "adding-1.10.0.zip").as_uri()


def test_build_index_refuses_other_archives(tmp_path: Path) -> None:
    (tmp_path / "x.zip").write_bytes(b"not a zip")
    with pytest.raises(SourceError, match="isn't a course archive"):
        sources.build_index(tmp_path)
