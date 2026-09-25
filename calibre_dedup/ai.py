"""AI providers (Ollama, Azure OpenAI, OpenAI, Anthropic), metadata extraction and cover comparison."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import struct
import tempfile
import threading
import zlib
from dataclasses import dataclass, field
from pathlib import Path

import requests

from .config import ANTHROPIC, AZURE, OLLAMA, OPENAI, ProviderProfile, config_dir

log = logging.getLogger(__name__)


class AIError(Exception):
    pass


def _solid_png_b64(width: int, height: int, rgb: tuple[int, int, int]) -> str:
    """A single-colour PNG, base64 encoded."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    rows = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))
    return base64.b64encode(png).decode()


# --- providers ---------------------------------------------------------------
class Provider:
    def __init__(self, profile: ProviderProfile):
        self.profile = profile

    def _log_call(self, user: str, images: list[str] | None) -> None:
        log.info("AI call started: provider=%s model=%s text_chars=%d images=%d",
                 self.profile.kind, self.profile.model or "(default)", len(user), len(images or []))

    def _log_response(self, response: str) -> str:
        log.info("AI response: provider=%s model=%s\n%s",
                 self.profile.kind, self.profile.model or "(default)", response)
        return response

    def chat(self, system: str, user: str, images: list[str] | None = None) -> str:
        raise NotImplementedError

    def list_models(self) -> list[str]:
        return []

    def test(self) -> str:
        reply = self.chat("Reply with a JSON object.", 'Return {"ok": true}.')
        return f"OK: {reply[:200]}"

    def test_images(self) -> bool:
        """Send a plain red image and check the model names its colour: a model that
        can't read images either fails or can't know it."""
        reply = self.chat("Reply with a JSON object.",
                          'What is the colour of the attached image? Return {"colour": "<one word>"}.',
                          [_solid_png_b64(64, 64, (220, 20, 20))])
        return "red" in reply.lower()


class OllamaProvider(Provider):
    def _url(self, path: str) -> str:
        return self.profile.base_url.rstrip("/") + path

    def chat(self, system: str, user: str, images: list[str] | None = None) -> str:
        p = self.profile
        self._log_call(user, images)
        user_msg: dict = {"role": "user", "content": user}
        if images:
            user_msg["images"] = images
        options: dict = {"num_ctx": p.num_ctx}
        if p.temperature is not None:
            options["temperature"] = p.temperature
        body = {
            "model": p.model,
            "messages": [{"role": "system", "content": system}, user_msg],
            "format": "json",
            "stream": False,
            "options": options,
        }
        try:
            r = requests.post(self._url("/api/chat"), json=body, timeout=p.timeout)
        except requests.RequestException as e:
            raise AIError(f"Ollama request failed: {e}") from e
        if r.status_code != 200:
            raise AIError(f"Ollama error {r.status_code}: {r.text[:500]}")
        return self._log_response(r.json().get("message", {}).get("content", ""))

    def list_models(self) -> list[str]:
        try:
            r = requests.get(self._url("/api/tags"), timeout=10)
            r.raise_for_status()
        except requests.RequestException as e:
            raise AIError(f"Cannot reach Ollama at {self.profile.base_url}: {e}") from e
        return sorted(m["name"] for m in r.json().get("models", []))


class AzureOpenAIProvider(Provider):
    def chat(self, system: str, user: str, images: list[str] | None = None) -> str:
        p = self.profile
        self._log_call(user, images)
        if not p.base_url or not p.model:
            raise AIError("Azure OpenAI profile needs an endpoint and a deployment name")
        key = p.api_key
        if not key:
            raise AIError(f"No API key set for profile {p.name!r}")
        endpoint = p.base_url.rstrip("/")
        content: list | str = user
        if images:
            content = [{"type": "text", "text": user}] + [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img}"}} for img in images
            ]
        body: dict = {
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
            "response_format": {"type": "json_object"},
        }
        if p.temperature is not None:
            body["temperature"] = p.temperature
        if p.api_version.strip().lower() == "v1":
            url = f"{endpoint}/openai/v1/chat/completions"
            body["model"] = p.model
        else:
            url = f"{endpoint}/openai/deployments/{p.model}/chat/completions?api-version={p.api_version}"
        try:
            r = requests.post(url, json=body, timeout=p.timeout, headers={"api-key": key})
        except requests.RequestException as e:
            raise AIError(f"Azure OpenAI request failed: {e}") from e
        if r.status_code != 200:
            raise AIError(f"Azure OpenAI error {r.status_code}: {r.text[:500]}")
        choices = r.json().get("choices") or [{}]
        return self._log_response(choices[0].get("message", {}).get("content") or "")


