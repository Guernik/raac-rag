"""PageIndex Cloud pipeline: tag -> Citation mapping, eval report, key check (fake Cloud client, no network).

The Cloud responses here are hand-built in the shapes the pageindex SDK documents
(`chat_completions` envelope, `get_citations` entries); no live Cloud response was recorded.
"""

import pytest

from conftest import FIXTURES
from raac import cli, cloud, corpus
from raac.cloud import CloudDoc, CloudKeyMissing, CloudPipeline, build_cloud_pipeline, map_answer
from raac.evals import load_cases, run_eval
from raac.pipeline import SeccionRef

URL = "https://docs.anac.gob.ar/index.php/s/PtMG8j8sFeRyren"
SEED = FIXTURES.parent.parent / "evals" / "cases.jsonl"
DOC = "pi-61"
BLOCK_61535 = {
    "document": "raac-61.pdf",
    "doc_id": DOC,
    "page": 67,
    "block_id": "p67_text_4",
    "bbox": [80, 420, 920, 520],
    "block_type": "text",
    "text": "El titular de una licencia de Piloto Privado de Avión que no haya recibido instrucción en vuelo VFR nocturno",
}
PAGE_67 = {"document": "raac-61.pdf", "doc_id": DOC, "page": 67}


@pytest.fixture
def docs(parte61):
    return {DOC: CloudDoc(DOC, parte61, URL)}


