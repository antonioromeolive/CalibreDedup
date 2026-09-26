"""Wires settings to the planner (shared by the GUI and the CLI)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import requests

from .ai import AICache, make_provider
from .calibre_env import find_calibre_dir
from .config import OLLAMA, ProviderProfile, Settings
from .extract import TextExtractor
from .planner import AIResolver

log = logging.getLogger(__name__)


def require_calibre_dir(settings: Settings) -> Path:
    d = find_calibre_dir(settings.calibre_dir or None)
    if d is None:
        raise RuntimeError("Calibre installation not found. Set its folder in Settings.")
    return d


def _describe(p: ProviderProfile) -> str:
    return f"{p.name} ({p.model or 'no model'})"


def make_resolver(settings: Settings, on_down: Callable[[str, bool], bool] | None = None,
                  cls: type[AIResolver] = AIResolver, cache: AICache | None = None) -> AIResolver | None:
    """Return an AI resolver (an instance of `cls`), or None if AI is off. Caller must
    close() its extractor. `on_down`: see AIResolver (asked when an AI keeps failing)."""
    if not settings.use_ai:
        log.info("AI: off (metadata only)")
        return None
    profile = settings.profile()
    if profile is None:
        raise RuntimeError(f"Text AI profile {settings.text_profile!r} not found. Configure it in Settings.")
    image = settings.image_ai()
    if settings.image_profile and image is None:
        log.warning("Image AI %r ignored: profile not found or not marked 'Supports images'",
                    settings.image_profile)
    if image is None:
        log.info("AI: text = %s · images = none (cover check and scanned PDFs skipped)", _describe(profile))
    else:
        log.info("AI: text = %s · images = %s · cover check %s", _describe(profile), _describe(image),
                 "on" if settings.cover_check else "off")
    extractor = TextExtractor(
        require_calibre_dir(settings), settings.pdf_pages, settings.text_chars,
        render_images=image is not None,
    )
    return cls(
        make_provider(profile), extractor, cache or AICache(),
        make_provider(image) if image else None,
        on_down=on_down,
    )


# --- pre-flight -----------------------------------------------------------------
@dataclass
class Issue:
    """A setting that won't take effect, found before an analysis starts."""
    key: str  # stable id, for "don't warn me again"
    message: str
    dismissable: bool = True  # False: a real problem (AI unreachable), always shown
    fix_image_profile: str = ""  # the fix: use this profile as Image AI


PING_TIMEOUT = 3  # seconds: the check runs on the GUI thread before the analysis


def _ollama_problem(p: ProviderProfile) -> str:
    """Why this Ollama profile can't answer, or ''."""
    try:
        r = requests.get(p.base_url.rstrip("/") + "/api/tags", timeout=PING_TIMEOUT)
        r.raise_for_status()
        names = {m.get("name", "") for m in r.json().get("models", [])}
    except (requests.RequestException, ValueError, AttributeError) as e:
        return f"cannot reach Ollama at {p.base_url} (is it running?): {e}"
    if p.model and p.model not in names and f"{p.model}:latest" not in names:
        return f"model {p.model!r} is not installed in Ollama at {p.base_url}"
    return ""


def preflight(settings: Settings, ping: Callable[[ProviderProfile], str] = _ollama_problem) -> list[Issue]:
    """Settings that are on but can't take effect, and AIs that can't be reached.
    Only local Ollama servers are pinged (cloud APIs would need keys and cost calls)."""
    issues: list[Issue] = []
    text = settings.profile() if settings.use_ai else None
    image = settings.image_ai()
    if not settings.use_ai:
        wanted = [name for on, name in ((settings.cover_check or settings.always_cover, "cover check"),
                                        (settings.recheck_years, "year re-check")) if on]
        if wanted:
            issues.append(Issue("ai_off", f"Text AI is None: {' and '.join(wanted)} won't run, and missing "
                                          "titles/authors won't be read from the books."))
        return issues
    if text is None:
        issues.append(Issue("text_missing", f"Text AI profile {settings.text_profile!r} not found.", False))
    if settings.image_profile and image is None:
        issues.append(Issue("image_invalid", f"Image AI {settings.image_profile!r} is not usable: profile not "
                                             "found or not marked 'Supports images'.", False))
    elif (settings.cover_check or settings.always_cover) and image is None:
        vision = [p for p in settings.profiles if p.vision]
        fix = text.name if text is not None and text.vision else (vision[0].name if vision else "")
        hint = "" if fix else " No profile is marked 'Supports images' in Settings."
        issues.append(Issue("cover_no_image", "Cover check is on, but no Image AI is selected: covers won't be "
                                              f"compared and scanned PDFs won't be read.{hint}",
                            fix_image_profile=fix))
    checked: set[str] = set()
    for role, p in (("Text AI", text), ("Image AI", image)):
        if p is None or p.kind != OLLAMA or p.name in checked:
            continue
        checked.add(p.name)
        problem = ping(p)
        if problem:
            issues.append(Issue(f"unreachable:{p.name}", f"{role} {p.name!r}: {problem}.", False))
    return issues


# --- stale plan -----------------------------------------------------------------
# Settings that change the analysis' result, with how they are named to the user.
ANALYSIS_SETTINGS = {
    "source_library": "source library", "target_library": "target library",
    "text_profile": "Text AI", "image_profile": "Image AI",
    "pdf_pages": "PDF pages to read", "text_chars": "characters to read",
    "ignore_subtitle": "ignore subtitles", "similar_matching": "similar author matching",
    "cover_check": "cover check", "recheck_years": "year re-check", "same_series": "same series",
    "similar_titles": "similar titles", "always_cover": "always compare covers",
    "author_variants": "authors written differently",
}


def analysis_signature(settings: Settings) -> dict:
    sig = {k: getattr(settings, k) for k in ANALYSIS_SETTINGS}
    for k in ("text_profile", "image_profile"):  # a profile edited in Settings counts too
        p = settings.profile(sig[k]) if sig[k] else None
        sig[k] = (sig[k], p.kind, p.model, p.base_url, p.vision) if p else sig[k]
    for k in ("source_library", "target_library"):
        sig[k] = str(sig[k]).strip().replace("\\", "/").rstrip("/").casefold()
    return sig


def changed_settings(before: dict, after: dict) -> list[str]:
    return [label for k, label in ANALYSIS_SETTINGS.items() if before.get(k) != after.get(k)]
