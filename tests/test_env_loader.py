import os

from db_agent.env_loader import load_dotenv
from db_agent.provider_config import env_value_for


def test_load_dotenv_supports_exact_provider_names(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text(
        "groq_api_key=groq-secret\n"
        "ollama-cloud_api_key='ollama-secret'\n"
        "# ignored\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("groq_api_key", raising=False)
    monkeypatch.delenv("ollama-cloud_api_key", raising=False)
    load_dotenv(path)
    assert os.environ["groq_api_key"] == "groq-secret"
    assert os.environ["ollama-cloud_api_key"] == "ollama-secret"


def test_existing_environment_wins_over_dotenv(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("groq_api_key=dotenv-secret\n", encoding="utf-8")
    monkeypatch.setenv("groq_api_key", "process-secret")
    load_dotenv(path)
    assert env_value_for("groq") == ("groq_api_key", "process-secret")
