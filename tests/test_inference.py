"""Inference handler and HTTP contract tests.

`TestResponseContract` is the test M6 nominates as its regression-catching test.
The response body is consumed by three later milestones — M3's Route state reads
`confidence`, M4 derives metrics from these fields, M5's drift job parses them out
of data-capture records — so a renamed or dropped key breaks all three at runtime
with no compile-time signal. Asserting the exact key set is what turns that into a
failing test at PR time.

The status-code tests matter for a less obvious reason: the endpoint's 5xx alarm
drives M2's automatic rollback. Returning 500 for a malformed client request would
roll back a healthy deployment because someone posted bad JSON.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from src.config import AUTO_APPROVE_CONFIDENCE_THRESHOLD, DOCUMENT_CLASSES
from src.data import generate
from src.inference import inference
from src.inference.inference import (
    CONTENT_TYPE_JSON,
    InferenceError,
    input_fn,
    model_fn,
    output_fn,
    predict_fn,
)
from src.training.model import TfidfLinearClassifier


@pytest.fixture(scope="module")
def model_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A real trained model on disk, laid out the way SageMaker mounts it."""
    directory = tmp_path_factory.mktemp("model")
    docs = generate.generate_documents(docs_per_class=40, seed=4242)
    model = TfidfLinearClassifier(seed=4242, min_df=1)
    model.fit([d.text for d in docs], [d.label for d in docs])
    model.save(directory / "model.joblib")
    return directory


@pytest.fixture(scope="module")
def model(model_dir: Path) -> Any:
    return model_fn(str(model_dir))


class TestModelFn:
    def test_loads_a_real_artifact(self, model: Any) -> None:
        assert model.classes

    def test_missing_artifact_lists_the_directory(self, tmp_path: Path) -> None:
        """The error must name what *was* there.

        The usual cause is a model.tar.gz packed with an unexpected internal
        layout, and the directory listing identifies that without a rebuild.
        """
        (tmp_path / "wrong-name.joblib").write_bytes(b"x")
        with pytest.raises(FileNotFoundError, match="wrong-name.joblib"):
            model_fn(str(tmp_path))

    def test_nonexistent_directory_is_reported_clearly(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="does not exist"):
            model_fn(str(tmp_path / "nope"))


class TestInputFn:
    def test_single_document(self) -> None:
        assert input_fn(json.dumps({"text": "hello"})) == ["hello"]

    def test_batch_via_texts(self) -> None:
        assert input_fn(json.dumps({"texts": ["a", "b"]})) == ["a", "b"]

    def test_batch_via_instances(self) -> None:
        body = json.dumps({"instances": [{"text": "a"}, {"text": "b"}]})
        assert input_fn(body) == ["a", "b"]

    def test_accepts_bytes(self) -> None:
        assert input_fn(json.dumps({"text": "hello"}).encode("utf-8")) == ["hello"]

    def test_accepts_content_type_with_charset(self) -> None:
        assert input_fn(
            json.dumps({"text": "hello"}), "application/json; charset=utf-8"
        ) == ["hello"]

    @pytest.mark.parametrize(
        "body,expected",
        [
            ("not json", "not valid JSON"),
            ("[1,2,3]", "expected a JSON object"),
            ('{"other": 1}', "must contain one of"),
            ('{"texts": "notalist"}', "must be a list"),
            ('{"texts": []}', "no documents"),
            ('{"text": ""}', "empty or whitespace"),
            ('{"text": "   "}', "empty or whitespace"),
            ('{"text": 42}', "must be a string"),
            ('{"instances": [{"no_text": 1}]}', "must be an object containing"),
        ],
    )
    def test_rejects_malformed_payloads(self, body: str, expected: str) -> None:
        """Strict validation, deliberately.

        A permissive parser that coerced junk into an empty string would return a
        confident prediction for a document nobody submitted, and the Route state
        would auto-approve it.
        """
        with pytest.raises(InferenceError, match=expected):
            input_fn(body)

    def test_rejects_wrong_content_type(self) -> None:
        with pytest.raises(InferenceError, match="unsupported content type"):
            input_fn(json.dumps({"text": "x"}), "text/csv")

    def test_rejects_invalid_utf8(self) -> None:
        with pytest.raises(InferenceError, match="not valid UTF-8"):
            input_fn(b"\xff\xfe not utf8")

    def test_rejects_oversized_batch(self) -> None:
        body = json.dumps({"texts": ["x"] * (inference.MAX_BATCH_SIZE + 1)})
        with pytest.raises(InferenceError, match="exceeds the maximum"):
            input_fn(body)

    def test_rejects_oversized_document(self) -> None:
        body = json.dumps({"text": "x" * (inference.MAX_TEXT_CHARS + 1)})
        with pytest.raises(InferenceError, match="over the"):
            input_fn(body)


