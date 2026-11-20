"""llm.complete_json: 길이 제한(max_tokens)에 걸려 잘린 응답 → 더 큰 한도로 한 번 다시 → 성공 / 또 잘리면 Truncated."""
import types

import pytest

from negotiation_agent import llm


def _fake(monkeypatch, replies):
    calls = []

    def create(system, user, max_tokens):
        calls.append(max_tokens)
        text, stop = replies.pop(0)
        return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=text)], stop_reason=stop)

    monkeypatch.setattr(llm, "_create", create)
    return calls


def test_truncated_reply_is_retried_with_bigger_limit(monkeypatch, capsys):
    calls = _fake(monkeypatch, [('```json\n{\n  "subject": "Inquiry: EVA hard shell", "body": "Dear Mrs. Mel', "max_tokens"),
                                ('```json\n{"subject": "Inquiry", "body": "Dear Melody"}\n```', "end_turn")])
    assert llm.complete_json("s", "u") == {"subject": "Inquiry", "body": "Dear Melody"}
    assert calls == [llm.MAX_TOKENS, llm.RETRY_MAX_TOKENS]
    assert "길이 제한" in capsys.readouterr().out


def test_truncated_twice_says_so_not_json_error(monkeypatch):
    calls = _fake(monkeypatch, [('{"body": "a', "max_tokens"), ('{"body": "ab', "max_tokens")])
    with pytest.raises(llm.Truncated, match="길이 제한"):
        llm.complete_json("s", "u", max_tokens=3000)
    assert calls == [3000, llm.RETRY_MAX_TOKENS]


def test_normal_reply_single_call(monkeypatch):
    calls = _fake(monkeypatch, [('{"a": 1}', "end_turn")])
    assert llm.complete_json("s", "u") == {"a": 1} and calls == [6000]
