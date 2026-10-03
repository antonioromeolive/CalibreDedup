# Copyright (c) 2026 Antonio Romeo <antonioromeo@ilve.it>
# Author: Antonio Romeo (with Claude Code et al.)
# SPDX-License-Identifier: MIT

import json
import logging

import pytest

from calibre_dedup import perf
from calibre_dedup.ai import AIError, make_provider
from calibre_dedup.config import AZURE, OLLAMA, ProviderProfile


@pytest.fixture
def records():
    """The performance log's records, as dicts."""
    lines: list[str] = []

    class Collect(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())

    handler = Collect()
    perf.log.addHandler(handler)
    yield lambda: [json.loads(line) for line in lines]
    perf.log.removeHandler(handler)
    perf.run_end(0)  # a failed test leaves no run open


class Reply:
    status_code = 200

    def __init__(self, data):
        self.data = data
        self.text = json.dumps(data)

    def json(self):
        return self.data


def provider(kind, monkeypatch, data):
    monkeypatch.setattr("calibre_dedup.ai.requests.post", lambda *a, **k: Reply(data))
    monkeypatch.setattr("calibre_dedup.config.get_secret", lambda name: "k")  # never the real keyring
    return make_provider(ProviderProfile(name="p", kind=kind, model="m", base_url="https://x"))


OLLAMA_REPLY = {"message": {"content": "{}"}, "done_reason": "stop", "prompt_eval_count": 3000, "eval_count": 80,
                "load_duration": 50_000_000, "prompt_eval_duration": 1_500_000_000,
                "eval_duration": 900_000_000, "total_duration": 2_500_000_000}


def test_a_call_logs_sizes_tokens_and_timings_but_no_book_text(records, monkeypatch):
    ollama = provider(OLLAMA, monkeypatch, OLLAMA_REPLY)
    ollama.chat("system prompt", "Il guardiano del faro, Elena Marchetti", ["AAAA"])
    [call] = records()
    assert call["event"] == "call" and call["outcome"] == "ok" and call["book"] is None
    assert call["text_chars"] == 38 and call["images"] == 1 and call["image_bytes"] == 3
    assert call["input_tokens"] == 3000 and call["output_tokens"] == 80
    assert call["prompt_seconds"] == 1.5 and call["server_seconds"] == 2.5
    assert "Marchetti" not in json.dumps(records())


def test_azure_usage_is_logged(records, monkeypatch):
    azure = provider(AZURE, monkeypatch, {
        "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1200, "completion_tokens": 300, "prompt_tokens_details": {"cached_tokens": 1024},
                  "completion_tokens_details": {"reasoning_tokens": 250}}})
    azure.chat("s", "u")
    [call] = records()
    assert (call["input_tokens"], call["cached_tokens"], call["output_tokens"], call["reasoning_tokens"]) \
        == (1200, 1024, 300, 250)


def test_run_counts_the_books_sent_to_the_ai(records, monkeypatch):
    ollama = provider(OLLAMA, monkeypatch, OLLAMA_REPLY)
    perf.run_start("dedup", 3, ollama)
    perf.book(1)
    ollama.chat("s", "start")
    ollama.chat("s", "end")
    perf.book(2)  # answered from the cache: no call
    perf.book(3)
    ollama.chat("s", "start")
    perf.run_end(3)
    start, *calls, end = records()
    assert start["event"] == "run_start" and start["text_ai"]["provider"] == OLLAMA
    assert [c["book"] for c in calls] == [1, 1, 3]
    assert end["event"] == "run_end" and end["books_done"] == 3 and end["books_with_ai"] == 2
    assert end["calls"] == 3 and end["input_tokens"] == 9000


def test_a_failed_call_is_logged_with_its_outcome(records, monkeypatch):
    ollama = provider(OLLAMA, monkeypatch, {})
    monkeypatch.setattr(type(ollama), "_chat", lambda *a: (_ for _ in ()).throw(AIError("x", filtered=True)))
    with pytest.raises(AIError):
        ollama.chat("s", "u")
    assert records()[0]["outcome"] == "filtered"


def test_a_run_left_open_by_an_exception_is_closed_by_the_next(records):
    perf.run_start("review", 5)
    perf.run_start("review", 5)
    assert [r["event"] for r in records()] == ["run_start", "run_end", "run_start"]
    assert records()[1]["aborted"]


def test_two_runs_at_once_count_their_own_calls(records, monkeypatch):
    import threading
    ollama = provider(OLLAMA, monkeypatch, OLLAMA_REPLY)
    perf.run_start("review", 10, ollama)
    perf.book(7)
    started, go_on = threading.Event(), threading.Event()

    def ask():  # the AI asked again about 2 books, in its own thread, during the scan
        perf.run_start("review-ask", 2, ollama)
        perf.book(1)
        started.set()
        go_on.wait()
        ollama.chat("s", "u")
        perf.run_end(1)

    t = threading.Thread(target=ask)
    t.start()
    started.wait()
    ollama.chat("s", "u")  # the scan's call, while the other run is open
    go_on.set()
    t.join()
    perf.run_end(10)
    calls = [r for r in records() if r["event"] == "call"]
    assert [(c["program"], c["book"]) for c in calls] == [("review", 7), ("review-ask", 1)]
    ends = {r["program"]: r for r in records() if r["event"] == "run_end"}
    assert ends["review"]["calls"] == ends["review-ask"]["calls"] == 1


def test_time_paused_is_left_out_of_the_run(records, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(perf.time, "monotonic", lambda: now[0])
    perf.run_start("dedup", 10)
    now[0] += 5
    perf.pause()
    now[0] += 600
    perf.resume()
    now[0] += 5
    perf.run_end(10)
    start, pause, resume, end = records()
    assert (pause["event"], resume["event"], resume["paused_seconds"]) == ("pause", "resume", 600)
    assert end["seconds"] == 10 and end["paused_seconds"] == 600
