"""AI providers (Ollama, Azure OpenAI, OpenAI, Anthropic), metadata extraction and cover comparison."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import logging
import os
import re
import struct
import tempfile
import threading
import time
import zlib
from dataclasses import dataclass, field, replace
from pathlib import Path

import requests

from .config import ANTHROPIC, AZURE, OLLAMA, OPENAI, ProviderProfile, config_dir

log = logging.getLogger(__name__)


class AIError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status  # HTTP status when the server answered with an error, else None


def _solid_png_b64(width: int, height: int, rgb: tuple[int, int, int]) -> str:
    """A single-colour PNG, base64 encoded."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    rows = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))
    return base64.b64encode(png).decode()


# --- advanced parameters -----------------------------------------------------
# Request fields the app sets itself, or that have their own field in the profile
# (model, temperature, context size): never taken from the advanced parameters.
_RESERVED_ROOTS = {"model", "messages", "stream", "temperature"}
_RESERVED = {
    OLLAMA: {"format", "options.num_ctx", "options.temperature", "options"},
    AZURE: {"response_format"},
    OPENAI: {"response_format"},
    ANTHROPIC: {"system", "max_tokens"},
}
_NAME = re.compile(r"[A-Za-z_][\w-]*(\.[A-Za-z_][\w-]*)*")


def extra_params(profile: ProviderProfile) -> tuple[dict, list[str]]:
    """The profile's advanced parameters as {dotted name: value}, and the problems
    found. A value is JSON when it parses as JSON (false, 8192, "text", {...}),
    else plain text (none, low)."""
    params: dict = {}
    problems: list[str] = []
    reserved = _RESERVED.get(profile.kind, set())
    for pair in profile.extra_params:
        name, text = (list(pair) + ["", ""])[:2]
        name, text = name.strip(), text.strip()
        if not name and not text:
            continue
        if not _NAME.fullmatch(name):
            problems.append(f"{name or '(empty name)'!s}: not a valid parameter name")
            continue
        low = name.lower()
        if low.split(".")[0] in _RESERVED_ROOTS or low in reserved:
            problems.append(f"{name}: set by the app or by a field of this profile; not accepted here")
            continue
        if low in {n.lower() for n in params}:
            problems.append(f"{name}: given twice")
            continue
        if not text:
            problems.append(f"{name}: no value")
            continue
        try:
            params[name] = json.loads(text)
        except ValueError:
            params[name] = text
    return params, problems


def _with_extra(body: dict, params: dict) -> dict:
    """`body` with the parameters added; a dotted name goes inside an object ("options.num_predict")."""
    body = json.loads(json.dumps(body))
    for name, value in params.items():
        *path, leaf = name.split(".")
        node = body
        for part in path:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                raise AIError(f"Advanced parameter {name}: {part} is not an object in the request")
        node[leaf] = value
    return body


