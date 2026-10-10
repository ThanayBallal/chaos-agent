"""Ollama provider: OpenAI-compatible endpoint first, native /api/chat as fallback. No network."""
import pytest
import requests

from agent import llm


class Rsp:
    def __init__(self, obj):
        self.obj = obj

    def raise_for_status(self):
        pass

    def json(self):
        return self.obj


def fail(*arg):
    raise RuntimeError("404 from /v1")


def test_compat_used_when_it_works(monkeypatch):
    monkeypatch.setattr(llm, "_ollama_compat", lambda *arg: '{"ok": 1}')
    monkeypatch.setattr(requests, "post", lambda *arg, **kw: pytest.fail("native should not be called"))
    assert llm._ollama("k", "m", "s", "u", None, 10) == '{"ok": 1}'


def test_falls_back_to_native(monkeypatch):
    sen = {}

    def post(url, **kw):
        sen.update(url=url, **kw)
        return Rsp({"message": {"content": '{"ok": 2}'}, "done_reason": "stop"})

    monkeypatch.setenv("LLM_BASE_URL", "https://ollama.com/v1/")
    monkeypatch.setattr(llm, "_ollama_compat", fail)
    monkeypatch.setattr(requests, "post", post)
    assert llm._ollama("k", "m", "s", "u", None, 10) == '{"ok": 2}'
    assert sen["url"] == "https://ollama.com/api/chat"
    assert sen["headers"]["Authorization"] == "Bearer k"
    assert sen["json"]["stream"] is False and sen["json"]["model"] == "m"


def test_both_fail_names_both(monkeypatch):
    def post(url, **kw):
        raise requests.ConnectionError("native down")

    monkeypatch.setattr(llm, "_ollama_compat", fail)
    monkeypatch.setattr(requests, "post", post)
    with pytest.raises(RuntimeError, match="404 from /v1.*native down"):
        llm._ollama("k", "m", "s", "u", None, 10)


def test_native_length_cutoff_raises(monkeypatch):
    monkeypatch.setattr(llm, "_ollama_compat", fail)
    monkeypatch.setattr(requests, "post", lambda *arg, **kw: Rsp({"message": {"content": "{"}, "done_reason": "length"}))
    with pytest.raises(RuntimeError, match="length"):
        llm._ollama("k", "m", "s", "u", None, 10)


def test_config_accepts_ollama(monkeypatch):
    monkeypatch.delenv("CHAOS_NO_LLM", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setenv("LLM_MODEL", "")
    assert llm.config() == ("ollama", "k", "gpt-oss:120b")
