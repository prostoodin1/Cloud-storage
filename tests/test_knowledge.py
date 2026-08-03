from cloud_storage.help.knowledge import KnowledgeBase


def test_local_assistant_finds_pairing_answer_and_source(tmp_path) -> None:
    knowledge = KnowledgeBase(tmp_path / "knowledge.db")

    answer = knowledge.ask("почему код подключения не работает", "client")

    assert answer.sources
    assert answer.sources[0].id == "pairing-code-expiry"
    assert "15 минут" in answer.text
    assert answer.confidence > 0.5


def test_articles_are_filtered_for_server_and_client(tmp_path) -> None:
    knowledge = KnowledgeBase(tmp_path / "knowledge.db")

    client_ids = {item.article.id for item in knowledge.search("", "client", limit=100)}
    server_ids = {item.article.id for item in knowledge.search("", "server", limit=100)}

    assert "server-address" in client_ids
    assert "server-address" not in server_ids
    assert "create-user" in server_ids
    assert "create-user" not in client_ids
    assert "personal-storage" in client_ids & server_ids


def test_unknown_questions_are_persisted_without_inventing_an_answer(tmp_path) -> None:
    path = tmp_path / "knowledge.db"
    knowledge = KnowledgeBase(path)

    answer = knowledge.ask("умеет ли система выращивать базилик на марсе", "client")
    repeated = KnowledgeBase(path).ask("умеет ли система выращивать базилик на марсе", "client")

    assert answer.sources == ()
    assert answer.confidence == 0
    assert repeated.sources == ()
    assert knowledge.unanswered_count() == 2
