"""Persistent settings, stored as JSON in the user's config folder.

API keys go to the OS credential store (keyring) when available.
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
    vision: bool = False  # model accepts images (used for scanned PDFs)

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
    active_profile: str = "Ollama (local)"
    use_ai: bool = True
    vision_profile: str = ""  # profile used for scanned PDFs; "" disables
    pdf_pages: int = 6  # pages read from the start/end of a PDF
    text_chars: int = 12000  # characters read from the start/end of other formats
    ignore_subtitle: bool = False
    update_metadata: bool = True  # write AI-found title/authors/publisher to moved books
    delete_permanently: bool = False  # else removed books go to Calibre's own recycle bin
    calibre_dir: str = ""
    source_library: str = ""
    target_library: str = ""
    trash_library: str = ""

    def profile(self, name: str | None = None) -> ProviderProfile | None:
        name = self.active_profile if name is None else name
        return next((p for p in self.profiles if p.name == name), None)

    # --- persistence -------------------------------------------------------
    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        path = path or config_dir() / "settings.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls()
        except (OSError, ValueError) as e:
            log.warning("Ignoring unreadable settings %s: %s", path, e)
            return cls()
        known = {f.name for f in fields(cls)}
        pknown = {f.name for f in fields(ProviderProfile)}
        settings = cls(**{k: v for k, v in data.items() if k in known and k != "profiles"})
        if "profiles" in data:
            settings.profiles = [
                ProviderProfile(**{k: v for k, v in p.items() if k in pknown}) for p in data["profiles"]
            ]
        return settings

    def save(self, path: Path | None = None) -> None:
        path = path or config_dir() / "settings.json"
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")


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
