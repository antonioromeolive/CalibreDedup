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
