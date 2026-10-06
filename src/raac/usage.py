"""Tokens and USD cost per pipeline stage (routing, search, answer) for one exchange.

Routing and the Answer call Claude directly, so their usage comes from the Messages API
response. Tree search is PageIndex's own agent (openai-agents over LiteLLM) calling Claude over
several turns; its events carry no usage, so while a search runs every model turn the agent
takes is recorded from the openai-agents LitellmModel it runs on. Cost is computed here from
token counts and prices.toml, never taken from a provider's or LiteLLM's own price table.
"""

import re
import threading
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

STAGES = ("routing", "search", "answer")
_PRICES_FILE = Path(__file__).with_name("prices.toml")
_DATE_SUFFIX = re.compile(r"-\d{8}$")


@dataclass(frozen=True)
class Price:
    """USD per million tokens."""

    input: float
    cache_write: float
    cache_read: float
    output: float


def load_prices(path: Path = _PRICES_FILE) -> dict[str, Price]:
    return {model: Price(**rates) for model, rates in tomllib.loads(path.read_text()).items()}


def price_for(model: str, prices: dict[str, Price]) -> Price | None:
    """Price by model ID, ignoring a LiteLLM route prefix ("anthropic/") and a snapshot date suffix."""
    model = model.rsplit("/", 1)[-1]
    return prices.get(model) or prices.get(_DATE_SUFFIX.sub("", model))


@dataclass
class StageUsage:
    requests: int = 0
    input_tokens: int = 0  # uncached input
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = 0.0  # None when a call's model has no price or a call went unmetered
    models: list[str] = field(default_factory=list)
    unpriced_models: list[str] = field(default_factory=list)
    unmetered_requests: int = 0  # calls whose usage never arrived; their tokens are missing above

    def add(
        self,
        model: str,
        prices: dict[str, Price],
        input_tokens: int = 0,
        cache_creation_input_tokens: int = 0,
        cache_read_input_tokens: int = 0,
        output_tokens: int = 0,
        requests: int = 1,
    ) -> None:
        self.requests += requests
        self.input_tokens += input_tokens
        self.cache_creation_input_tokens += cache_creation_input_tokens
        self.cache_read_input_tokens += cache_read_input_tokens
        self.output_tokens += output_tokens
        if model not in self.models:
            self.models.append(model)
        price = price_for(model, prices)
        if price is None:
            if model not in self.unpriced_models:
                self.unpriced_models.append(model)
            self.cost_usd = None
        elif self.cost_usd is not None:
            self.cost_usd += (
                input_tokens * price.input
                + cache_creation_input_tokens * price.cache_write
                + cache_read_input_tokens * price.cache_read
                + output_tokens * price.output
            ) / 1_000_000

    def add_unmetered(self, count: int) -> None:
        self.requests += count
        self.unmetered_requests += count
        self.cost_usd = None


def merge(usages: list[StageUsage]) -> StageUsage:
    """Sum usages, e.g. one stage across eval cases; an unknown cost anywhere makes the sum unknown."""
    total = StageUsage()
    for u in usages:
        total.requests += u.requests
        total.input_tokens += u.input_tokens
        total.cache_creation_input_tokens += u.cache_creation_input_tokens
        total.cache_read_input_tokens += u.cache_read_input_tokens
        total.output_tokens += u.output_tokens
        total.unmetered_requests += u.unmetered_requests
        total.cost_usd = None if total.cost_usd is None or u.cost_usd is None else total.cost_usd + u.cost_usd
        total.models += [m for m in u.models if m not in total.models]
        total.unpriced_models += [m for m in u.unpriced_models if m not in total.unpriced_models]
    return total


def total_cost(usage: dict[str, StageUsage]) -> float | None:
    """Cost over all stages; None when any is unknown, or when nothing was metered at all."""
    costs = [u.cost_usd for u in usage.values()]
    return None if not costs or None in costs else sum(costs)


