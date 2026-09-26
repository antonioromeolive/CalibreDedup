from calibre_dedup.config import OLLAMA, ProviderProfile, Settings
from calibre_dedup.session import analysis_signature, changed_settings, preflight


def _settings(**kw):
    s = Settings(profiles=[ProviderProfile("Text", OLLAMA, "qwen"), ProviderProfile("Seeing", OLLAMA, "gemma", vision=True)],
                 text_profile="Text", image_profile="", cover_check=True, recheck_years=True)
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def keys(issues):
    return [i.key for i in issues]


def test_cover_check_without_image_ai_offers_a_vision_profile():
    issues = preflight(_settings(), ping=lambda p: "")
    assert keys(issues) == ["cover_no_image"] and issues[0].fix_image_profile == "Seeing"
    assert preflight(_settings(image_profile="Seeing"), ping=lambda p: "") == []
    assert preflight(_settings(cover_check=False), ping=lambda p: "") == []


def test_text_profile_that_reads_images_is_the_preferred_fix():
    s = _settings()
    s.profiles[0].vision = True
    assert preflight(s, ping=lambda p: "")[0].fix_image_profile == "Text"


def test_ai_off_with_ai_options_on():
    issues = preflight(_settings(text_profile=""), ping=lambda p: "down")
    assert keys(issues) == ["ai_off"] and "cover check and year re-check" in issues[0].message
    assert preflight(_settings(text_profile="", cover_check=False, recheck_years=False)) == []


def test_unreachable_ollama_is_always_reported_once_per_profile():
    pinged = []
    issues = preflight(_settings(image_profile="Seeing"), ping=lambda p: pinged.append(p.name) or "refused")
    assert keys(issues) == ["unreachable:Text", "unreachable:Seeing"]
    assert not any(i.dismissable for i in issues) and pinged == ["Text", "Seeing"]
    s = _settings(text_profile="Seeing", image_profile="Seeing")
    assert keys(preflight(s, ping=lambda p: "refused")) == ["unreachable:Seeing"]


def test_changed_settings_name_what_changed():
    s = _settings()
    before = analysis_signature(s)
    assert changed_settings(before, analysis_signature(s)) == []
    s.cover_check = False
    s.image_profile = "Seeing"
    s.source_library = r"C:\Books" "\\"
    assert changed_settings(before, analysis_signature(s)) == ["source library", "Image AI", "cover check"]
    s = _settings(source_library="c:/books")
    t = _settings(source_library=r"C:\Books" "\\")
    assert changed_settings(analysis_signature(s), analysis_signature(t)) == []
    t.profiles[0].model = "llama"  # the Text AI profile was edited
    assert changed_settings(analysis_signature(s), analysis_signature(t)) == ["Text AI"]


def test_always_compare_covers_also_needs_an_image_ai():
    issues = preflight(_settings(cover_check=False, always_cover=True), ping=lambda p: "")
    assert keys(issues) == ["cover_no_image"]
