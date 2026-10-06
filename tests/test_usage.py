"""Tokens and cost per stage: pricing, the routing/answer usage, every turn of a stubbed PageIndex agent run
during search, and their totals in the eval report (no live LLM call)."""

import asyncio
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from conftest import FIXTURES
from raac.answerer import Answer
from raac.evals import load_cases, run_eval, summarize
from raac.indexer import ParteIndex, attach_secciones
from raac.pipeline import IndexVersion, PipelineResult, SeccionRef
from raac.retriever import Retriever
from raac.router import ParteRouter
from raac.usage import Price, StageUsage, UsageMeter, load_prices, merge, price_for, total_cost

PRICES = {"claude-opus-5-5": Price(input=4, cache_write=5, cache_read=0.2, output=20), "claude-haiku-4-5": Price(1, 1.25, 0.1, 5)}


def test_price_table_covers_every_configured_model(monkeypatch):
    from raac.config import load_models

    for stage in ("indexing", "routing", "search", "answer"):
        monkeypatch.delenv(f"RAAC_MODEL_{stage.upper()}", raising=False)  # models.toml, not an override
    prices = load_prices()
    models = load_models()
    for model in (models.routing, models.search, models.answer):
        assert price_for(model, prices) is not None, model


def test_price_lookup_ignores_route_prefix_and_snapshot_date():
    assert price_for("anthropic/claude-opus-5-5", PRICES) is PRICES["claude-opus-5-5"]
    assert price_for("claude-haiku-4-5-20251001", PRICES) is PRICES["claude-haiku-4-5"]
    assert price_for("claude-unknown", PRICES) is None


def test_cost_prices_each_token_kind_at_its_own_rate():
    u = StageUsage()
    u.add("claude-opus-5-5", PRICES, input_tokens=1_000_000, cache_creation_input_tokens=1_000_000,
          cache_read_input_tokens=1_000_000, output_tokens=1_000_000)
    assert u.cost_usd == pytest.approx(4 + 5 + 0.2 + 20)


def test_unpriced_model_makes_cost_unknown_not_zero():
    u = StageUsage()
    u.add("claude-opus-5-5", PRICES, input_tokens=10)
    u.add("mystery-model", PRICES, input_tokens=10)
    assert u.cost_usd is None and u.unpriced_models == ["mystery-model"] and u.input_tokens == 20
    assert total_cost({"search": u, "answer": StageUsage()}) is None
    assert merge([u, StageUsage()]).cost_usd is None


def test_routing_records_usage(tmp_path):
    from test_router import CARDS, fake_claude

    meter = UsageMeter(PRICES)
    ParteRouter(fake_claude('{"partes": ["61"]}'), "claude-haiku-4-5", CARDS, meter=meter).route("¿VFR?")
    routing = meter.take()["routing"]
    assert (routing.requests, routing.input_tokens, routing.output_tokens) == (1, 900, 12)
    assert routing.cost_usd == pytest.approx((900 * 1 + 12 * 5) / 1e6)


def test_answer_usage_prices_every_fallback_attempt_at_its_own_model():
    meter = UsageMeter(PRICES)
    response = {
        "model": "claude-haiku-4-5",  # served by the fallback
        "usage": {
            "input_tokens": 100, "output_tokens": 50,
            "iterations": [
                {"type": "message", "model": None, "input_tokens": 1000, "output_tokens": 10},
                {"type": "fallback_message", "model": "claude-haiku-4-5", "input_tokens": 100, "output_tokens": 50},
            ],
        },
    }
    meter.record_anthropic("answer", response, requested_model="claude-opus-5-5")
    answer = meter.take()["answer"]
    assert answer.requests == 1
    assert (answer.input_tokens, answer.output_tokens) == (1100, 60)
    assert answer.models == ["claude-opus-5-5", "claude-haiku-4-5"]
    assert answer.cost_usd == pytest.approx((1000 * 4 + 10 * 20 + 100 * 1 + 50 * 5) / 1e6)


# Search: a stubbed PageIndex agent run. The stub runs a real openai-agents agent on LiteLLM, as
# PageIndex's local chat does (its own event loop on a background thread, yielding only tool-call
# events, never usage), against a loopback fake of the Anthropic Messages API that streams one
# recorded-shape SSE response per turn: two get_page_content calls, then the answer.

# Per turn: Anthropic usage (input_tokens excludes cache), output_tokens
TURNS = [
    ({"input_tokens": 100, "cache_creation_input_tokens": 5_000, "cache_read_input_tokens": 0}, 40),
    ({"input_tokens": 150, "cache_creation_input_tokens": 1_200, "cache_read_input_tokens": 5_000}, 35),
    ({"input_tokens": 300, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 6_200}, 120),
]


