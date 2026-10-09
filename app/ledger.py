"""Cost ledger: one JSONL row per model or speech call (SPEC 9)."""
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from app.config import ROOT

PRICES_PATH = ROOT / "config" / "prices.json"


class BudgetExceeded(RuntimeError):
    pass


class LedgerRow(BaseModel):
    ts: str
    session_id: str
    experiment: str
    pack: str = ""  # pack_id@version (M7)
    role: Literal["stt", "gate", "cue", "review"]
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    audio_seconds: float = 0.0
    cost_usd: float = 0.0
    cost_source: Literal["provider", "computed"] = "computed"
    latency_ms: int = 0
    ok: bool = True
    error: str | None = None
    status: int | None = None   # HTTP status of a failed attempt (M10a)
    attempt: int = 1
    waited_s: float = 0.0       # queue wait for the request limit plus retry waits before this attempt


def load_prices(path: str | Path = PRICES_PATH) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def compute_cost(prices: dict, provider: str, model: str, input_tokens: int = 0,
                 output_tokens: int = 0, audio_seconds: float = 0.0) -> float:
    if not (input_tokens or output_tokens or audio_seconds):
        return 0.0  # failed or free calls need no price entry
    p = prices.get(f"{provider}:{model}")
    if p is None:
        raise KeyError(f"No price entry for {provider}:{model} in prices.json")
    return (input_tokens * p.get("input_usd_per_mtok", 0.0) / 1e6
            + output_tokens * p.get("output_usd_per_mtok", 0.0) / 1e6
            + audio_seconds * p.get("audio_usd_per_hour", 0.0) / 3600)


class Ledger:
    def __init__(self, session_dir: Path, session_id: str, experiment: str,
                 max_cost_usd: float, prices: dict | None = None, pack: str = ""):
        self.path = Path(session_dir) / "ledger.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.session_id, self.experiment, self.max_cost_usd, self.pack = session_id, experiment, max_cost_usd, pack
        self.prices = load_prices() if prices is None else prices
        self.total_usd = 0.0
        self.rows: list[LedgerRow] = []
        self.on_record = None  # optional callback(row), e.g. to update the experiment bar

    def check_budget(self) -> None:
        """Call before every paid call. Raises once the session total passes the cap."""
        if self.total_usd > self.max_cost_usd:
            raise BudgetExceeded(
                f"Session cost ${self.total_usd:.4f} passed the cap of ${self.max_cost_usd:.2f}. "
                "Paid calls are stopped for this session.")

    def record(self, role: str, provider: str, model: str, *, input_tokens: int = 0,
               output_tokens: int = 0, audio_seconds: float = 0.0, provider_cost_usd: float | None = None,
               latency_ms: int = 0, ok: bool = True, error: str | None = None, status: int | None = None,
               attempt: int = 1, waited_s: float = 0.0) -> LedgerRow:
        if provider_cost_usd is not None:
            cost, source = provider_cost_usd, "provider"
        else:
            cost, source = compute_cost(self.prices, provider, model, input_tokens, output_tokens,
                                        audio_seconds), "computed"
        row = LedgerRow(
            ts=datetime.now(timezone.utc).isoformat(), session_id=self.session_id,
            experiment=self.experiment, pack=self.pack, role=role, provider=provider, model=model,
            input_tokens=input_tokens, output_tokens=output_tokens, audio_seconds=audio_seconds,
            cost_usd=cost, cost_source=source, latency_ms=latency_ms, ok=ok, error=error, status=status,
            attempt=attempt, waited_s=waited_s)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(row.model_dump_json() + "\n")
        self.total_usd += cost
        self.rows.append(row)
        if self.on_record is not None:
            self.on_record(row)
        return row