class UsageMeter:
    """Collects usage per stage for one exchange at a time: `take()` returns it and starts over."""

    def __init__(self, prices: dict[str, Price] | None = None):
        self.prices = load_prices() if prices is None else prices
        self._lock = threading.Lock()
        self._stages: dict[str, StageUsage] = {}

    def record_anthropic(self, stage: str, response: dict[str, Any], requested_model: str | None = None) -> None:
        """Add one Messages API response (as JSON); its usage counts uncached input separately from cache.

        With server-side fallbacks, top-level usage covers only the attempt that produced the message;
        `usage.iterations` lists every attempt, each billed at its own model's rates, so price those.
        An attempt without a model is the requested one.
        """
        usage = response.get("usage") or {}
        served = response.get("model") or "unknown"
        attempts = usage.get("iterations") or [usage]
        with self._lock:
            stage_usage = self._stages.setdefault(stage, StageUsage())
            for n, it in enumerate(attempts):
                stage_usage.add(
                    it.get("model") or requested_model or served,
                    self.prices,
                    input_tokens=it.get("input_tokens") or 0,
                    cache_creation_input_tokens=it.get("cache_creation_input_tokens") or 0,
                    cache_read_input_tokens=it.get("cache_read_input_tokens") or 0,
                    output_tokens=it.get("output_tokens") or 0,
                    requests=int(n == 0),  # one API call, however many attempts it made
                )

    def record_agent_turn(self, stage: str, model: str, usage: Any) -> None:
        """Add one openai-agents model turn. Its input_tokens include cache reads and writes; split them out."""
        if usage is None:
            self.record_unmetered(stage, 1)
            return
        details = getattr(usage, "input_tokens_details", None)
        cache_read = getattr(details, "cached_tokens", 0) or 0
        cache_write = getattr(details, "cache_write_tokens", 0) or 0
        with self._lock:
            self._stages.setdefault(stage, StageUsage()).add(
                model.rsplit("/", 1)[-1],  # LiteLLM route prefix ("anthropic/") off: the model ID
                self.prices,
                input_tokens=max((getattr(usage, "input_tokens", 0) or 0) - cache_read - cache_write, 0),
                cache_creation_input_tokens=cache_write,
                cache_read_input_tokens=cache_read,
                output_tokens=getattr(usage, "output_tokens", 0) or 0,
            )

    def record_unmetered(self, stage: str, count: int) -> None:
        with self._lock:
            self._stages.setdefault(stage, StageUsage()).add_unmetered(count)

    def take(self) -> dict[str, StageUsage]:
        with self._lock:
            stages, self._stages = self._stages, {}
        return {name: stages.get(name, StageUsage()) for name in STAGES} | stages

    @contextmanager
    def agent_turns(self, stage: str):
        """Record every model turn PageIndex's agent takes inside the block under `stage`.

        The agent runs on its own thread and event loop, so the recording target is process-wide:
        one metered block at a time.
        """
        _install_agent_hook()
        global _sink
        with _sink_lock:
            if _sink is not None:
                raise RuntimeError("agent turns are already being metered")
            _sink = (self, stage)
        try:
            yield
        finally:
            with _sink_lock:
                _sink = None


_sink: tuple[UsageMeter, str] | None = None
_sink_lock = threading.Lock()


def _record_turn(model: str, usage: Any) -> None:
    with _sink_lock:
        sink = _sink
    if sink is not None:
        meter, stage = sink
        meter.record_agent_turn(stage, model, usage)


def _install_agent_hook() -> None:
    """Wrap LitellmModel, the openai-agents model PageIndex's agent calls Claude through, so each
    turn's usage is recorded as it completes: inline in the stream, before the run can end."""
    from agents.extensions.models.litellm_model import LitellmModel

    if getattr(LitellmModel, "_raac_metered", False):
        return
    stream_response = LitellmModel.stream_response
    get_response = LitellmModel.get_response

    async def metered_stream_response(self, *args, **kwargs):
        async for event in stream_response(self, *args, **kwargs):
            if getattr(event, "type", None) == "response.completed":
                _record_turn(self.model, event.response.usage)
            yield event

    async def metered_get_response(self, *args, **kwargs):
        response = await get_response(self, *args, **kwargs)
        _record_turn(self.model, response.usage)
        return response

    LitellmModel.stream_response = metered_stream_response
    LitellmModel.get_response = metered_get_response
    LitellmModel._raac_metered = True
