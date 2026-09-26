import pytest

from calibre_dedup import ai
from calibre_dedup.ai import (
    GOOD, NOTE, PROBLEM, AIError, _with_extra, connection_test, extra_params, make_provider, report_text,
)
from calibre_dedup.config import ANTHROPIC, AZURE, OLLAMA, OPENAI, ProviderProfile


def profile(kind=OLLAMA, params=()):
    return ProviderProfile(name="p", kind=kind, model="m", base_url="https://x", extra_params=[list(p) for p in params])


def test_values_are_json_when_they_parse_else_text():
    params, problems = extra_params(profile(params=[
        ("think", "false"), ("options.num_predict", "1024"), ("reasoning_effort", "none"),
        ("quoted", '"8192"'), ("obj", '{"a": 1}'), ("", ""),
    ]))
    assert problems == []
    assert params == {"think": False, "options.num_predict": 1024, "reasoning_effort": "none",
                      "quoted": "8192", "obj": {"a": 1}}


@pytest.mark.parametrize("kind,name", [
    (OLLAMA, "model"), (OLLAMA, "Temperature"), (OLLAMA, "messages.role"), (OLLAMA, "format"),
    (OLLAMA, "options"), (OLLAMA, "options.num_ctx"), (OLLAMA, "options.temperature"),
    (OPENAI, "response_format"), (AZURE, "response_format"), (ANTHROPIC, "max_tokens"),
    (ANTHROPIC, "system"), (OPENAI, "stream"),
])
def test_app_and_form_fields_are_refused(kind, name):
    params, problems = extra_params(profile(kind, [(name, "1")]))
    assert params == {} and "not accepted" in problems[0]


def test_bad_names_duplicates_and_missing_values_are_reported():
    _, problems = extra_params(profile(params=[("bad name", "1"), ("a", "1"), ("A", "2"), ("b", "")]))
    assert [p.split(":")[0] for p in problems] == ["bad name", "A", "b"]


def test_dotted_names_go_inside_objects_without_touching_the_rest():
    body = {"model": "m", "options": {"num_ctx": 16384}}
    out = _with_extra(body, {"think": False, "options.num_predict": 512, "chat_template_kwargs.enable_thinking": False})
    assert out == {"model": "m", "think": False, "options": {"num_ctx": 16384, "num_predict": 512},
                   "chat_template_kwargs": {"enable_thinking": False}}
    assert body == {"model": "m", "options": {"num_ctx": 16384}}  # not modified
    with pytest.raises(AIError):
        _with_extra({"model": "m"}, {"model.x": 1})


class FakeResponse:
    status_code = 200

    def __init__(self, data):
        self.data = data

    def json(self):
        return self.data


REPLIES = {
    OLLAMA: {"message": {"content": "{}"}, "done_reason": "stop"},
    AZURE: {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]},
    OPENAI: {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]},
    ANTHROPIC: {"content": [{"type": "text", "text": "{}"}], "stop_reason": "end_turn"},
}


@pytest.mark.parametrize("kind", [OLLAMA, AZURE, OPENAI, ANTHROPIC])
def test_every_provider_sends_the_advanced_parameters(kind, monkeypatch):
    sent = {}

    def post(url, json=None, **kw):
        sent.update(json)
        return FakeResponse(REPLIES[kind])
    monkeypatch.setattr(ai.requests, "post", post)
    monkeypatch.setattr(ProviderProfile, "api_key", property(lambda self: "key"))
    make_provider(profile(kind, [("reasoning_effort", "none")])).chat("s", "u")
    assert sent["reasoning_effort"] == "none" and "messages" in sent