def _sse(n: int, usage: dict, output_tokens: int, pages: str | None) -> bytes:
    events = [{"type": "message_start", "message": {
        "id": f"msg_{n}", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "content": [],
        "stop_reason": None, "stop_sequence": None, "usage": {**usage, "output_tokens": 1}}}]
    if pages:
        block = {"type": "tool_use", "id": f"toolu_{n}", "name": "get_page_content", "input": {}}
        delta = {"type": "input_json_delta", "partial_json": json.dumps({"pages": pages})}
    else:
        block, delta = {"type": "text", "text": ""}, {"type": "text_delta", "text": "Listo."}
    events += [
        {"type": "content_block_start", "index": 0, "content_block": block},
        {"type": "content_block_delta", "index": 0, "delta": delta},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use" if pages else "end_turn", "stop_sequence": None},
         "usage": {"output_tokens": output_tokens}},
        {"type": "message_stop"},
    ]
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


@pytest.fixture
def fake_anthropic(monkeypatch):
    """Loopback Anthropic endpoint; each request gets the next turn of the current agent run."""
    turns: list[bytes] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["content-length"]))
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            self.wfile.write(turns.pop(0))

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.undo()  # lift the no-network guard for loopback only
    real_connect = socket.socket.connect

    def loopback_only(sock, address, *args):
        if address[0] != "127.0.0.1":
            raise RuntimeError(f"test tried a live network call to {address!r}")
        return real_connect(sock, address, *args)

    monkeypatch.setattr(socket.socket, "connect", loopback_only)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    yield turns
    server.shutdown()


class StubbedAgentPageIndex:
    """Stands in for PageIndexClient.chat(stream=True): an agent run whose events carry no usage."""

    def __init__(self, turns: list[bytes], pages_by_doc: dict[str, str]):
        self.turns = turns
        self.pages_by_doc = pages_by_doc

    def chat(self, question, doc_id, stream, instructions):
        pages = self.pages_by_doc[doc_id]
        self.turns += [_sse(n, usage, out, pages if n < len(TURNS) - 1 else None) for n, (usage, out) in enumerate(TURNS)]
        return SimpleNamespace(events=self._run(question, pages))

    def _run(self, question, pages):
        from agents import Agent, ModelSettings, RunConfig, Runner, function_tool
        from agents.extensions.models.litellm_model import LitellmModel

        @function_tool
        def get_page_content(pages: str) -> str:
            """Text of the given PDF pages."""
            return "texto"

        agent = Agent(name="PageIndex", instructions="x", tools=[get_page_content],
                      model=LitellmModel("anthropic/claude-opus-5-5"),
                      model_settings=ModelSettings(include_usage=True, max_tokens=512))
        events = []

        async def run():
            streamed = Runner.run_streamed(agent, input=question, run_config=RunConfig(tracing_disabled=True))
            async for event in streamed.stream_events():
                if event.type == "run_item_stream_event" and event.name == "tool_called":
                    events.append({"type": "tool_call", "name": event.item.raw_item.name,
                                   "arguments": event.item.raw_item.arguments})

        thread = threading.Thread(target=lambda: asyncio.run(run()))
        thread.start()
        thread.join()
        yield from events


def _index(parsed) -> ParteIndex:
    tree = [{"title": "Todo", "node_id": "0000", "start_index": 1, "end_index": parsed.page_count}]
    return ParteIndex(parsed.code, parsed.content_hash, f"doc-{parsed.code}", attach_secciones(tree, parsed))


def test_search_usage_includes_every_turn_of_the_agent(fake_anthropic, parte61, parte91_sample):
    meter = UsageMeter(PRICES)
    partes = [(p, _index(p)) for p in (parte61, parte91_sample)]
    router = SimpleNamespace(route=lambda q: ["61", "91"])
    pageindex = StubbedAgentPageIndex(fake_anthropic, {"doc-61": "67", "doc-91": "6"})
    retrieval = Retriever(pageindex, partes, router=router, meter=meter).retrieve("¿VFR?")
    assert "61:61.535" in [f"{s.parte.code}:{s.seccion.id}" for s in retrieval.secciones]  # read pages still map
    assert not fake_anthropic  # every recorded turn was served

    search = meter.take()["search"]
    runs = 2  # one agent run per routed Parte
    total = {k: runs * sum(u[k] for u, _ in TURNS) for k in TURNS[0][0]}
    assert search.requests == runs * len(TURNS)
    assert search.unmetered_requests == 0
    assert search.input_tokens == total["input_tokens"]
    assert search.cache_creation_input_tokens == total["cache_creation_input_tokens"]
    assert search.cache_read_input_tokens == total["cache_read_input_tokens"]
    assert search.output_tokens == runs * sum(out for _, out in TURNS)
    assert search.models == ["claude-opus-5-5"]
    expected = (total["input_tokens"] * 4 + total["cache_creation_input_tokens"] * 5
                + total["cache_read_input_tokens"] * 0.2 + search.output_tokens * 20) / 1e6
    assert search.cost_usd == pytest.approx(expected)


