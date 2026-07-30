"""HTTP serving layer — the /ping and /invocations contract SageMaker requires.

SageMaker's container contract is exactly two routes:

  GET  /ping         must return 200 once the container is ready to serve
  POST /invocations  the prediction endpoint

The distinction this module cares most about is **readiness vs liveness**. `/ping`
returns 200 only after the model is loaded and has successfully scored a canary
document. A container that answers `/ping` before it can actually predict gets
traffic routed to it and fails every request — and during a canary deployment that
looks like the *new* variant working, so the rollback alarm never fires and the bad
version proceeds to full traffic. Readiness that does not exercise the model is
worse than no readiness check.

Status-code mapping is also deliberate: malformed client input is 4xx, internal
failure is 5xx. The endpoint's 5xx alarm drives the automatic rollback, so
returning 500 for bad JSON would roll back a healthy deployment because someone
posted a malformed request.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Final

from flask import Flask, Response, request

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.inference.inference import (  # noqa: E402
    CONTENT_TYPE_JSON,
    InferenceError,
    input_fn,
    model_fn,
    output_fn,
    predict_fn,
)

# SageMaker mounts the extracted model.tar.gz here.
MODEL_DIR: Final[str] = os.environ.get("SM_MODEL_DIR", "/opt/ml/model")

# The canary document scored during readiness. Deliberately trivial — its purpose
# is to prove the model can execute, not to check what it predicts.
READINESS_PROBE_TEXT: Final[str] = "document 000000 reference date page total"

logger = logging.getLogger("intake.inference")


def _configure_logging() -> None:
    """Structured JSON logs on stdout.

    JSON from the start rather than retrofitted at M4: the correlation_id that M3
    propagates and M4 traces on has to be a real field, and reformatting logs later
    means reprocessing every stored log line.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.handlers = [handler]
    logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
    logger.propagate = False


def log_event(level: int, event: str, **fields: Any) -> None:
    logger.log(level, json.dumps({"event": event, **fields}, default=str))


class ModelState:
    """Holds the loaded model and the outcome of the readiness probe.

    Load failures are captured rather than raised at import time. A container that
    exits on startup gives SageMaker no endpoint to query and produces a generic
    "container failed" message; one that starts and reports *why* it is not ready
    via /ping is diagnosable from CloudWatch without reproducing the failure.
    """

    def __init__(self) -> None:
        self.model: Any | None = None
        self.error: str | None = None
        self.ready: bool = False
        self.loaded_at: float | None = None

    def load(self) -> None:
        started = time.monotonic()
        try:
            self.model = model_fn(MODEL_DIR)
        except Exception as error:  # noqa: BLE001 - must not escape startup
            self.error = f"model load failed: {type(error).__name__}: {error}"
            log_event(logging.ERROR, "model_load_failed", error=self.error)
            return

        # Readiness is only true once the model has actually produced a
        # prediction. See the module docstring for why a load-only check is
        # actively dangerous during a canary deployment.
        try:
            predict_fn([READINESS_PROBE_TEXT], self.model)
        except Exception as error:  # noqa: BLE001
            self.error = (
                f"readiness probe failed: {type(error).__name__}: {error}"
            )
            log_event(logging.ERROR, "readiness_probe_failed", error=self.error)
            return

        self.ready = True
        self.loaded_at = time.monotonic()
        log_event(
            logging.INFO,
            "model_ready",
            load_seconds=round(self.loaded_at - started, 3),
            model_dir=MODEL_DIR,
        )


def create_app(state: ModelState | None = None) -> Flask:
    _configure_logging()
    app = Flask(__name__)

    if state is None:
        state = ModelState()
        state.load()
    app.config["MODEL_STATE"] = state

    @app.get("/ping")
    def ping() -> Response:
        """Readiness, not liveness. 200 only when the model can actually score."""
        current: ModelState = app.config["MODEL_STATE"]
        if current.ready:
            return Response(
                json.dumps({"status": "ready"}),
                status=200,
                mimetype=CONTENT_TYPE_JSON,
            )
        return Response(
            json.dumps({"status": "unavailable", "reason": current.error}),
            status=503,
            mimetype=CONTENT_TYPE_JSON,
        )

    @app.post("/invocations")
    def invocations() -> Response:
        current: ModelState = app.config["MODEL_STATE"]

        # A correlation id supplied by the caller is preserved; otherwise one is
        # minted so a request is always traceable. M3 passes the id it carries
        # from the originating S3 event, which is what makes a single document
        # followable end to end.
        correlation_id = (
            request.headers.get("X-Correlation-Id")
            or request.headers.get("X-Amzn-SageMaker-Custom-Attributes")
            or str(uuid.uuid4())
        )

        if not current.ready or current.model is None:
            log_event(
                logging.ERROR,
                "invocation_while_not_ready",
                correlation_id=correlation_id,
                reason=current.error,
            )
            return Response(
                json.dumps({"error": "model not ready", "reason": current.error}),
                status=503,
                mimetype=CONTENT_TYPE_JSON,
            )

        started = time.perf_counter()
        try:
            texts = input_fn(
                request.get_data(), request.content_type or CONTENT_TYPE_JSON
            )
        except InferenceError as error:
            # 4xx, deliberately: a malformed request is not an endpoint fault, and
            # counting it as one would trip the 5xx rollback alarm.
            log_event(
                logging.WARNING,
                "bad_request",
                correlation_id=correlation_id,
                error=str(error),
            )
            return Response(
                json.dumps({"error": str(error)}),
                status=400,
                mimetype=CONTENT_TYPE_JSON,
            )

        try:
            predictions = predict_fn(texts, current.model)
            body, content_type = output_fn(
                predictions, request.headers.get("Accept", CONTENT_TYPE_JSON)
            )
        except InferenceError as error:
            return Response(
                json.dumps({"error": str(error)}),
                status=400,
                mimetype=CONTENT_TYPE_JSON,
            )
        except Exception as error:  # noqa: BLE001
            # 5xx: a genuine endpoint fault, and exactly what should drive the
            # rollback alarm.
            log_event(
                logging.ERROR,
                "inference_failed",
                correlation_id=correlation_id,
                error=f"{type(error).__name__}: {error}",
            )
            return Response(
                json.dumps({"error": "internal inference failure"}),
                status=500,
                mimetype=CONTENT_TYPE_JSON,
            )

        elapsed_ms = (time.perf_counter() - started) * 1000.0
        log_event(
            logging.INFO,
            "invocation",
            correlation_id=correlation_id,
            batch_size=len(texts),
            latency_ms=round(elapsed_ms, 2),
            # Logged per request so M4 can build the confidence percentiles and
            # auto-approval rate without re-reading data-capture from S3.
            predicted_classes=[p["predicted_class"] for p in predictions],
            confidences=[round(p["confidence"], 4) for p in predictions],
            auto_approve_eligible=[
                p["auto_approve_eligible"] for p in predictions
            ],
        )

        response = Response(body, status=200, mimetype=content_type)
        response.headers["X-Correlation-Id"] = correlation_id
        return response

    return app


app = create_app() if os.environ.get("EAGER_LOAD", "1") == "1" else None


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    create_app().run(host="0.0.0.0", port=port)
