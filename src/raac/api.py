"""HTTP API: the only interface clients use (the web UI today, others later).

`POST /api/ask` takes a question and answers as a Server-Sent Events stream of JSON
events (`AskEvent`): `progress` while routing and searching, one `sentence` per grounded
sentence as soon as it is complete, then a final `answer` that is authoritative (the
client shows it in place of the streamed sentences) or an `error`. The event schema is
published in the OpenAPI document under `components.schemas.AskEvent`.

The pipeline is loaded once at startup. Runs are serialized: the pipeline's progress
callback and usage meter are per pipeline, not per request.
"""

import asyncio
import logging
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal, Protocol

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, TypeAdapter

from . import strings
from .answerer import LikelyParte, Sentence
from .pipeline import PipelineResult

log = logging.getLogger(__name__)

Progress = Callable[[str], None]


class AskPipeline(Protocol):
    def run(self, standalone_question: str, *, on_sentence: Callable[[Sentence], None] | None = None) -> PipelineResult: ...


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


class AnswerOut(BaseModel):
    """What a client shows. The gap line and dropped uncited sentences stay in logs."""

    sentences: list[Sentence]
    refused: bool  # nothing grounded: show the refusal and only likely Partes
    incomplete: bool  # grounded, but part of the question is not covered
    likely_partes: list[LikelyParte]


class ProgressEvent(BaseModel):
    type: Literal["progress"] = "progress"
    message: str


class SentenceEvent(BaseModel):
    type: Literal["sentence"] = "sentence"
    sentence: Sentence


class AnswerEvent(BaseModel):
    type: Literal["answer"] = "answer"
    answer: AnswerOut


class ErrorEvent(BaseModel):
    type: Literal["error"] = "error"
    message: str


AskEvent = Annotated[ProgressEvent | SentenceEvent | AnswerEvent | ErrorEvent, Field(discriminator="type")]
_EVENT = TypeAdapter(AskEvent)


class _Runner:
    """Owns the pipeline; forwards its progress to whichever request is running."""

    def __init__(self) -> None:
        self.pipeline: AskPipeline | None = None
        self._lock = threading.Lock()
        self._sink: Progress | None = None

    def progress(self, message: str) -> None:
        sink = self._sink
        if sink is None:
            log.info(message)  # startup indexing
        else:
            sink(message)

    def run(self, question: str, emit: Callable[[Any], None]) -> PipelineResult:
        assert self.pipeline is not None
        with self._lock:
            self._sink = lambda message: emit(ProgressEvent(message=message))
            try:
                return self.pipeline.run(question, on_sentence=lambda s: emit(SentenceEvent(sentence=s)))
            finally:
                self._sink = None


def create_app(load_pipeline: Callable[[Progress], AskPipeline]) -> FastAPI:
    """`load_pipeline(on_progress)` builds the pipeline at startup, wiring progress to the API."""
    runner = _Runner()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        runner.pipeline = await asyncio.to_thread(load_pipeline, runner.progress)
        yield

    app = FastAPI(title="raac-rag", lifespan=lifespan)

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post(
        "/api/ask",
        response_class=StreamingResponse,
        responses={
            200: {
                "description": "Server-Sent Events; each `data:` line is one AskEvent as JSON.",
                "content": {"text/event-stream": {"schema": {"$ref": "#/components/schemas/AskEvent"}}},
            }
        },
    )
    async def ask(request: AskRequest) -> StreamingResponse:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[BaseModel | None] = asyncio.Queue()

        def emit(event: BaseModel | None) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, event)

        def work() -> None:
            try:
                answer = runner.run(request.question, emit).answer
                emit(
                    AnswerEvent(
                        answer=AnswerOut(
                            sentences=answer.sentences,
                            refused=answer.refused,
                            incomplete=answer.incomplete,
                            likely_partes=answer.likely_partes,
                        )
                    )
                )
            except Exception:
                log.exception("ask failed")
                emit(ErrorEvent(message=strings.API_ERROR))
            finally:
                emit(None)

        loop.run_in_executor(None, work)

        async def events() -> AsyncIterator[str]:
            while (event := await queue.get()) is not None:
                yield f"data: {event.model_dump_json()}\n\n"

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
            event_schema = _EVENT.json_schema(ref_template="#/components/schemas/{model}")
            components = schema.setdefault("components", {}).setdefault("schemas", {})
            components.update(event_schema.pop("$defs", {}))
            components["AskEvent"] = event_schema
            app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = openapi
    return app

