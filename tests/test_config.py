import pytest

from app.config import ROOT, load_settings

ENV_EXAMPLE = ROOT / ".env.example"


def test_defaults_from_yaml_with_no_keys():
    s = load_settings(env_file=ENV_EXAMPLE, environ={})
    assert s.gate_adapter == "llm"
    assert s.window_utterances == 12
    assert s.max_session_cost_usd == 1.00
    assert s.store_audio is False
    assert set(s.models) == {"stt", "gate", "cue", "review"}
    assert not any(s.has_key(n) for n in s.secrets)


def test_env_file_overrides_yaml_and_process_env_overrides_env_file(tmp_path):
    env = tmp_path / ".env"
    env.write_text("GATE_MODEL=google:from-dotenv\nEXPERIMENT_LABEL=dotenv\nCUE_MODEL=openrouter:cue-x\n")
    s = load_settings(env_file=env, environ={"GATE_MODEL": "openai:from-process", "WINDOW_UTTERANCES": "8"})
    assert s.models["gate"] == "openai:from-process"
    assert s.model_for("gate") == ("openai", "from-process")
    assert s.models["cue"] == "openrouter:cue-x"
    assert s.experiment_label == "dotenv"
    assert s.window_utterances == 8


def test_secrets_are_not_printed(tmp_path):
    env = tmp_path / ".env"
    env.write_text("OPENROUTER_API_KEY=sk-secret-value\n")
    s = load_settings(env_file=env, environ={})
    assert s.has_key("OPENROUTER_API_KEY")
    assert "sk-secret-value" not in repr(s) and "sk-secret-value" not in s.model_dump_json()


def test_missing_role_is_rejected(tmp_path):
    y = tmp_path / "settings.yaml"
    y.write_text("models:\n  gate: google:x\n")
    with pytest.raises(ValueError, match="stt"):
        load_settings(settings_path=y, env_file=None, environ={})