class OpenAIProvider(Provider):
    def chat(self, system: str, user: str, images: list[str] | None = None) -> str:
        p = self.profile
        self._log_call(user, images)
        if not p.model:
            raise AIError("OpenAI profile needs a model name")
        key = p.api_key
        if not key:
            raise AIError(f"No API key set for profile {p.name!r}")
        content: list | str = user
        if images:
            content = [{"type": "text", "text": user}] + [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img}"}} for img in images
            ]
        body: dict = {
            "model": p.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
            "response_format": {"type": "json_object"},
        }
        if p.temperature is not None:
            body["temperature"] = p.temperature
        try:
            r = requests.post("https://api.openai.com/v1/chat/completions", json=body,
                              timeout=p.timeout, headers={"Authorization": f"Bearer {key}"})
        except requests.RequestException as e:
            raise AIError(f"OpenAI request failed: {e}") from e
        if r.status_code != 200:
            raise AIError(f"OpenAI error {r.status_code}: {r.text[:500]}")
        choices = r.json().get("choices") or [{}]
        return self._log_response(choices[0].get("message", {}).get("content") or "")


class AnthropicProvider(Provider):
    def chat(self, system: str, user: str, images: list[str] | None = None) -> str:
        p = self.profile
        self._log_call(user, images)
        if not p.model:
            raise AIError("Anthropic profile needs a model name")
        key = p.api_key
        if not key:
            raise AIError(f"No API key set for profile {p.name!r}")
        content: list | str = user
        if images:
            content = [{"type": "text", "text": user}] + [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img}}
                for img in images
            ]
        body: dict = {
            "model": p.model,
            "max_tokens": 2048,
            "system": system,
            "messages": [{"role": "user", "content": content}],
        }
        if p.temperature is not None:
            body["temperature"] = p.temperature
        try:
            r = requests.post(
                "https://api.anthropic.com/v1/messages", json=body, timeout=p.timeout,
                headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
            )
        except requests.RequestException as e:
            raise AIError(f"Anthropic request failed: {e}") from e
        if r.status_code != 200:
            raise AIError(f"Anthropic error {r.status_code}: {r.text[:500]}")
        blocks = r.json().get("content") or []
        return self._log_response("\n".join(block.get("text", "") for block in blocks if block.get("type") == "text"))


def make_provider(profile: ProviderProfile) -> Provider:
    if profile.kind == OLLAMA:
        return OllamaProvider(profile)
    if profile.kind == AZURE:
        return AzureOpenAIProvider(profile)
    if profile.kind == OPENAI:
        return OpenAIProvider(profile)
    if profile.kind == ANTHROPIC:
        return AnthropicProvider(profile)
    raise AIError(f"Unknown provider type {profile.kind!r}")


# --- metadata extraction -----------------------------------------------------
SYSTEM_PROMPT = """You are a librarian extracting bibliographic metadata from an excerpt of an e-book \
(front matter such as the title page and copyright page, or back matter such as a colophon). \
The text may be in any language.

Respond with a single JSON object with exactly these keys:
{"title": string|null, "authors": [string], "publisher": string|null, "edition": string|null,
 "edition_number": integer|null, "year": integer|null, "isbn": [string]}

Rules:
- Use only information present in the text. Never guess. Use null or [] when not found.
- "title": the book's title (include the subtitle if clearly shown), not a chapter or series name.
- "authors": the authors' names as printed. Exclude translators, editors of forewords, illustrators.
- "publisher": the publishing house of THIS edition (not the printer or distributor).
- "edition": the edition statement as printed, e.g. "Second edition", "3a edizione".
- "edition_number": the edition number (1 for an explicitly stated first edition). Printing/reprint \
lines such as "10 9 8 7 6 5 4 3 2 1" or "ristampa" are NOT editions.
- "year": the publication year of THIS edition (not the original first publication, if both are shown).
- "isbn": ISBNs printed for this book (digits and X only).
"""