def test_azure_content_filter_is_a_filtered_error(monkeypatch):
    class Refused(FakeResponse):
        status_code = 400
        text = "..."
    body = {"error": {"code": "content_filter", "status": 400, "innererror": {
        "code": "ResponsibleAIPolicyViolation", "content_filter_result": {
            "hate": {"filtered": False, "severity": "safe"}, "jailbreak": {"detected": False, "filtered": False},
            "violence": {"filtered": True, "severity": "high"}}}}}
    monkeypatch.setattr(ai.requests, "post", lambda *a, **k: Refused(body))
    monkeypatch.setattr(ProviderProfile, "api_key", property(lambda self: "key"))
    with pytest.raises(AIError, match=r"content filter refused the request \(violence\)") as e:
        make_provider(profile(AZURE, [])).chat("s", "u")
    assert e.value.filtered and e.value.status == 400
    monkeypatch.setattr(ai.requests, "post", lambda *a, **k: Refused({"error": {"code": "BadRequest"}}))
    with pytest.raises(AIError, match="Azure OpenAI error 400") as e:
        make_provider(profile(AZURE, [])).chat("s", "u")
    assert not e.value.filtered


def test_invalid_parameters_are_not_sent(monkeypatch):
    monkeypatch.setattr(ai.requests, "post", lambda *a, **k: pytest.fail("must not be sent"))
    with pytest.raises(AIError, match="Invalid advanced parameters"):
        make_provider(profile(OLLAMA, [("model", "x")])).chat("s", "u")


class ScriptedProvider(ai.Provider):
    def __init__(self, reply, info=None, params=()):
        super().__init__(profile(OLLAMA, params))
        self.reply, self.info = reply, info or {"finish": "stop"}

    def chat(self, system, user, images=None):
        self.last_info = self.info
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def run_test(provider):
    ok, lines = connection_test(provider)
    return ok, report_text(lines)


CORRECT = ('{"title": "Il guardiano del faro", "authors": ["Elena Marchetti"], "publisher": "Edizioni Lanterna",'
        ' "edition": "Terza edizione", "edition_number": 3, "year": 2021, "isbn": ["978-88-7000-123-4"]}')


def test_test_connection_passes_a_correct_answer():
    ok, report = run_test(ScriptedProvider(CORRECT, params=[("think", "false")]))
    assert ok and report.count("✓") == 6 and "think = false" in report


def test_test_connection_reports_misread_values():
    wrong = CORRECT.replace('"Elena Marchetti"', '"Paolo Bianchi"').replace('"edition_number": 3', '"edition_number": 1')
    ok, report = run_test(ScriptedProvider(wrong))
    assert ok and "✗ authors" in report and "✗ edition" in report and "2 of 6 values differ" in report


def test_test_connection_explains_an_empty_reply_cut_off_by_thinking():
    ok, report = run_test(ScriptedProvider("", {"finish": "length", "thinking_chars": 43000}))
    assert not ok and "empty reply" in report and "think = false" in report


def test_test_connection_reports_ignored_think():
    ok, report = run_test(ScriptedProvider(CORRECT, {"finish": "stop", "thinking_chars": 900},
                                           params=[("think", "false")]))
    assert not ok and "still thought" in report


class FakeServer(ai.Provider):
    """Any server: refuses a request holding a parameter in `refused` (or all of
    `together` at once) with an error in its own format; `silent` names time out."""

    def __init__(self, params, refused=(), together=(), silent=(), broken=False):
        super().__init__(profile(OLLAMA, params))
        self.refused, self.together, self.silent, self.broken = set(refused), set(together), set(silent), broken
        self.log = []  # shared by the copies the test makes to probe

    def chat(self, system, user, images=None):
        names = {n for n, _ in self.profile.extra_params}
        self.log.append(sorted(names))
        if self.broken:
            raise AIError("Acme error 401: {\"oops\": \"bad key\"}", status=401)
        if names & self.refused or (self.together and self.together <= names):
            raise AIError("Acme error 422: <html>unprocessable</html>", status=422)
        if names & self.silent:
            raise AIError("Acme request failed: read timed out")
        self.last_info = {"finish": "stop"}
        return CORRECT


def test_refused_parameter_is_found_by_asking_the_server_whatever_its_error_format():
    server = FakeServer([("think", "false"), ("top_k", "5"), ("mystery", "1")], refused={"mystery"})
    ok, report = run_test(server)
    assert not ok
    assert "✓ without advanced parameters: accepted" in report
    assert "✓ think = false: accepted" in report and "✓ top_k = 5: accepted" in report
    assert "✗ mystery = 1: refused. Acme error 422" in report
    # the test, then without parameters, then each one alone
    assert server.log == [["mystery", "think", "top_k"], [], ["think"], ["top_k"], ["mystery"]]