# --- providers ---------------------------------------------------------------
class Provider:
    def __init__(self, profile: ProviderProfile):
        self.profile = profile
        self.last_info: dict = {}  # details of the last reply (finish reason, thinking…), for the tests

    def _prepare(self, body: dict) -> dict:
        """Add the profile's advanced parameters; refuse to send if any is invalid."""
        params, problems = extra_params(self.profile)
        if problems:
            raise AIError("Invalid advanced parameters: " + "; ".join(problems))
        return _with_extra(body, params) if params else body

    def _log_call(self, user: str, images: list[str] | None) -> None:
        params, _ = extra_params(self.profile)
        extra = " extra=" + ",".join(f"{k}={json.dumps(v)}" for k, v in params.items()) if params else ""
        log.info("AI call started: provider=%s model=%s text_chars=%d images=%d%s",
                 self.profile.kind, self.profile.model or "(default)", len(user), len(images or []), extra)

    def _log_response(self, response: str) -> str:
        log.info("AI response: provider=%s model=%s\n%s",
                 self.profile.kind, self.profile.model or "(default)", response)
        return response

    def chat(self, system: str, user: str, images: list[str] | None = None) -> str:
        raise NotImplementedError

    def _json(self, r: requests.Response) -> dict:
        """The reply as a JSON object; else an error that shows what the server sent
        (e.g. an HTML page from a wrong URL)."""
        try:
            data = r.json()
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise AIError(f"The server's reply is not what a {self.profile.kind} API sends (wrong URL?): "
                          f"{r.text[:500]!r}")
        return data

    def _openai_content(self, data: dict) -> str:
        """Reply text of an OpenAI-style chat completion (OpenAI, Azure)."""
        choice = (data.get("choices") or [{}])[0]
        usage = data.get("usage") or {}
        self.last_info = {"finish": choice.get("finish_reason"), "output_tokens": usage.get("completion_tokens"),
                          "reasoning_tokens": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")}
        return choice.get("message", {}).get("content") or ""

    def list_models(self) -> list[str]:
        return []

    def test_images(self) -> tuple[bool, str]:
        """Send a plain red image and check the model names its colour: a model that
        can't read images either fails or can't know it. Returns (seen, reply)."""
        reply = self.chat("Reply with a JSON object.",
                          'What is the colour of the attached image? Return {"colour": "<one word>"}.',
                          [_solid_png_b64(64, 64, (220, 20, 20))])
        return "red" in reply.lower(), reply


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
            r = requests.post(self._url("/api/chat"), json=self._prepare(body), timeout=p.timeout)
        except requests.RequestException as e:
            raise AIError(f"Ollama request failed: {e}") from e
        if r.status_code != 200:
            raise AIError(f"Ollama error {r.status_code}: {r.text[:500]}", status=r.status_code)
        data = self._json(r)
        message = data.get("message", {})
        self.last_info = {"finish": data.get("done_reason"), "thinking_chars": len(message.get("thinking") or ""),
                          "output_tokens": data.get("eval_count")}
        return self._log_response(message.get("content", ""))

    def list_models(self) -> list[str]:
        try:
            r = requests.get(self._url("/api/tags"), timeout=10)
            r.raise_for_status()
        except requests.RequestException as e:
            raise AIError(f"Cannot reach Ollama at {self.profile.base_url}: {e}") from e
        return sorted(m["name"] for m in self._json(r).get("models", []))


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
            r = requests.post(url, json=self._prepare(body), timeout=p.timeout, headers={"api-key": key})
        except requests.RequestException as e:
            raise AIError(f"Azure OpenAI request failed: {e}") from e
        if r.status_code != 200:
            raise AIError(f"Azure OpenAI error {r.status_code}: {r.text[:500]}", status=r.status_code)
        return self._log_response(self._openai_content(self._json(r)))


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
            r = requests.post("https://api.openai.com/v1/chat/completions", json=self._prepare(body),
                              timeout=p.timeout, headers={"Authorization": f"Bearer {key}"})
        except requests.RequestException as e:
            raise AIError(f"OpenAI request failed: {e}") from e
        if r.status_code != 200:
            raise AIError(f"OpenAI error {r.status_code}: {r.text[:500]}", status=r.status_code)
        return self._log_response(self._openai_content(self._json(r)))


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
                "https://api.anthropic.com/v1/messages", json=self._prepare(body), timeout=p.timeout,
                headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
            )
        except requests.RequestException as e:
            raise AIError(f"Anthropic request failed: {e}") from e
        if r.status_code != 200:
            raise AIError(f"Anthropic error {r.status_code}: {r.text[:500]}", status=r.status_code)
        data = self._json(r)
        blocks = data.get("content") or []
        self.last_info = {"finish": data.get("stop_reason"),
                          "output_tokens": (data.get("usage") or {}).get("output_tokens")}
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