class FakeCloud:
    def __init__(self, text, resolved, model="pageindex-chat-x"):
        self.text = text
        self.resolved = resolved
        self.model = model
        self.calls = []

    def chat_completions(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return {"model": self.model, "choices": [{"message": {"role": "assistant", "content": self.text}}]}

    def get_citations(self, answer, doc_id=None):
        return self.resolved


def test_block_citation_maps_to_the_seccion_containing_the_block(docs):
    text = "Sí, pero hay un régimen transitorio para piloto privado sin VFR nocturno. <doc=raac-61.pdf;page=67;block=p67_text_4>"
    answer = map_answer(text, [BLOCK_61535], docs)
    assert not answer.refused
    [sentence] = answer.sentences
    [c] = sentence.citations
    assert (c.parte, c.seccion) == ("61", "61.535")
    assert (c.pdf_page_start, c.pdf_page_end) == (67, 67)
    assert (c.printed_page_start, c.edicion, c.enmienda, c.fecha) == ("10", "VI", "I", "mayo 2026")
    assert c.source_url == URL
    assert c.cited_text == BLOCK_61535["text"]


def test_page_citation_cites_every_seccion_on_the_page(docs):
    answer = map_answer("Se requiere instrucción nocturna <doc=raac-61.pdf;page=67>.", [PAGE_67], docs)
    assert [c.seccion for c in answer.sentences[0].citations] == ["61.530", "61.535"]


def test_uncited_sentences_are_dropped_and_tags_attach_to_the_sentence_they_end(docs):
    text = (
        "Te cuento lo que dice la RAAC. "
        "Necesitás instrucción en vuelo nocturno.<doc=raac-61.pdf;page=67;block=p67_text_4> "
        "Consultá también la Sección 61.520."
    )
    answer = map_answer(text, [BLOCK_61535], docs)
    assert [s.text for s in answer.sentences] == ["Necesitás instrucción en vuelo nocturno."]
    assert answer.dropped == ["Te cuento lo que dice la RAAC.", "Consultá también la Sección 61.520."]


def test_cite_tags_both_forms(docs):
    text = 'Hay un régimen transitorio <cite doc="raac-61.pdf" page="67" block="p67_text_4"/>. <cite doc="raac-61.pdf" page="67">Rige para VFR nocturno.</cite>'
    answer = map_answer(text, [BLOCK_61535, PAGE_67], docs)
    assert [s.text for s in answer.sentences] == ["Hay un régimen transitorio.", "Rige para VFR nocturno."]
    assert [c.seccion for c in answer.sentences[0].citations] == ["61.535"]
    assert len(answer.sentences[1].citations) == 2


def test_no_tags_or_unknown_documents_is_a_refusal(docs):
    assert map_answer("No encontré información sobre eso.", [], docs).refused
    unknown = {"document": "otro.pdf", "doc_id": None, "page": 3}
    out_of_range = {"document": "raac-61.pdf", "doc_id": DOC, "page": 999}
    answer = map_answer("Algo. <doc=otro.pdf;page=3> Otra cosa. <doc=raac-61.pdf;page=999>", [unknown, out_of_range], docs)
    assert answer.refused
    assert answer.dropped == ["Algo.", "Otra cosa."]


def test_eval_runner_targets_cloud_with_the_same_cases_and_report_shape(docs, parte61):
    fake = FakeCloud("Necesitás instrucción en vuelo nocturno. <doc=raac-61.pdf;page=67;block=p67_text_4>", [BLOCK_61535])
    pipeline = CloudPipeline(fake, list(docs.values()))
    cases = [c for c in load_cases(SEED) if c.id == "61-vfr-nocturno-ppl"]
    report = run_eval(pipeline, cases, SEED)
    assert report["pipeline"]["name"] == "pageindex-cloud"
    assert report["pipeline"]["served_answer_models"] == ["pageindex-chat-x"]
    assert report["pipeline"]["index_versions"] == [
        {"parte": "61", "content_hash": parte61.content_hash, "edicion": "VI", "enmienda": "I", "from_cache": False}
    ]
    [case] = report["cases"]
    assert case["error"] is None
    assert case["cited_secciones"] == [{"parte": "61", "seccion": "61.535"}]
    assert case["retrieved_secciones"] == case["cited_secciones"]
    assert case["scores"] == {"retrieval_hit": True, "retrieval_recall": 1.0, "grounding": 1.0, "refusal_correct": True, "correctness": None}
    messages, kwargs = fake.calls[0]
    assert messages == [{"role": "user", "content": cases[0].question}]
    assert kwargs == {"doc_id": [DOC], "enable_citations": True}


def test_pipeline_result_uses_cited_secciones_as_retrieval(docs):
    fake = FakeCloud("Rige el régimen transitorio. <doc=raac-61.pdf;page=67;block=p67_text_4>", [BLOCK_61535])
    result = CloudPipeline(fake, list(docs.values())).run("¿VFR nocturno?")
    assert result.routed_partes == ["61"]
    assert result.retrieved_secciones == [SeccionRef("61", "61.535")]


def test_missing_key_fails_before_any_download(monkeypatch, tmp_path):
    monkeypatch.delenv("PAGEINDEX_API_KEY", raising=False)
    monkeypatch.setattr(corpus, "fetch_listings", lambda *a, **k: pytest.fail("downloaded without a key"))
    with pytest.raises(CloudKeyMissing, match="PAGEINDEX_API_KEY is not set"):
        build_cloud_pipeline(["61"], tmp_path)


def test_cli_eval_cloud_without_key_exits_with_a_clear_error(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("PAGEINDEX_API_KEY", raising=False)
    monkeypatch.setattr(cli, "load_env", lambda: None)
    monkeypatch.setattr(corpus, "fetch_listings", lambda *a, **k: pytest.fail("downloaded without a key"))
    code = cli.main(["eval", "--pipeline", "cloud", "--cases", str(SEED), "--cache-dir", str(tmp_path)])
    assert code == 2
    assert "PAGEINDEX_API_KEY is not set" in capsys.readouterr().err


class UploadingCloud(FakeCloud):
    def __init__(self):
        super().__init__("", [])
        self.uploads = []

    def submit_document(self, path, **kwargs):
        self.uploads.append((path, kwargs))
        return {"doc_id": f"pi-{len(self.uploads)}"}

    def get_document(self, doc_id):
        return {"id": doc_id, "status": "completed"}


def test_each_pdf_is_uploaded_once_by_content_hash(monkeypatch, tmp_path, parte61):
    monkeypatch.setenv("PAGEINDEX_API_KEY", "test-key")
    listing = corpus.ParteListing("61", "Licencias", URL)
    data = (FIXTURES / "raac-parte-61.pdf").read_bytes()
    monkeypatch.setattr(corpus, "fetch_listings", lambda partes, client=None: [listing])
    monkeypatch.setattr(corpus, "download", lambda listing, client=None: corpus.Pdf(data, parte61.content_hash))
    fake = UploadingCloud()
    first = build_cloud_pipeline(["61"], tmp_path, client=fake)
    second = build_cloud_pipeline(["61"], tmp_path, client=fake)
    assert len(fake.uploads) == 1
    _, kwargs = fake.uploads[0]
    assert kwargs["metadata"] == {"parte": "61", "sha256": parte61.content_hash}
    assert kwargs["wait"] is True
    assert first.index_versions() == second.index_versions()
    assert [v.parte for v in first.index_versions()] == ["61"]
