"""Wires settings to the planner (shared by the GUI and the CLI)."""

from __future__ import annotations

from pathlib import Path

from .ai import AICache, make_provider
from .calibre_env import find_calibre_dir
from .config import Settings
from .extract import TextExtractor
from .planner import AIResolver


def require_calibre_dir(settings: Settings) -> Path:
    d = find_calibre_dir(settings.calibre_dir or None)
    if d is None:
        raise RuntimeError("Calibre installation not found. Set its folder in Settings.")
    return d


def make_resolver(settings: Settings) -> AIResolver | None:
    """Return an AI resolver, or None if AI is disabled. Caller must close() its extractor."""
    if not settings.use_ai:
        return None
    profile = settings.profile()
    if profile is None:
        raise RuntimeError(f"AI profile {settings.active_profile!r} not found. Configure it in Settings.")
    vision_profile = settings.profile(settings.vision_profile) if settings.vision_profile else None
    extractor = TextExtractor(
        require_calibre_dir(settings), settings.pdf_pages, settings.text_chars,
        render_images=vision_profile is not None,
    )
    return AIResolver(
        make_provider(profile), extractor, AICache(),
        make_provider(vision_profile) if vision_profile else None,
    )