# --- connection test -----------------------------------------------------------
# A made-up front matter (so the model can't answer from memory), with the traps
# seen in real books: a translator, an original title, a first and a later edition.
TEST_EXCERPT = """ELENA MARCHETTI

IL GUARDIANO DEL FARO

Romanzo

Titolo originale dell'opera: Der Leuchtturmwächter
Traduzione di Paolo Bianchi

Edizioni Lanterna

© 2019 Edizioni Lanterna S.r.l., Torino
Prima edizione: marzo 2019
Terza edizione: ottobre 2021
ISBN 978-88-7000-123-4

Tutti i diritti riservati.

CAPITOLO PRIMO

La nebbia arrivò all'alba, come ogni giorno da quando Tommaso era tornato sull'isola."""
TEST_EXPECTED = {
    "title": ("Il guardiano del faro", lambda m: bool(m.title) and "guardiano del faro" in m.title.casefold()),
    "authors": ("Elena Marchetti (not the translator)",
                lambda m: any("marchetti" in a.casefold() for a in m.authors)
                and not any("bianchi" in a.casefold() for a in m.authors)),
    "publisher": ("Edizioni Lanterna", lambda m: bool(m.publisher) and "lanterna" in m.publisher.casefold()),
    "edition": ("3 (this copy is the third edition)", lambda m: m.edition_number == 3),
    "year": ("2021", lambda m: m.year == 2021),
    "isbn": ("978-88-7000-123-4",
             lambda m: any(re.sub(r"[^0-9]", "", i) == "9788870001234" for i in m.isbn)),
}


# Report line levels: the dialog colours them (good green, problem red, note orange).
INFO, GOOD, PROBLEM, NOTE = "info", "good", "problem", "note"
ReportLine = tuple[str, str]


def report_text(lines: list[ReportLine]) -> str:
    """The report as plain text (tests, logs)."""
    return "\n".join(text for _, text in lines)


def _probe(provider: Provider, pairs: list[list[str]]) -> AIError | None:
    """Send a short request with only these advanced parameters: the error, or None if accepted."""
    probe = copy.copy(provider)
    probe.profile = replace(provider.profile, extra_params=pairs)
    try:
        probe.chat("Reply with a JSON object.", 'Return {"ok": true}.')
    except AIError as e:
        return e
    except Exception as e:  # never lose the actual error
        return AIError(f"unexpected {type(e).__name__}: {e}")
    return None


def _find_refused(provider: Provider, params: dict, first_error: str = "") -> list[ReportLine]:
    """Which advanced parameters the server refuses, found by asking it again:
    without them, then with each one alone. Needs no knowledge of the server's
    error format, so it works with any provider."""
    def short(e: AIError) -> str:
        return str(e)[:600]

    lines: list[ReportLine] = [(INFO, f"Looking for the refused parameter ({len(params) + 1} short requests):")]
    error = _probe(provider, [])
    if error is not None:
        if error.status is None:
            return lines + [(NOTE, f"Inconclusive: no answer without parameters ({short(error)}).")]
        same = str(error) == first_error  # already shown above: don't repeat it
        return lines + [(PROBLEM, "It fails even without advanced parameters, so they are not the cause: "
                                  "check the model, key and URL." + ("" if same else f" ({short(error)})"))]
    lines.append((GOOD, "✓ without advanced parameters: accepted"))
    refused = unknown = 0
    for pair in ([n, v] for n, v in ((list(x) + ["", ""])[:2] for x in provider.profile.extra_params)
                 if n.strip() in params):
        error = _probe(provider, [pair])
        if error is None:
            lines.append((GOOD, f"✓ {pair[0]} = {pair[1]}: accepted"))
        elif error.status is None:
            unknown += 1
            lines.append((NOTE, f"? {pair[0]} = {pair[1]}: no answer ({short(error)})"))
        else:
            refused += 1
            lines.append((PROBLEM, f"✗ {pair[0]} = {pair[1]}: refused. {short(error)}"))
    if refused:
        lines.append((PROBLEM, "Remove or correct the refused parameters (✗)."))
    elif not unknown:
        lines.append((PROBLEM, "Each parameter is accepted alone, but not all together."))
    return lines


