import json
import os

from calibre_dedup.calibre_env import known_libraries


def _lib(tmp_path, name):
    d = tmp_path / name
    d.mkdir()
    (d / "metadata.db").write_bytes(b"")
    return d


def test_known_libraries_reads_usage_from_gui_json_most_used_first(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    a, b, c = _lib(tmp_path, "a"), _lib(tmp_path, "b"), _lib(tmp_path, "c")
    gone = tmp_path / "gone"  # no metadata.db: left out
    (cfg / "global.py.json").write_text(json.dumps({"library_path": str(c)}), encoding="utf-8")
    (cfg / "gui.json").write_text(json.dumps({"library_usage_stats": {
        a.as_posix(): 3, b.as_posix(): 10, gone.as_posix(): 50, c.as_posix(): 1}}), encoding="utf-8")
    monkeypatch.setenv("CALIBRE_CONFIG_DIRECTORY", str(cfg))
    assert known_libraries() == [str(c), str(b), str(a)]  # current library, then by use


def test_known_libraries_merges_paths_differing_only_in_case(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    a, b = _lib(tmp_path, "a"), _lib(tmp_path, "b")
    stats = {a.as_posix(): 4, b.as_posix(): 5}
    if os.path.normcase("A") == os.path.normcase("a"):  # case-insensitive file system (Windows)
        stats[a.as_posix().upper()] = 3  # 4 + 3 uses: now ahead of b
    (cfg / "gui.json").write_text(json.dumps({"library_usage_stats": stats}), encoding="utf-8")
    monkeypatch.setenv("CALIBRE_CONFIG_DIRECTORY", str(cfg))
    libs = known_libraries()
    assert len(libs) == 2
    if os.path.normcase("A") == os.path.normcase("a"):
        assert libs == [str(a), str(b)]


def test_known_libraries_without_calibre_config(tmp_path, monkeypatch):
    monkeypatch.setenv("CALIBRE_CONFIG_DIRECTORY", str(tmp_path / "none"))
    assert known_libraries() == []
