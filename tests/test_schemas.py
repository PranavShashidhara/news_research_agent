import sys
sys.path.append(".")
from shared.schemas import ResearchAnswer, AnswerSentence, Confidence


def test_answer_roundtrip():
    a = ResearchAnswer(
        question="q",
        sentences=[AnswerSentence(text="x", source_ids=["s1"])],
        confidence=Confidence.high,
    )
    d = a.model_dump(mode="json")
    b = ResearchAnswer(**d)
    assert b.sentences[0].source_ids == ["s1"]
    assert b.confidence == Confidence.high