class TestResponseContract:
    """The regression-catching test M6 nominates.

    These key names are a cross-milestone contract. A rename here is invisible at
    build time and breaks M3 routing, M4 metrics and M5 drift parsing at runtime.
    """

    EXPECTED_PREDICTION_KEYS = {
        "predicted_class",
        "confidence",
        "class_probabilities",
        "auto_approve_eligible",
        "confidence_threshold",
    }

    def test_prediction_keys_are_exactly_the_contract(self, model: Any) -> None:
        result = predict_fn(["invoice amount due payable"], model)[0]
        assert set(result) == self.EXPECTED_PREDICTION_KEYS

    def test_envelope_keys_are_exactly_the_contract(self, model: Any) -> None:
        body, _ = output_fn(predict_fn(["some text"], model))
        assert set(json.loads(body)) == {
            "schema_version",
            "predictions",
            "model_version",
        }

    def test_class_probabilities_cover_every_configured_class(
        self, model: Any
    ) -> None:
        """Every class must be present, even one the model never predicts.

        M5 computes prediction-drift against the baseline's per-class priors; a
        missing key there is a KeyError in a scheduled job rather than a visible
        failure at deploy time.
        """
        result = predict_fn(["anything at all"], model)[0]
        assert set(result["class_probabilities"]) == set(DOCUMENT_CLASSES)

    def test_probabilities_sum_to_one(self, model: Any) -> None:
        result = predict_fn(["invoice total vat"], model)[0]
        assert sum(result["class_probabilities"].values()) == pytest.approx(1.0)

    def test_confidence_equals_the_top_class_probability(self, model: Any) -> None:
        result = predict_fn(["patient specimen findings"], model)[0]
        assert result["confidence"] == pytest.approx(
            result["class_probabilities"][result["predicted_class"]]
        )

    def test_auto_approve_flag_matches_the_threshold(self, model: Any) -> None:
        """The flag and the number it derives from must agree.

        Computed in the handler rather than in the state machine so the threshold
        and the probability cannot drift apart across two codebases.
        """
        for result in predict_fn(
            [d.text for d in generate.generate_documents(docs_per_class=3, seed=8)],
            model,
        ):
            assert result["auto_approve_eligible"] == (
                result["confidence"] >= AUTO_APPROVE_CONFIDENCE_THRESHOLD
            )
            assert result["confidence_threshold"] == AUTO_APPROVE_CONFIDENCE_THRESHOLD

    def test_batch_order_is_preserved(self, model: Any) -> None:
        """Results must align positionally with the request.

        A reordered batch silently attaches every prediction to the wrong
        document, and every aggregate metric still looks normal.
        """
        texts = [
            "invoice amount due payable vat remittance",
            "patient specimen clinician findings impression",
            "passport surname nationality expiry issuing",
        ]
        single = [predict_fn([t], model)[0]["predicted_class"] for t in texts]
        batched = [p["predicted_class"] for p in predict_fn(texts, model)]
        assert batched == single

    def test_predicted_class_is_always_a_known_class(self, model: Any) -> None:
        result = predict_fn(["completely unrelated gibberish zzz"], model)[0]
        assert result["predicted_class"] in DOCUMENT_CLASSES


class TestOutputFn:
    def test_returns_json_content_type(self, model: Any) -> None:
        _, content_type = output_fn(predict_fn(["x"], model))
        assert content_type == CONTENT_TYPE_JSON

    def test_accepts_wildcard_accept_header(self, model: Any) -> None:
        body, _ = output_fn(predict_fn(["x"], model), "*/*")
        assert json.loads(body)["predictions"]

    def test_rejects_unsupported_accept(self, model: Any) -> None:
        with pytest.raises(InferenceError, match="unsupported accept"):
            output_fn(predict_fn(["x"], model), "application/xml")


