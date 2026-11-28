from app.core.demo import seed_demo
from app.core.store import Store
from app.knowledge import GroundedAnswer, KnowledgeBase
from app.testing import FakeModel


def knowledge_base(tmp_path):
    store = Store(tmp_path / "test.sqlite3")
    store.initialize()
    seed_demo(store)
    return KnowledgeBase(store)


def test_replace_and_search_are_profile_scoped(tmp_path):
    knowledge = knowledge_base(tmp_path)
    knowledge.index("demo-p1", "rules", "Оплата в пятницу.")
    knowledge.index("demo-p2", "rules", "Оплата во вторник. Частный документ.")
    assert [p.text for p in knowledge.search("demo-p1", "оплата OR -{")] == ["Оплата в пятницу."]
    knowledge.index("demo-p1", "rules", "Оплата в среду.")
    assert knowledge.search("demo-p1", "пятницу") == []
    assert [p.text for p in knowledge.search("demo-p1", "оплата")] == ["Оплата в среду."]
    assert [p.text for p in knowledge.search("demo-p2", "оплата")] == [
        "Оплата во вторник. Частный документ.",
    ]


def test_answer_sends_own_passages_and_rejects_unknown_citations(tmp_path):
    knowledge = knowledge_base(tmp_path)
    knowledge.index("demo-p1", "rules", "Оплата в пятницу.")
    knowledge.index("demo-p2", "rules", "Оплата во вторник.")
    model = FakeModel(answer=GroundedAnswer(answer="В пятницу.", citations=[1]))
    answer, citations = knowledge.answer("demo-p1", "Когда оплата?", model)
    assert answer == "В пятницу."
    assert [(p.source_id, p.position) for p in citations] == [("rules", 1)]
    assert "пятницу" in model.calls[0][1][1]["content"]
    assert "вторник" not in model.calls[0][1][1]["content"]

    model = FakeModel(answer=GroundedAnswer(answer="Выдуманный ответ", citations=[2]))
    answer, citations = knowledge.answer("demo-p1", "Когда оплата?", model)
    assert answer.startswith("В базе знаний")
    assert not citations

    model = FakeModel()
    assert not knowledge.answer("demo-p1", "НДС", model)[1]
    assert not model.calls