def connection_test(provider: Provider) -> tuple[bool, list[ReportLine]]:
    """Send one real metadata request with the profile's settings and advanced
    parameters, and report what went wrong: parameters refused, empty reply,
    no JSON, answer cut short, slow answer, wrong values. Returns (ok, report
    lines as (level, text))."""
    lines: list[ReportLine] = []
    params, problems = extra_params(provider.profile)
    if params:
        lines.append((INFO, "Advanced parameters sent: "
                       + ", ".join(f"{k} = {json.dumps(v)}" for k, v in params.items())))
    else:
        lines.append((INFO, "No advanced parameters."))
    if problems:
        return False, lines + [(INFO, ""), (PROBLEM, "Not sent. Fix these parameters first:")] + [
            (PROBLEM, f"• {p}") for p in problems]

    started = time.monotonic()
    try:
        raw = provider.chat(SYSTEM_PROMPT, f"Excerpt:\n\n{TEST_EXCERPT}")
    except AIError as e:
        lines += [(INFO, ""), (PROBLEM, f"FAILED: {e}")]
        if params and e.status is not None:  # the server answered with an error: maybe a parameter
            lines += [(INFO, "")] + _find_refused(provider, params, str(e))
        return False, lines
    except Exception as e:  # anything else: still show the actual error
        return False, lines + [(INFO, ""), (PROBLEM, f"FAILED (unexpected {type(e).__name__}): {e}")]
    secs = time.monotonic() - started
    info = provider.last_info
    ok = True
    lines.append((INFO, f"Answer in {secs:.1f} s" + (f" · finish: {info['finish']}" if info.get("finish") else "")
                  + (f" · output tokens: {info['output_tokens']}" if info.get("output_tokens") else "")))
    if info.get("thinking_chars"):
        lines.append((INFO, f"The model thought before answering ({info['thinking_chars']:,} characters)."))
    if info.get("reasoning_tokens"):
        lines.append((INFO, f"The model reasoned before answering ({info['reasoning_tokens']:,} tokens)."))

    if not raw.strip():
        ok = False
        lines += [(INFO, ""), (PROBLEM, "PROBLEM: empty reply.")]
        if info.get("finish") in ("length", "max_tokens"):
            lines.append((PROBLEM, "The answer was cut off: the model used all its room (thinking/reasoning?) "
                                   "before writing it. Switch thinking off (Ollama: think = false; OpenAI: "
                                   "reasoning_effort = none or low) or raise the context size."))
    else:
        try:
            meta = AIMetadata.from_json(raw)
        except AIError as e:
            return False, lines + [(INFO, ""), (PROBLEM, f"PROBLEM: {e}"), (INFO, "The model replied:"),
                                   (INFO, raw[:800] + ("…" if len(raw) > 800 else ""))]
        if info.get("finish") in ("length", "max_tokens"):
            ok = False
            lines.append((PROBLEM, "PROBLEM: the answer was cut off (finish: length)."))
        lines += [(INFO, ""), (INFO, "Values read from the test page:")]
        for field_name, (expected, check) in TEST_EXPECTED.items():
            good = check(meta)
            got = getattr(meta, field_name if field_name != "edition" else "edition_number")
            got = ", ".join(got) if isinstance(got, list) else got
            lines.append((GOOD if good else PROBLEM,
                          f"{'✓' if good else '✗'} {field_name}: {got if got not in (None, '') else '—'}"
                          + ("" if good else f"   (expected {expected})")))
        wrong = [f for f, (_, check) in TEST_EXPECTED.items() if not check(meta)]
        if wrong:
            lines.append((NOTE, f"{len(wrong)} of {len(TEST_EXPECTED)} values differ: the model works, "
                                "but may misread some books."))
    if "think" in {k.lower() for k in params} and params.get("think") is False and info.get("thinking_chars"):
        ok = False
        lines.append((PROBLEM, "PROBLEM: think = false was sent but the model still thought: this model or "
                               "Ollama version ignores it."))
    elif provider.profile.kind == OLLAMA and params and info.get("thinking_chars"):
        lines.append((NOTE, "The model still thought: if one of the parameters was meant to switch thinking "
                            "off, it had no effect (Ollama's name is think)."))
    notes = []
    if secs > 60:
        notes.append(f"Slow: {secs:.0f} s for one short page. An analysis makes this call once or "
                     "twice per undecided book.")
    if params:
        notes.append("A model may ignore a parameter it doesn't support, or refuse the request. A refusal "
                     "shows up here as an error; an ignored parameter doesn't, so check that it had the "
                     "effect you wanted (e.g. a faster answer).")
        if provider.profile.kind == OLLAMA:
            notes.append("Ollama ignores parameter names it doesn't know, without any error: "
                         "check the spelling.")
    if notes:
        lines += [(INFO, "")] + [(NOTE, f"Note: {n}") for n in notes]
    return ok, lines


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