class TestHttpContract:
    """The /ping and /invocations routes SageMaker requires."""

    @pytest.fixture()
    def client(self, model_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
        from src.inference import serve

        monkeypatch.setattr(serve, "MODEL_DIR", str(model_dir))
        state = serve.ModelState()
        state.load()
        app = serve.create_app(state)
        app.config.update(TESTING=True)
        with app.test_client() as test_client:
            yield test_client

    def test_ping_returns_200_when_ready(self, client: Any) -> None:
        response = client.get("/ping")
        assert response.status_code == 200
        assert response.get_json()["status"] == "ready"

    def test_invocations_returns_predictions(self, client: Any) -> None:
        response = client.post(
            "/invocations",
            data=json.dumps({"text": "invoice amount due payable"}),
            content_type=CONTENT_TYPE_JSON,
        )
        assert response.status_code == 200
        assert response.get_json()["predictions"][0]["predicted_class"] in (
            DOCUMENT_CLASSES
        )

    def test_malformed_json_is_4xx_not_5xx(self, client: Any) -> None:
        """The single most consequential status code in this file.

        The endpoint's 5xx alarm drives the automatic rollback. If a malformed
        client request returned 500, posting bad JSON would roll back a perfectly
        healthy deployment.
        """
        response = client.post(
            "/invocations", data="{not json", content_type=CONTENT_TYPE_JSON
        )
        assert response.status_code == 400

    def test_wrong_content_type_is_4xx(self, client: Any) -> None:
        response = client.post(
            "/invocations", data=json.dumps({"text": "x"}), content_type="text/csv"
        )
        assert response.status_code == 400

    def test_correlation_id_is_echoed(self, client: Any) -> None:
        """End-to-end traceability depends on this surviving the endpoint hop."""
        response = client.post(
            "/invocations",
            data=json.dumps({"text": "invoice total"}),
            content_type=CONTENT_TYPE_JSON,
            headers={"X-Correlation-Id": "trace-me-123"},
        )
        assert response.headers["X-Correlation-Id"] == "trace-me-123"

    def test_correlation_id_is_minted_when_absent(self, client: Any) -> None:
        response = client.post(
            "/invocations",
            data=json.dumps({"text": "invoice total"}),
            content_type=CONTENT_TYPE_JSON,
        )
        assert response.headers.get("X-Correlation-Id")


class TestReadinessSemantics:
    """Readiness must mean "can serve", not merely "process started"."""

    def test_ping_is_503_when_the_model_is_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A container with no model must fail readiness, not accept traffic.

        This is the behaviour the M2 rollback depends on. If /ping returned 200
        for a broken model, the canary variant would look healthy, the alarm would
        never fire, and the bad version would proceed to 100% of traffic.
        """
        from src.inference import serve

        monkeypatch.setattr(serve, "MODEL_DIR", str(tmp_path))
        state = serve.ModelState()
        state.load()
        app = serve.create_app(state)

        with app.test_client() as client:
            response = client.get("/ping")
            assert response.status_code == 503
            assert "model load failed" in response.get_json()["reason"]

    def test_invocations_is_503_when_not_ready(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.inference import serve

        monkeypatch.setattr(serve, "MODEL_DIR", str(tmp_path))
        state = serve.ModelState()
        state.load()
        app = serve.create_app(state)

        with app.test_client() as client:
            response = client.post(
                "/invocations",
                data=json.dumps({"text": "x"}),
                content_type=CONTENT_TYPE_JSON,
            )
            assert response.status_code == 503

    def test_readiness_requires_a_successful_prediction(
        self, model_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A model that loads but cannot predict must not report ready.

        Load-only readiness is the dangerous variant: an artifact can deserialise
        and still fail on every call (a version-mismatched pickle, a missing
        vectoriser vocabulary), and that container would then be handed traffic.
        """
        from src.inference import serve

        monkeypatch.setattr(serve, "MODEL_DIR", str(model_dir))

        def exploding_predict(texts: list[str], model: Any) -> list[dict[str, Any]]:
            raise RuntimeError("vectoriser vocabulary is empty")

        monkeypatch.setattr(serve, "predict_fn", exploding_predict)
        state = serve.ModelState()
        state.load()

        assert state.ready is False
        assert state.error is not None
        assert "readiness probe failed" in state.error
