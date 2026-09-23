"""Connections: four types, LangChain interfaces with their own argument names, and the roles."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel

from vilagent import connections
from vilagent.config.app_config import AppConfig
from vilagent.config.computer_use_config import ComputerUseConfig
from vilagent.server import models, models_api, state
from vilagent.server.deps import get_config, internal_auth_headers

CHAT_OPENAI = "langchain_openai:ChatOpenAI"
CHAT_GEMINI = "langchain_google_genai:ChatGoogleGenerativeAI"
CHAT_OLLAMA = "langchain_ollama:ChatOllama"
EMBED_OPENAI = "langchain_openai:OpenAIEmbeddings"
EMBED_HF = "langchain_huggingface:HuggingFaceEndpointEmbeddings"


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "STATE_FILE_PATH", tmp_path / "state.json")


def _config() -> AppConfig:
    return AppConfig(computer_use=ComputerUseConfig(enabled=True))


def _client():
    app = FastAPI()
    app.include_router(models_api.router)
    app.dependency_overrides[get_config] = lambda: _config()
    return TestClient(app, headers=internal_auth_headers())


def _glm(kind: str = connections.LLM) -> connections.Connection:
    return connections.save(
        "My GLM",
        kind,
        CHAT_OPENAI,
        {"model": "glm-4.5-flash", "base_url": "https://open.bigmodel.cn/api/paas/v4", "api_key": "secret-key", "temperature": "0.2", "max_tokens": "2048", "timeout": "45", "max_retries": "3"},
    )


# --- the store ------------------------------------------------------------------------


def test_a_connection_keeps_its_arguments_and_encrypts_only_the_secrets():
    saved = _glm()

    assert (saved.id, saved.kind, saved.interface) == ("my-glm", connections.LLM, CHAT_OPENAI)
    assert saved.params == {"model": "glm-4.5-flash", "base_url": "https://open.bigmodel.cn/api/paas/v4", "temperature": "0.2", "max_tokens": "2048", "timeout": "45", "max_retries": "3"}
    assert saved.secrets_set == ("api_key",)  # the key is not among the params
    assert connections.secrets_of("my-glm") == {"api_key": "secret-key"}

    with sqlite3.connect(connections.db_path()) as db:
        row = db.execute("SELECT params, secrets FROM connections WHERE id = ?", ("my-glm",)).fetchone()
    assert "secret-key" not in row[0] and b"secret-key" not in bytes(row[1])


def test_an_edit_keeps_a_secret_that_is_not_sent_again():
    _glm()
    connections.save("My GLM", connections.LLM, CHAT_OPENAI, {"model": "glm-4.6", "base_url": "https://open.bigmodel.cn/api/paas/v4"}, connection_id="my-glm")

    assert connections.secrets_of("my-glm") == {"api_key": "secret-key"}
    assert connections.get("my-glm").params["model"] == "glm-4.6"
    assert "temperature" not in connections.get("my-glm").params  # left out means left unset

    connections.save("My GLM", connections.LLM, CHAT_OPENAI, {"model": "glm-4.6", "base_url": "https://a/v1", "api_key": "new-key"}, connection_id="my-glm")
    assert connections.secrets_of("my-glm") == {"api_key": "new-key"}


def test_a_required_argument_is_checked_by_its_real_name():
    with pytest.raises(ValueError, match="model is required"):
        connections.save("X", connections.LLM, CHAT_OPENAI, {"base_url": "https://a/v1"})
    with pytest.raises(ValueError, match="google_api_key is required"):
        connections.save("X", connections.LLM, CHAT_GEMINI, {"model": "gemini-2.5-flash"})
    with pytest.raises(ValueError, match="nickname"):
        connections.save("  ", connections.LLM, CHAT_OPENAI, {"model": "m", "base_url": "https://a/v1"})
    # An interface only serves the types it declares.
    with pytest.raises(ValueError, match="cannot serve"):
        connections.save("X", connections.COMPUTER_USE, CHAT_GEMINI, {"model": "gemini-2.5-flash", "google_api_key": "k"})
    with pytest.raises(ValueError, match="cannot serve"):
        connections.save("X", connections.EMBEDDING, CHAT_OPENAI, {"model": "m", "base_url": "https://a/v1"})


def test_arguments_that_are_not_the_interfaces_own_are_dropped():
    saved = connections.save("Ollama", connections.VLM, CHAT_OLLAMA, {"model": "llama3.2-vision", "base_url": "http://localhost:11434", "api_key": "nope", "made_up": "x"})

    assert saved.params == {"model": "llama3.2-vision", "base_url": "http://localhost:11434"}
    assert saved.secrets_set == ()


def test_two_connections_with_the_same_nickname_get_their_own_ids():
    first = connections.save("GLM", connections.LLM, CHAT_OPENAI, {"model": "m", "base_url": "https://a/v1"})
    second = connections.save("GLM", connections.LLM, CHAT_OPENAI, {"model": "m", "base_url": "https://b/v1"})
    assert [first.id, second.id] == ["glm", "glm-2"]
    assert connections.delete("glm-2") is True and connections.get("glm-2") is None


def test_a_secret_that_cannot_be_decrypted_here_reads_as_missing():
    _glm()
    with sqlite3.connect(connections.db_path()) as db:  # as if copied from another machine
        db.execute("UPDATE connections SET secrets = ? WHERE id = ?", (b"not-a-dpapi-blob", "my-glm"))

    assert connections.secrets_of("my-glm") == {}
    assert connections.get("my-glm").secrets_set == ()


def test_a_database_from_an_earlier_version_is_rebuilt_not_rejected():
    """The first schema had base_url/secret columns and no interface; its rows still open."""
    with sqlite3.connect(connections.db_path()) as db:
        db.execute(
            "CREATE TABLE connections (id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, "
            "base_url TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '', secret BLOB NOT NULL, created_at TEXT NOT NULL)"
        )
        db.executemany(
            "INSERT INTO connections (id, name, kind, base_url, model, secret, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                ("my-glm", "My GLM", "chat", "https://open.bigmodel.cn/api/paas/v4", "glm-4.5-flash", connections.protect("old-key"), "2026-01-01T00:00:00+00:00"),
                ("fara", "Colab FARA", "vision", "https://abc.ngrok-free.app/v1", "microsoft/Fara-7B", b"", "2026-01-02T00:00:00+00:00"),
            ],
        )

    listed = connections.list_connections()

    assert [(item.id, item.kind, item.interface) for item in listed] == [
        ("my-glm", connections.LLM, CHAT_OPENAI),
        ("fara", connections.COMPUTER_USE, CHAT_OPENAI),
    ]
    assert listed[0].params == {"model": "glm-4.5-flash", "base_url": "https://open.bigmodel.cn/api/paas/v4"}
    assert connections.secrets_of("my-glm") == {"api_key": "old-key"}  # the key it had is still readable
    # And the rebuilt table takes new rows (the old NOT NULL `secret` column is gone).
    assert connections.save("New", connections.LLM, CHAT_OPENAI, {"model": "m", "base_url": "https://a/v1"}).id == "new"


# --- building the client ---------------------------------------------------------------


def test_the_arguments_reach_langchain_under_their_own_names():
    built = connections.chat_model(_glm())

    assert isinstance(built, BaseChatModel)
    assert built.model_name == "glm-4.5-flash" and built.openai_api_base == "https://open.bigmodel.cn/api/paas/v4"
    assert built.openai_api_key.get_secret_value() == "secret-key"
    assert (built.temperature, built.max_tokens, built.request_timeout, built.max_retries) == (0.2, 2048, 45.0, 3)


def test_each_interface_builds_its_own_client():
    gemini = connections.chat_model(connections.save("G", connections.VLM, CHAT_GEMINI, {"model": "gemini-2.5-flash", "google_api_key": "google-key", "max_output_tokens": "900"}))
    assert gemini.max_output_tokens == 900

    ollama = connections.chat_model(connections.save("O", connections.COMPUTER_USE, CHAT_OLLAMA, {"model": "llama3.2-vision", "base_url": "http://localhost:11434", "num_predict": "512"}))
    assert (ollama.model, ollama.num_predict) == ("llama3.2-vision", 512)

    embeddings = connections.embeddings_model(
        connections.save("E", connections.EMBEDDING, EMBED_OPENAI, {"model": "nomic-embed-text", "base_url": "http://localhost:11434/v1", "check_embedding_ctx_length": "false"})
    )
    assert isinstance(embeddings, Embeddings) and embeddings.check_embedding_ctx_length is False

    # Hugging Face serves embeddings as feature extraction, so that is the task it is given.
    hugging = connections.embeddings_model(
        connections.save("HF", connections.EMBEDDING, EMBED_HF, {"model": "sentence-transformers/all-MiniLM-L6-v2", "huggingfacehub_api_token": "hf-token"})
    )
    assert isinstance(hugging, Embeddings) and (hugging.model, hugging.task) == ("sentence-transformers/all-MiniLM-L6-v2", "feature-extraction")

    with pytest.raises(TypeError, match="not a LangChain chat model"):
        connections.chat_model(connections.save("Wrong", connections.EMBEDDING, EMBED_OPENAI, {"model": "m", "base_url": "https://a/v1"}))


# --- the roles ------------------------------------------------------------------------


def test_a_role_only_takes_a_connection_of_a_type_it_can_use():
    _glm()
    connections.save("Seeing", connections.VLM, CHAT_OPENAI, {"model": "qwen2.5-vl", "base_url": "https://vl/v1"})
    connections.save("Colab FARA", connections.COMPUTER_USE, CHAT_OPENAI, {"model": "microsoft/Fara-7B", "base_url": "https://abc.ngrok-free.app/v1"})
    client = _client()

    assert client.post("/api/computer-use/models/planner", json={"connection": "colab-fara"}).status_code == 422  # computer use
    assert client.post("/api/computer-use/models/supervisor", json={"connection": "my-glm"}).status_code == 422  # an LLM cannot see
    assert client.post("/api/computer-use/models/vision", json={"connection": "my-glm"}).status_code == 422
    assert client.post("/api/computer-use/models/planner", json={"connection": "nope"}).status_code == 422
    assert client.post("/api/computer-use/models/planner", json={"connection": ""}).status_code == 422
    assert client.post("/api/computer-use/models/embeddings", json={"connection": ""}).status_code == 200

    assert client.post("/api/computer-use/models/planner", json={"connection": "my-glm"}).status_code == 200
    assert client.post("/api/computer-use/models/supervisor", json={"connection": "seeing"}).status_code == 200
    assert client.post("/api/computer-use/models/vision", json={"connection": "colab-fara"}).status_code == 200

    config = _config()
    assert models.planner_model(config) == "glm-4.5-flash"
    assert models.planner_sees_images(config) is False  # an LLM connection
    assert models.supervisor_sees_images(config) is True  # a VLM one
    assert models.vision_model(config)[1] == "microsoft/Fara-7B"
    assert models.role_info("planner", config).where == "open.bigmodel.cn"


def test_the_supervisor_can_follow_the_planner():
    connections.save("Seeing", connections.VLM, CHAT_OPENAI, {"model": "qwen2.5-vl", "base_url": "https://vl/v1"})
    client = _client()
    client.post("/api/computer-use/models/planner", json={"connection": "seeing"})
    client.post("/api/computer-use/models/supervisor", json={"connection": "planner"})

    assert models.supervisor_factory(_config())().model_name == "qwen2.5-vl"
    assert models.supervisor_sees_images(_config()) is True


def test_a_role_with_no_connection_says_so_instead_of_guessing():
    config = _config()

    assert models.choice("planner").connection == ""
    assert models.current_embeddings(config) is None
    assert models.vision_model(config) == (None, "")
    with pytest.raises(Exception, match="no model yet"):
        models.planner_factory(config)

    check = asyncio.run(models.check_role("planner", config))
    assert check.ok is False and "no connection" in check.detail


def test_a_connection_that_is_deleted_leaves_its_role_empty():
    _glm()
    client = _client()
    client.post("/api/computer-use/models/planner", json={"connection": "my-glm"})

    assert client.delete("/api/computer-use/connections/my-glm").status_code == 200
    assert client.delete("/api/computer-use/connections/my-glm").status_code == 422
    assert models.choice("planner").connection == ""


# --- what the screens are told ------------------------------------------------------------


def test_the_screen_gets_the_interfaces_with_their_parameters():
    listed = _client().get("/api/computer-use/models").json()

    assert [kind["id"] for kind in listed["kinds"]] == ["llm", "vlm", "computer_use", "embedding"]
    assert listed["role_kinds"] == {"planner": ["llm", "vlm"], "supervisor": ["vlm"], "vision": ["computer_use"], "embeddings": ["embedding"]}

    by_path = {item["path"]: item for item in listed["interfaces"]}
    assert set(by_path) == {
        CHAT_OPENAI,
        CHAT_OLLAMA,
        CHAT_GEMINI,
        "langchain_anthropic:ChatAnthropic",
        EMBED_OPENAI,
        "langchain_ollama:OllamaEmbeddings",
        "langchain_google_genai:GoogleGenerativeAIEmbeddings",
        EMBED_HF,
    }
    openai = {param["name"]: param for param in by_path[CHAT_OPENAI]["params"]}
    # The parameters are LangChain's own, and each says whether it must be filled in.
    assert list(openai) == ["model", "base_url", "api_key", "temperature", "max_tokens", "timeout", "max_retries"]
    assert openai["model"]["required"] is True and openai["base_url"]["required"] is False
    # The defaults are what the app has always used.
    assert openai["api_key"]["type"] == "secret"
    assert [openai[name]["default"] for name in ("temperature", "max_tokens", "timeout", "max_retries")] == ["0.0", "2048", "60", "1"]
    assert by_path[CHAT_OPENAI]["kinds"] == ["llm", "vlm", "computer_use"]
    assert by_path[CHAT_GEMINI]["kinds"] == ["llm", "vlm"]


def test_a_connection_is_added_and_read_back_through_the_api():
    client = _client()
    body = {"name": "My GLM", "kind": "llm", "interface": CHAT_OPENAI, "params": {"model": "glm-4.5-flash", "base_url": "https://a/v1", "api_key": "secret-key"}}

    assert client.post("/api/computer-use/connections", json=body).status_code == 200
    assert client.post("/api/computer-use/connections", json={**body, "params": {"base_url": "https://a/v1"}}).status_code == 422

    listed = client.get("/api/computer-use/connections").json()
    assert listed == [{"id": "my-glm", "name": "My GLM", "kind": "llm", "interface": CHAT_OPENAI, "params": {"model": "glm-4.5-flash", "base_url": "https://a/v1"}, "secrets_set": ["api_key"]}]
    assert "secret-key" not in str(listed)

    # An edit without the key keeps the stored one.
    assert client.post("/api/computer-use/connections?connection_id=my-glm", json={**body, "name": "GLM2", "params": {"model": "glm-4.6", "base_url": "https://a/v1"}}).status_code == 200
    assert connections.secrets_of("my-glm") == {"api_key": "secret-key"}
    assert connections.get("my-glm").name == "GLM2"