def test_agent_turns_outside_a_search_are_not_counted(fake_anthropic, parte61):
    meter = UsageMeter(PRICES)
    pageindex = StubbedAgentPageIndex(fake_anthropic, {"doc-61": "67"})
    Retriever(pageindex, [(parte61, _index(parte61))]).retrieve("¿VFR?")  # no meter: nothing recorded
    Retriever(pageindex, [(parte61, _index(parte61))], meter=UsageMeter(PRICES)).retrieve("¿VFR?")
    assert meter.take()["search"].requests == 0


# The eval report


def _usage(routing, search, answer):
    stages = {}
    for name, (model, tokens_in, tokens_out) in {"routing": routing, "search": search, "answer": answer}.items():
        stages[name] = StageUsage()
        stages[name].add(model, PRICES, input_tokens=tokens_in, output_tokens=tokens_out)
    return stages


class CostlyPipeline:
    name = "fake"

    def models(self):
        return {"answer": "claude-opus-5-5"}

    def index_versions(self):
        return [IndexVersion("61", "abc123", "VI", "I")]

    def run(self, standalone_question):
        usage = _usage(("claude-haiku-4-5", 1_000, 10), ("claude-opus-5-5", 50_000, 500), ("claude-opus-5-5", 10_000, 1_000))
        if "noche" not in standalone_question:
            e = RuntimeError("answer call failed")
            e.usage = usage | {"answer": StageUsage()}
            raise e
        return PipelineResult(routed_partes=["61"], retrieved_secciones=[SeccionRef("61", "61.535")],
                              answer=Answer(sentences=[], refused=True), usage=usage)


@pytest.fixture
def cost_report(tmp_path):
    cases = load_cases(FIXTURES.parent.parent / "evals" / "cases.jsonl")
    ok = next(c for c in cases if "noche" in c.question)
    failed = next(c for c in cases if "noche" not in c.question)
    return run_eval(CostlyPipeline(), [ok, failed])


ROUTING = (1_000 * 1 + 10 * 5) / 1e6
SEARCH = (50_000 * 4 + 500 * 20) / 1e6
ANSWER = (10_000 * 4 + 1_000 * 20) / 1e6


def test_each_case_has_tokens_and_cost_per_stage(cost_report):
    ok, failed = cost_report["cases"]
    assert ok["usage"]["search"]["input_tokens"] == 50_000
    assert ok["usage"]["answer"]["output_tokens"] == 1_000
    assert ok["cost_usd"] == pytest.approx(ROUTING + SEARCH + ANSWER)
    assert failed["error"]  # a case that raised still reports what it spent
    assert failed["cost_usd"] == pytest.approx(ROUTING + SEARCH)


def test_aggregate_has_per_stage_and_total_cost(cost_report):
    agg = cost_report["aggregate"]
    assert list(agg["usage"]) == ["routing", "search", "answer"]
    assert agg["usage"]["routing"]["cost_usd"] == pytest.approx(2 * ROUTING)
    assert agg["usage"]["search"]["cost_usd"] == pytest.approx(2 * SEARCH)
    assert agg["usage"]["answer"]["cost_usd"] == pytest.approx(ANSWER)
    assert agg["usage"]["search"]["requests"] == 2
    assert agg["cost_usd"] == pytest.approx(2 * ROUTING + 2 * SEARCH + ANSWER)
    assert agg["cost_usd_mean"] == pytest.approx(agg["cost_usd"] / 2)
    json.dumps(cost_report)  # still JSON-serializable


def test_summary_prints_total_cost_and_cost_per_stage(cost_report):
    text = summarize(cost_report)
    assert f"total cost: ${2 * ROUTING + 2 * SEARCH + ANSWER:.4f}" in text
    for stage, cost in (("routing", 2 * ROUTING), ("search", 2 * SEARCH), ("answer", ANSWER)):
        line = next(l for l in text.splitlines() if l.startswith(stage + " "))
        assert f"${cost:.4f}" in line
