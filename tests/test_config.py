# Copyright (c) 2026 Antonio Romeo <antonioromeo@ilve.it>
# Author: Antonio Romeo (with Claude Code et al.)
# SPDX-License-Identifier: MIT
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

from pathlib import Path

from calibre_dedup import config
from calibre_dedup.config import ProviderProfile, Settings


def _fake_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "appdata"))
    return home


def test_data_dir_is_in_user_home(monkeypatch, tmp_path):
    home = _fake_home(monkeypatch, tmp_path)
    assert config.config_dir() == home / ".CalibreDedup"
    s = Settings(profiles=[ProviderProfile(name="Azure", kind=config.AZURE, model="gpt-4o")])
    s.save()
    assert (home / ".CalibreDedup" / "settings.json").is_file()
    assert Settings.load().profile("Azure").model == "gpt-4o"


def test_legacy_data_is_moved(monkeypatch, tmp_path):
    home = _fake_home(monkeypatch, tmp_path)
    legacy = tmp_path / "appdata" / config.APP_NAME
    legacy.mkdir(parents=True)
    (legacy / "settings.json").write_text('{"source_library": "D:/Books"}', encoding="utf-8")
    assert Settings.load().source_library == "D:/Books"
    assert (home / ".CalibreDedup" / "settings.json").is_file()
    assert not legacy.exists()


def test_old_ai_settings_are_migrated(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"profiles": [{"name": "G", "kind": "ollama", "vision": true}, {"name": "T", "kind": "ollama"}],'
                    ' "active_profile": "T", "use_ai": true, "vision_profile": "G"}', encoding="utf-8")
    s = Settings.load(path)
    assert (s.text_profile, s.image_profile) == ("T", "G")
    assert s.image_ai().name == "G"

    path.write_text('{"active_profile": "T", "use_ai": false}', encoding="utf-8")
    s = Settings.load(path)
    assert s.text_profile == "" and not s.use_ai


def test_image_ai_needs_text_ai_and_image_support():
    s = Settings(profiles=[ProviderProfile(name="G", vision=True), ProviderProfile(name="T")],
                 text_profile="T", image_profile="G")
    assert s.image_ai().name == "G"
    s.image_profile = "T"  # not marked 'Supports images'
    assert s.image_ai() is None
    s.image_profile, s.text_profile = "G", ""  # AI off means images off too
    assert s.image_ai() is None


def test_review_settings_start_as_a_copy_then_are_separate(monkeypatch, tmp_path):
    _fake_home(monkeypatch, tmp_path)
    dedup = config.Settings.load()
    dedup.trash_library, dedup.source_library = "D:/Trash", "D:/Inbox"
    dedup.save()
    review = config.load_review_settings()
    assert (review.trash_library, review.source_library) == ("D:/Trash", "D:/Inbox")
    review.trash_library = "E:/ReviewTrash"
    review.save()  # to its own file
    dedup.source_library = "D:/Other"
    dedup.save()
    assert config.Settings.load().trash_library == "D:/Trash"
    again = config.load_review_settings()  # not copied again
    assert (again.trash_library, again.source_library) == ("E:/ReviewTrash", "D:/Inbox")


def test_review_cache_starts_as_a_copy_then_is_separate(monkeypatch, tmp_path):
    _fake_home(monkeypatch, tmp_path)
    from calibre_dedup.ai import AICache
    from calibre_dedup.review import review_cache
    shared = AICache()
    shared.put("old", {"title": "x"})
    shared.save()
    cache = review_cache()
    assert cache.get("old") == {"title": "x"}
    cache.put("new", {"title": "y"})
    cache.save()
    assert AICache().get("new") is None
    assert review_cache().get("new") == {"title": "y"}


