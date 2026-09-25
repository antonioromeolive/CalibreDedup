"""Wires settings to the planner (shared by the GUI and the CLI)."""

from __future__ import annotations

import logging
from pathlib import Path

from .ai import AICache, make_provider
from .calibre_env import find_calibre_dir
from .config import ProviderProfile, Settings
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


def make_resolver(settings: Settings) -> AIResolver | None:
    """Return an AI resolver, or None if AI is off. Caller must close() its extractor."""
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
    return AIResolver(
        make_provider(profile), extractor, AICache(),
        make_provider(image) if image else None,
    )