def test_parameters_refused_only_together():
    ok, report = run_test(FakeServer([("a", "1"), ("b", "2")], together={"a", "b"}))
    assert "Each parameter is accepted alone, but not all together" in report


def test_failure_without_parameters_is_not_blamed_on_them():
    ok, report = run_test(FakeServer([("a", "1")], broken=True))
    assert "fails even without advanced parameters" in report and "✗" not in report


def test_timeouts_are_not_refusals():
    ok, report = run_test(FakeServer([("a", "1"), ("slow", "1")], refused={"a"}, silent={"slow"}))
    assert "✗ a = 1: refused" in report and "? slow = 1: no answer" in report
    server = FakeServer([("slow", "1")], silent={"slow"})
    ok, report = run_test(server)  # the test itself timed out: no server answer, nothing to look for
    assert not ok and "Looking for the refused parameter" not in report and len(server.log) == 1


def test_test_connection_does_not_send_invalid_parameters():
    ok, report = run_test(ScriptedProvider(AIError("must not be called"), params=[("temperature", "1")]))
    assert not ok and "Not sent" in report and "must not be called" not in report


def test_report_lines_carry_their_level():
    wrong = CORRECT.replace('"edition_number": 3', '"edition_number": 1')
    _, lines = connection_test(ScriptedProvider(wrong, {"finish": "stop"}, params=[("think", "false")]))
    levels = dict((text.split(":")[0], level) for level, text in lines if text)
    assert levels["✓ title"] == GOOD and levels["✗ edition"] == PROBLEM
    notes = [text for level, text in lines if level == NOTE]
    assert any("values differ" in n for n in notes)
    assert any("A model may ignore a parameter" in n for n in notes)
    assert any("Ollama ignores parameter names" in n for n in notes)


def test_no_parameter_notes_without_parameters():
    _, lines = connection_test(ScriptedProvider(CORRECT))
    assert not [text for level, text in lines if level == NOTE]


class HtmlResponse:
    status_code = 200
    text = "<html><body>Welcome to nginx!</body></html>"

    def json(self):
        raise ValueError("Expecting value")


@pytest.mark.parametrize("kind", [OLLAMA, AZURE, OPENAI, ANTHROPIC])
def test_a_reply_that_is_not_json_shows_what_the_server_sent(kind, monkeypatch):
    monkeypatch.setattr(ai.requests, "post", lambda *a, **k: HtmlResponse())
    monkeypatch.setattr(ProviderProfile, "api_key", property(lambda self: "key"))
    ok, report = run_test(make_provider(profile(kind)))
    assert not ok and "wrong URL?" in report and "Welcome to nginx!" in report


def test_unexpected_errors_are_shown_not_raised():
    ok, report = run_test(ScriptedProvider(KeyError("choices")))
    assert not ok and "FAILED (unexpected KeyError): 'choices'" in report


def test_invalid_json_shows_the_reply():
    ok, report = run_test(ScriptedProvider('{"title": "Il guardiano", oops}'))
    assert not ok and "not valid JSON" in report and '{"title": "Il guardiano", oops}' in report


@pytest.mark.parametrize("url,version,expected", [
    ("https://r.openai.azure.com", "2024-10-21", ("https://r.openai.azure.com", False)),
    ("https://r.openai.azure.com/", "v1", ("https://r.openai.azure.com", True)),
    ("https://r.services.ai.azure.com/openai/v1", "2024-10-21", ("https://r.services.ai.azure.com", True)),
    ("https://r.services.ai.azure.com/openai/v1/", "v1", ("https://r.services.ai.azure.com", True)),
    ("https://r.openai.azure.com/openai/", "2024-10-21", ("https://r.openai.azure.com", False)),
])
def test_azure_endpoint_as_shown_in_the_portal_is_accepted(url, version, expected):
    from calibre_dedup.ai import azure_endpoint
    assert azure_endpoint(url, version) == expected
