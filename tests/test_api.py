"""HTTP API: the event stream of POST /api/ask and its published schema (fake pipeline, no live LLM)."""

import json

from fastapi.testclient import TestClient

from raac import strings
from raac.answerer import Answer, Citation, Sentence
from raac.api import create_app
from raac.pipeline import PipelineResult, SeccionRef

URL = "https://docs.anac.gob.ar/index.php/s/PtMG8j8sFeRyren"
CITATION = Citation("61", "61.535", "t", 67, 67, "10", "10", "VI", "I", "mayo 2026", URL, "texto")


class FakePipeline:
    def __init__(self, on_progress, fail=False):
        self._progress = on_progress
        self._fail = fail

    def run(self, standalone_question, *, on_sentence=None):
        self._progress("Buscando en Parte 61...")
        if self._fail:
            raise RuntimeError("boom")
        first = Sentence("Sí.", [CITATION])
        on_sentence(first)
        answer = Answer(
            [first, Sentence("Hasta 2027.", [CITATION])],
            refused=False,
            gap="SIN RESPALDO: nada",
            dropped=["Sin cita."],
        )
        return PipelineResult(["61"], [SeccionRef("61", "61.535")], answer)


def _events(app, question="¿Puedo volar VFR de noche?"):
    with TestClient(app) as client:
        response = client.post("/api/ask", json={"question": question})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    return [json.loads(line.removeprefix("data: ")) for line in response.text.splitlines() if line.startswith("data: ")]


def test_ask_streams_progress_sentences_then_the_answer():
    events = _events(create_app(FakePipeline))
    assert [e["type"] for e in events] == ["progress", "sentence", "answer"]
    assert events[0]["message"] == "Buscando en Parte 61..."
    assert events[1]["sentence"]["text"] == "Sí."
    assert events[1]["sentence"]["citations"][0]["seccion"] == "61.535"
    answer = events[2]["answer"]
    assert [s["text"] for s in answer["sentences"]] == ["Sí.", "Hasta 2027."]
    assert answer["refused"] is False
    assert "gap" not in answer and "dropped_uncited" not in answer  # logs only, never shown


def test_a_failed_run_ends_with_a_spanish_error_event():
    events = _events(create_app(lambda p: FakePipeline(p, fail=True)))
    assert events[-1] == {"type": "error", "message": strings.API_ERROR}


def test_question_is_validated():
    with TestClient(create_app(FakePipeline)) as client:
        assert client.post("/api/ask", json={"question": ""}).status_code == 422


def test_openapi_publishes_the_event_schema():
    with TestClient(create_app(FakePipeline)) as client:
        schema = client.get("/openapi.json").json()
    content = schema["paths"]["/api/ask"]["post"]["responses"]["200"]["content"]
    assert content["text/event-stream"]["schema"] == {"$ref": "#/components/schemas/AskEvent"}
    components = schema["components"]["schemas"]
    assert {"AskEvent", "ProgressEvent", "SentenceEvent", "AnswerEvent", "ErrorEvent", "Citation"} <= set(components)
    assert components["AskEvent"]["discriminator"]["propertyName"] == "type"