@dataclass
class AIMetadata:
    title: str | None = None
    authors: list[str] = field(default_factory=list)
    publisher: str | None = None
    edition: str | None = None
    edition_number: int | None = None
    year: int | None = None
    isbn: list[str] = field(default_factory=list)

    @classmethod
    def from_json(cls, text: str) -> "AIMetadata":
        if not text.strip():
            # Some models answer nothing at all, instead of nulls, when the pages
            # hold no metadata. Taken as "nothing found", so it is cached, not retried.
            log.info("AI reply is empty: taken as no metadata found")
            return cls()
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise AIError(f"AI reply is not JSON: {text[:200]!r}")
        try:
            data = json.loads(m.group())
        except ValueError as e:
            raise AIError(f"AI reply is not valid JSON: {e}") from e

        def s(v):
            return v.strip() if isinstance(v, str) and v.strip() and v.strip().lower() not in ("null", "none", "unknown") else None

        def i(v):
            try:
                return int(v) if v is not None and str(v).strip() else None
            except (TypeError, ValueError):
                return None

        def lst(v):
            if isinstance(v, str):
                v = [v]
            return [x.strip() for x in v if isinstance(x, str) and x.strip()] if isinstance(v, list) else []

        year = i(data.get("year"))
        return cls(
            title=s(data.get("title")),
            authors=lst(data.get("authors")),
            publisher=s(data.get("publisher")),
            edition=s(data.get("edition")),
            edition_number=i(data.get("edition_number")),
            year=year if year and 1400 <= year <= 2100 else None,
            isbn=lst(data.get("isbn")),
        )

    def to_dict(self) -> dict:
        return self.__dict__.copy()


class AICache:
    """JSON cache so repeated analyses don't re-query the model."""

    def __init__(self, path: Path | None = None):
        self.path = path or config_dir() / "ai_cache.json"
        self._lock = threading.Lock()
        try:
            self._data: dict = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._data = {}

    @staticmethod
    def key(file_path: str, part: str, model: str | None = None) -> str:
        try:
            st = Path(file_path).stat()
            stamp = f"{st.st_size}:{int(st.st_mtime)}"
        except OSError:
            stamp = "?"
        token = f"{file_path}|{stamp}|{part}"
        return hashlib.sha1(token.encode()).hexdigest()

    @classmethod
    def pair_key(cls, path_a: str, path_b: str, part: str) -> str:
        """Order-insensitive key for a question about two files."""
        a, b = sorted((cls.key(path_a, part), cls.key(path_b, part)))
        return hashlib.sha1(f"{a}|{b}".encode()).hexdigest()

    def get(self, key: str) -> dict | None:
        return self._data.get(key)

    def put(self, key: str, value: dict) -> None:
        with self._lock:
            self._data[key] = value

    def save(self) -> None:
        with self._lock:
            tmp_name = ""
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=self.path.parent,
                    prefix=f"{self.path.stem}_", suffix=".tmp", delete=False,
                ) as tmp:
                    tmp_name = tmp.name
                    json.dump(self._data, tmp)
                    tmp.flush()
                    os.fsync(tmp.fileno())
                os.replace(tmp_name, self.path)
            except OSError as e:
                log.warning("Could not save AI cache %s: %s", self.path, e)
            finally:
                if tmp_name:
                    try:
                        Path(tmp_name).unlink()
                    except OSError:
                        pass


def extract_metadata(provider: Provider, text: str, images: list[str] | None = None) -> AIMetadata:
    if images:
        user = "The excerpt is given as page images" + (f", plus this extracted text:\n\n{text}" if text else ".")
    else:
        user = f"Excerpt:\n\n{text}"
    return AIMetadata.from_json(provider.chat(SYSTEM_PROMPT, user, images or None))


# --- cover comparison --------------------------------------------------------
COVER_PROMPT = """You compare two book cover images to tell whether they are the cover of the same \
edition of the same book.

Respond with a single JSON object: {"verdict": "same"|"different"|"unsure", "reason": string}

Rules:
- "same": the same cover artwork, title and author, and layout. Differences in resolution, cropping, \
compression, colour balance, borders or small overlays (e.g. a store badge) do not matter.
- "different": different artwork, title, author, publisher logo or edition statement (e.g. a \
"2nd edition" banner on one only).
- "unsure": either image is not a real cover (blank, generic placeholder, text-only generated cover) \
or you cannot tell.
"""

COVER_VERDICTS = ("same", "different", "unsure")


def compare_covers(provider: Provider, cover_a: str, cover_b: str) -> tuple[str, str]:
    """Ask a vision model whether two base64 PNG covers are the same. Returns (verdict, reason)."""
    text = provider.chat(COVER_PROMPT, "Cover 1 and cover 2 are attached.", [cover_a, cover_b])
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise AIError(f"AI reply is not JSON: {text[:200]!r}")
    try:
        data = json.loads(m.group())
    except ValueError as e:
        raise AIError(f"AI reply is not valid JSON: {e}") from e
    verdict = str(data.get("verdict", "")).strip().lower()
    reason = data.get("reason")
    return (verdict if verdict in COVER_VERDICTS else "unsure"), (reason if isinstance(reason, str) else "")
