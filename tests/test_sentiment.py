"""analyze_sentiment()'s JSON parsing, with the Groq API call fully mocked out -
these tests make no real network calls and cost nothing to run."""

import json
from unittest.mock import MagicMock

import collector


def _fake_completion(raw_content: str):
    """Builds a fake object shaped like what ai_client.chat.completions.create()
    returns, so analyze_sentiment() can't tell the difference from a real call."""
    completion = MagicMock()
    completion.choices = [MagicMock()]
    completion.choices[0].message.content = raw_content
    return completion


def test_parses_well_formed_json_response(monkeypatch):
    payload = json.dumps({
        "sentiment": "positive",
        "label": "Praise",
        "explanation": "[English] 'great product' - customer is happy",
    })
    monkeypatch.setattr(collector.ai_client.chat.completions, "create", lambda **kw: _fake_completion(payload))

    sentiment, label, explanation = collector.analyze_sentiment("great product!")

    assert sentiment == "Positive"
    assert label == "Praise"
    assert "great product" in explanation


def test_strips_markdown_json_code_fence(monkeypatch):
    payload = "```json\n" + json.dumps({
        "sentiment": "negative",
        "label": "Bug/Performance",
        "explanation": "App keeps crashing",
    }) + "\n```"
    monkeypatch.setattr(collector.ai_client.chat.completions, "create", lambda **kw: _fake_completion(payload))

    sentiment, label, explanation = collector.analyze_sentiment("this app keeps crashing")

    assert sentiment == "Negative"
    assert label == "Bug/Performance"


def test_falls_back_to_neutral_on_malformed_json(monkeypatch):
    monkeypatch.setattr(
        collector.ai_client.chat.completions, "create",
        lambda **kw: _fake_completion("this is not valid json at all {{{")
    )

    sentiment, label, explanation = collector.analyze_sentiment("some comment")

    assert sentiment == "Neutral"
    assert label == "General Inquiry"


def test_unrecognized_sentiment_value_falls_back_to_neutral(monkeypatch):
    """The AI is asked to only ever return Positive/Negative/Neutral - if it
    ever returns something else, we should not save garbage into the sentiment
    column (main.py's KPI/trend queries rely on it being one of those three)."""
    payload = json.dumps({"sentiment": "Sarcastic", "label": "Praise", "explanation": "..."})
    monkeypatch.setattr(collector.ai_client.chat.completions, "create", lambda **kw: _fake_completion(payload))

    sentiment, _, _ = collector.analyze_sentiment("yeah right, love waiting 3 hours for support")

    assert sentiment == "Neutral"


def test_retries_then_gives_up_on_persistent_rate_limiting(monkeypatch):
    """Confirms the retry-on-429 behavior that was missing from main.py's old
    duplicate copy of this function (the bug we fixed by consolidating onto
    this one) - and that it eventually gives up instead of retrying forever."""
    call_count = {"n": 0}

    def always_rate_limited(**kwargs):
        call_count["n"] += 1
        raise Exception("Error code: 429 - rate_limit_exceeded")

    monkeypatch.setattr(collector.ai_client.chat.completions, "create", always_rate_limited)
    monkeypatch.setattr(collector.time, "sleep", lambda seconds: None)  # skip real waiting

    sentiment, label, explanation = collector.analyze_sentiment("some comment")

    assert call_count["n"] == 3  # max_retries
    assert sentiment == "Neutral"
    assert "rate limit" in explanation.lower()
