"""Settings loader. Override order (later wins): settings.yaml, then .env, then process env."""
import os
from pathlib import Path
from typing import Literal

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, SecretStr

ROOT = Path(__file__).resolve().parent.parent
ROLES = ("stt", "gate", "cue", "review")
SECRET_NAMES = ("ASSEMBLYAI_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY")


class Limits(BaseModel):
    """Provider rate limits (M10a). requests_per_min: per model, keyed "provider" (default for its models) or
    "provider:model"; 0 or absent = unlimited. deadline_s: total time a call may take, retries and waits included."""
    requests_per_min: dict[str, int] = {"openrouter": 18}
    deadline_s: dict[str, float] = {"gate": 8, "cue": 8, "review": 60, "batch": 60}
    max_attempts: int = 4
    backoff_start_s: float = 1.0
    backoff_cap_s: float = 8.0


class Settings(BaseModel):
    models: dict[str, str]
    gate_adapter: Literal["llm", "jev", "hybrid"] = "llm"
    hybrid_min_confidence: float = 0.9
    gate_prompt: Literal["full", "short"] = "full"
    gate_temperature: float | None = None  # None = provider default (Google recommends 1.0 for Gemini 3)
    jev_model: str = "typesafe/jev-1.13"
    jev_min_probability: float = 0.6
    jev_questions: Literal["choice", "yesno", "atomic"] = "choice"
    jev_followup_probability: float = 0.6
    experiment_label: str = "default"
    window_utterances: int = 12
    cue_cooldown_seconds: int = 60
    cue_hold_suggested: bool = True  # a suggested card waits until the interviewee's answer is over
    cue_hold_s: float = 4.0          # ... = the user's next line, or this many seconds without a new line
    cue_reasoning_effort: str | None = "minimal"  # Gemini 3 cannot turn reasoning off; null = not sent
    stt_speaker_labels: bool = True
    stt_max_speakers: int = 3
    stt_split_turns: bool = True  # split a final turn where AssemblyAI's word-level speaker label changes
    store_audio: bool = False
    limits: Limits = Limits()
    max_session_cost_usd: float = 1.00
    pack: str = "packs/cps_interview_v2.yaml"
    secrets: dict[str, SecretStr] = {}

    def model_for(self, role: str) -> tuple[str, str]:
        """Return (provider, model) for a role, from a 'provider:model' string."""
        provider, _, model = self.models[role].partition(":")
        return provider, model

    def has_key(self, name: str) -> bool:
        s = self.secrets.get(name)
        return bool(s and s.get_secret_value())


def load_settings(
    settings_path: str | Path = ROOT / "config" / "settings.yaml",
    env_file: str | Path | None = ROOT / ".env",
    environ: dict[str, str] | None = None,
) -> Settings:
    data = yaml.safe_load(Path(settings_path).read_text(encoding="utf-8")) or {}
    data["models"] = dict(data.get("models") or {})
    missing = [r for r in ROLES if r not in data["models"]]
    if missing:
        raise ValueError(f"settings.yaml is missing models for roles: {', '.join(missing)}")

    overrides: dict[str, str] = {}
    if env_file and Path(env_file).exists():
        overrides.update({k: v for k, v in dotenv_values(env_file).items() if v is not None})
    overrides.update(os.environ if environ is None else environ)

    for role in ROLES:
        if v := overrides.get(f"{role.upper()}_MODEL"):
            data["models"][role] = v
    for name in Settings.model_fields:
        if name in ("models", "secrets"):
            continue
        if (v := overrides.get(name.upper())) not in (None, ""):
            data[name] = v
    data["secrets"] = {n: overrides.get(n, "") for n in SECRET_NAMES}
    return Settings.model_validate(data)
