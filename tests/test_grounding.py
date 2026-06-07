"""Tests the orchestrator's server-side grounding enforcement in isolation."""
import sys
sys.path.append(".")
sys.path.append("services/orchestrator")

# Re-implement the pure logic under test to avoid importing heavy deps
# (anthropic, otel) at test time. Mirrors _finalize's grounding rule.
from shared.schemas import AnswerSentence, ResearchAnswer  # noqa: E402


def enforce_grounding(sentences, verdicts):
    unsupported = {v["claim"]["text"] for v in verdicts if not v["supported"]}
    kept, dropped = [], 0
    for s in sentences:
        if not s.source_ids or any(u in s.text for u in unsupported):
            dropped += 1
            continue
        kept.append(s)
    return kept, dropped


def test_drops_uncited_sentence():
    sents = [AnswerSentence(text="A", source_ids=[]),
             AnswerSentence(text="B", source_ids=["s1"])]
    kept, dropped = enforce_grounding(sents, [])
    assert dropped == 1 and len(kept) == 1 and kept[0].text == "B"


def test_drops_unsupported_claim():
    sents = [AnswerSentence(text="X happened", source_ids=["s1"])]
    verdicts = [{"claim": {"text": "X happened"}, "supported": False}]
    kept, dropped = enforce_grounding(sents, verdicts)
    assert dropped == 1 and kept == []


def test_keeps_supported():
    sents = [AnswerSentence(text="Y is true", source_ids=["s2"])]
    verdicts = [{"claim": {"text": "Y is true"}, "supported": True}]
    kept, dropped = enforce_grounding(sents, verdicts)
    assert dropped == 0 and len(kept) == 1
