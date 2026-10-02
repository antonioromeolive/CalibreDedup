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

"""Persistent settings, stored as JSON in the user's config folder.

Each program keeps its own settings file; the list of AI providers is shared
(ai_profiles.json). API keys go to the OS credential store (keyring) when available.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

log = logging.getLogger(__name__)

APP_NAME = "CalibreDuplicateRemover"
OLLAMA = "ollama"
AZURE = "azure"
OPENAI = "openai"
ANTHROPIC = "anthropic"


DATA_DIR_NAME = ".CalibreDedup"
# The fields calibre-review can change (review.FIELDS), for its "Change:" boxes.
REVIEW_FIELD_NAMES = ("title", "authors", "publisher", "year", "series", "isbn", "language")
SETTINGS_FILE = "settings.json"  # Merge and Dedup
REVIEW_SETTINGS_FILE = "review_settings.json"  # calibre-review: its own copy, see load_review_settings
PROFILES_FILE = "ai_profiles.json"  # the AI providers, shared by both programs


def library_cache_dir() -> Path:
    """Where what was found in the books' files is kept between runs (see library_cache)."""
    return config_dir() / "library_cache"


def config_dir() -> Path:
    """All settings (including AI profiles), caches, remembered choices and logs
    live in ~/.CalibreDedup — never in the program's own folder."""
    d = Path.home() / DATA_DIR_NAME
    if not d.exists():
        _migrate_legacy_dir(d)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _migrate_legacy_dir(new: Path) -> None:
    """Move data from the folder used by earlier versions (%APPDATA%\\CalibreDuplicateRemover)."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home()))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    old = base / APP_NAME
    if old.is_dir():
        try:
            shutil.move(str(old), str(new))
            log.info("Moved data from %s to %s", old, new)
        except OSError as e:
            log.warning("Could not move %s to %s: %s", old, new, e)


@dataclass
class ProviderProfile:
    name: str
    kind: str = OLLAMA  # OLLAMA or AZURE
    # Ollama: the model name. Azure: the deployment name (or model name with api_version "v1").
    model: str = ""
    # Ollama: server URL. Azure: resource endpoint, e.g. https://myres.openai.azure.com
    base_url: str = "http://localhost:11434"
    api_version: str = "2024-10-21"  # Azure only; "v1" selects the /openai/v1 API
    temperature: float | None = 0.0  # None = don't send (needed for some reasoning models)
    timeout: int = 300
    num_ctx: int = 16384  # Ollama context window
    vision: bool = False  # model reads images as well as text (covers, scanned PDFs)
    # Advanced: [name, value] pairs added to every request, e.g. ["think", "false"] for
    # Ollama. Values are JSON when they parse as JSON, else text. See ai.extra_params().
    extra_params: list[list[str]] = field(default_factory=list)

    @property
    def api_key(self) -> str:
        return get_secret(self.name)

    @api_key.setter
    def api_key(self, value: str) -> None:
        set_secret(self.name, value)


@dataclass
class Settings:
    profiles: list[ProviderProfile] = field(default_factory=lambda: [
        ProviderProfile(name="Ollama (local)", kind=OLLAMA, model="llama3.1:8b"),
    ])
    text_profile: str = "Ollama (local)"  # reads book text for missing metadata; "" = AI off
    image_profile: str = ""  # reads text and images (covers, scanned PDFs); "" = none
    pdf_pages: int = 6  # pages read from the start/end of a PDF
    text_chars: int = 12000  # characters read from the start/end of other formats
    ignore_subtitle: bool = False
    similar_matching: bool = True  # authors match loosely and one shared author is enough
    cover_check: bool = True  # the image AI compares covers when metadata can't decide
    recheck_years: bool = True  # AI reads both books when only the metadata years differ
    same_series: bool = False  # same series + number (not 1) = same book, if the title or an author agrees
    always_cover: bool = False  # compare covers even when the metadata says different; same cover wins
    skip_generic_covers: bool = False  # for tests: don't look for generic covers (minutes on a big library)
    author_variants: bool = True  # same title, author written differently ("Frederickk"/"Frederick"; AI if on)
    fix_swapped: bool = True  # title and author swapped ("Kingston — The Log House by the Lake"): analyzed put right
    similar_titles: bool = True  # same author, one title inside the other ("1 Dune" / "Dune"): needs proof
    # Tick books with files Calibre can't open (fake or unsupported formats): all bad, the book
    # goes to the trash library; some, its record is copied there and those formats leave the source.
    trash_unreadable: bool = False
    # Cleanup of the source: nothing is copied to the target; only the source books already
    # in the target go to the trash library (not those whose target copy lacks a format).
    cleanup_only: bool = False
    only_tag: str = ""  # analyze only the source books with this tag; "" = all
    only_tag_exclude: bool = False  # ... all the source books except those with it, instead
    update_metadata: bool = True  # write AI-found title/authors/publisher to moved books
    delete_permanently: bool = False  # else removed books go to Calibre's own recycle bin
    calibre_dir: str = ""
    source_library: str = ""
    target_library: str = ""
    trash_library: str = ""  # where removed books go
    window_geometry: str = ""  # main window size/position (Qt saveGeometry, base64)
    dismissed_warnings: list[str] = field(default_factory=list)  # pre-flight warnings not to show again
    # calibre-review (python -m calibre_dedup.review)
    review_library: str = ""
    review_fields: list[str] = field(default_factory=lambda: list(REVIEW_FIELD_NAMES))
    # The fields there were when review_fields was saved: one added since starts on.
    review_fields_known: list[str] = field(default_factory=list)
    review_window_geometry: str = ""
    review_skip_reviewed: bool = True  # skip books tagged AIReviewed (review.REVIEWED_TAG)
    review_tag: str = ""  # review only the books with this tag; "" = all
    review_tag_exclude: bool = False  # ... all the books except those with it, instead

    def profile(self, name: str | None = None) -> ProviderProfile | None:
        name = self.text_profile if name is None else name
        return next((p for p in self.profiles if p.name == name), None)

    @property
    def use_ai(self) -> bool:
        return bool(self.text_profile)

    def image_ai(self) -> ProviderProfile | None:
        """The profile for images: only with AI on, and only if it supports images."""
        if not self.text_profile or not self.image_profile:
            return None
        p = self.profile(self.image_profile)
        return p if p is not None and p.vision else None

    def _fix_choices(self) -> None:
        """A Text/Image AI the other program renamed or removed: the first profile / none."""
        names = [p.name for p in self.profiles]
        if self.text_profile and self.text_profile not in names:
            log.info("Text AI %r no longer exists", self.text_profile)
            self.text_profile = names[0] if names else ""
        if self.image_profile and self.image_profile not in names:
            log.info("Image AI %r no longer exists", self.image_profile)
            self.image_profile = ""

    # --- persistence -------------------------------------------------------
    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        """Settings from `path` (default: Merge and Dedup's); save() writes them back there.
        The AI profiles come from ai_profiles.json in the same folder."""
        path = path or config_dir() / SETTINGS_FILE
        settings = cls._read(path)
        settings._path = path  # not a field: never saved
        shared = _load_profiles(path)
        if shared is not None:
            settings.profiles = shared
        settings._fix_choices()
        return settings

    def _settings_path(self) -> Path:
        return getattr(self, "_path", None) or config_dir() / SETTINGS_FILE

    def reload_profiles(self) -> bool:
        """Take in the profiles as saved now (perhaps edited by the other program).
        True if they changed; the Text/Image AI choices are fixed if they no longer exist."""
        shared = _load_profiles(self._settings_path())
        if shared is None or [asdict(p) for p in shared] == [asdict(p) for p in self.profiles]:
            return False
        self.profiles = shared
        self._fix_choices()
        return True

    def save_profiles(self) -> None:
        """Write the AI profiles for both programs (only the Settings dialog changes them)."""
        _write_json(self._settings_path().parent / PROFILES_FILE,
                    {"profiles": [asdict(p) for p in self.profiles]})

    @classmethod
    def _read(cls, path: Path) -> "Settings":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls()
        except (OSError, ValueError) as e:
            log.warning("Ignoring unreadable settings %s: %s", path, e)
            return cls()
        known = {f.name for f in fields(cls)}
        settings = cls(**{k: v for k, v in data.items() if k in known and k != "profiles"})
        if "profiles" in data:  # saved before ai_profiles.json
            settings.profiles = _profiles_from(data["profiles"])
        if "text_profile" not in data:  # settings saved before text/image profiles
            settings.text_profile = data.get("active_profile", settings.text_profile) if data.get("use_ai", True) else ""
            settings.image_profile = data.get("vision_profile", "")
        return settings

    def save(self, path: Path | None = None) -> None:
        """Everything but the AI profiles (see save_profiles): a program left open with
        an old list must not undo the other's changes. They stay in the file only while
        ai_profiles.json couldn't be written."""
        path = path or self._settings_path()
        data = asdict(self)
        if (path.parent / PROFILES_FILE).is_file():
            del data["profiles"]
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _profiles_from(items: list) -> list[ProviderProfile]:
    known = {f.name for f in fields(ProviderProfile)}
    return [ProviderProfile(**{k: v for k, v in p.items() if k in known}) for p in items]


def _read_json(path: Path):
    """The file's data; None if it is missing or unreadable."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        log.warning("Ignoring unreadable %s: %s", path, e)
        return None


def _write_json(path: Path, data) -> None:
    tmp = path.with_name(path.name + ".tmp")  # the other program never reads half a file
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _load_profiles(settings_path: Path) -> list[ProviderProfile] | None:
    """The shared AI profiles (ai_profiles.json next to the settings); None = none saved.
    The first time, they are made from calibre-review's list, else Merge and Dedup's,
    as both programs kept their own copy before."""
    folder = settings_path.parent
    shared = folder / PROFILES_FILE
    if shared.exists():
        data = _read_json(shared)
        if isinstance(data, dict) and isinstance(data.get("profiles"), list):
            return _profiles_from(data["profiles"])
        return None
    for source in (folder / REVIEW_SETTINGS_FILE, folder / SETTINGS_FILE, settings_path):
        data = _read_json(source)
        if isinstance(data, dict) and "profiles" in data:
            profiles = _profiles_from(data["profiles"])
            try:
                _write_json(shared, {"profiles": [asdict(p) for p in profiles]})
                log.info("AI profiles shared in %s, taken from %s", shared, source)
            except OSError as e:
                log.warning("Could not write %s: %s", shared, e)
            return profiles
    return None


def load_review_settings() -> Settings:
    """calibre-review's settings, separate from Merge and Dedup's so that both can
    run at the same time. The first time, they start as a copy of the duplicate
    remover's (trash library, Calibre folder…); after that each program keeps its
    own. The AI profiles (ai_profiles.json) and API keys (per profile name) are shared."""
    path = config_dir() / REVIEW_SETTINGS_FILE
    dedup = config_dir() / SETTINGS_FILE
    if not path.exists() and dedup.is_file():
        try:
            shutil.copyfile(dedup, path)
            log.info("Review settings created from %s", dedup)
        except OSError as e:
            log.warning("Could not copy %s to %s: %s", dedup, path, e)
    settings = Settings.load(path)
    # A field added since the settings were saved starts on; one the user turned off stays off.
    # Saved before review_fields_known existed: the fields known then were the first five.
    known = settings.review_fields_known or [f for f in REVIEW_FIELD_NAMES if f not in ("isbn", "language")]
    settings.review_fields += [f for f in REVIEW_FIELD_NAMES if f not in known and f not in settings.review_fields]
    settings.review_fields_known = list(REVIEW_FIELD_NAMES)
    return settings


# --- secrets ---------------------------------------------------------------
def _secrets_file() -> Path:
    return config_dir() / "secrets.json"


def get_secret(profile_name: str) -> str:
    env = os.environ.get("CDR_API_KEY")
    if env:
        return env
    try:
        import keyring
        value = keyring.get_password(APP_NAME, profile_name)
        if value is not None:
            return value
    except Exception:  # keyring missing or no backend
        pass
    try:
        return json.loads(_secrets_file().read_text(encoding="utf-8")).get(profile_name, "")
    except (OSError, ValueError):
        return ""


def set_secret(profile_name: str, value: str) -> None:
    try:
        import keyring
        if value:
            keyring.set_password(APP_NAME, profile_name, value)
        else:
            try:
                keyring.delete_password(APP_NAME, profile_name)
            except Exception:
                pass
        return
    except Exception as e:
        log.warning("Keyring unavailable (%s); storing API key in %s", e, _secrets_file())
    try:
        data = json.loads(_secrets_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data[profile_name] = value
    _secrets_file().write_text(json.dumps(data), encoding="utf-8")