def test_clearing_the_review_cache_does_not_bring_back_the_shared_answers(monkeypatch, tmp_path):
    _fake_home(monkeypatch, tmp_path)
    from calibre_dedup.ai import AICache
    from calibre_dedup.review import REVIEW_CACHE_FILE, review_cache
    shared = AICache()
    shared.put("old", {"title": "x"})
    shared.save()
    review = review_cache()  # first start: a copy
    review.put("mine", {"title": "y"})
    review.save()
    path = config.config_dir() / REVIEW_CACHE_FILE
    assert AICache.size(path)[0] == 2
    assert AICache.clear(path) == 2
    assert review_cache().get("old") is None and AICache.size(path) == (0, 2)  # emptied, not copied again
    assert AICache().get("old") == {"title": "x"}  # the other program's cache is untouched


def test_no_cache_ignores_the_saved_answers_but_saves_the_new_ones(monkeypatch, tmp_path):
    _fake_home(monkeypatch, tmp_path)
    from calibre_dedup.ai import AICache
    from calibre_dedup.review import review_cache
    shared = AICache()
    shared.put("old", {"title": "x"})
    shared.put("again", {"title": "x"})
    shared.save()
    for make in (lambda: AICache(off=True), lambda: review_cache(off=True)):
        cache = make()
        assert cache.get("old") is None and cache.get("again") is None
        cache.put("again", {"title": "y"})
        cache.put("new", {"title": "y"})
        assert cache.get("new") == {"title": "y"}  # reused within the run
        cache.save()
        assert cache.get("old") is None  # saving doesn't take the old answers back in
    for cache in (AICache(), review_cache()):
        assert cache.get("old") == {"title": "x"}  # the others are kept
        assert cache.get("again") == cache.get("new") == {"title": "y"}  # replaced, added


def test_programs_analyzing_at_once_keep_each_others_answers(monkeypatch, tmp_path):
    _fake_home(monkeypatch, tmp_path)
    from calibre_dedup.ai import AICache
    a, b = AICache(), AICache()  # both started before either saved
    a.put("a1", {"title": "x"})
    a.save()
    b.put("b1", {"title": "y"})
    b.save()
    a.put("a2", {"title": "z"})
    a.save()
    assert AICache().get("a1") and AICache().get("b1") and AICache().get("a2")
    assert a.get("b1") == {"title": "y"}  # the other's answers are taken in when saving


def test_nothing_new_nothing_written(monkeypatch, tmp_path):
    _fake_home(monkeypatch, tmp_path)
    from calibre_dedup.ai import AICache
    cache = AICache()
    cache.save()
    path = config.config_dir() / "ai_cache.json"
    assert not path.exists()
    cache.put("k", {"title": "x"})
    cache.save()
    path.write_text('{"k": {"title": "x"}, "other": {}}', encoding="utf-8")
    cache.put("k", {"title": "x"})  # same answer again: nothing new
    cache.save()
    assert path.read_text(encoding="utf-8") == '{"k": {"title": "x"}, "other": {}}'  # not rewritten


def test_clearing_while_an_analysis_runs_keeps_only_its_later_answers(monkeypatch, tmp_path):
    _fake_home(monkeypatch, tmp_path)
    from calibre_dedup.ai import AICache
    running = AICache()
    running.put("old", {"title": "x"})
    running.save()
    assert AICache.clear(config.config_dir() / "ai_cache.json") == 1
    running.put("new", {"title": "y"})
    running.save()
    assert AICache().get("old") is None and AICache().get("new") == {"title": "y"}
    assert running.get("old") is None


def test_a_save_that_cannot_lock_keeps_its_answers_for_the_next(monkeypatch, tmp_path):
    _fake_home(monkeypatch, tmp_path)
    from calibre_dedup import ai
    monkeypatch.setattr(ai._FileLock, "TIMEOUT", 0.1)
    cache = ai.AICache()
    cache.put("k", {"title": "x"})
    with ai._FileLock(cache.path):  # another program saving
        cache.save()
    assert ai.AICache().get("k") is None
    cache.save()
    assert ai.AICache().get("k") == {"title": "x"}
