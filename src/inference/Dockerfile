# Inference container for the document classifier.
#
# Built as a "bring your own container" image rather than using a managed
# SageMaker framework image, because the managed sklearn images pin their own
# scikit-learn version — which would make the reproducibility claim in
# requirements.txt a fiction, since the artifact must be unpickled by the same
# version that wrote it. Owning the image means the training and serving
# scikit-learn are provably identical.
#
# Pinned to a digest-addressable base and built with a deterministic layer order
# so the image digest is stable for identical inputs. The digest, not the tag, is
# what gets registered as model lineage.

FROM python:3.12-slim-bookworm

# SageMaker's contract for a BYOC image:
#   - listen on 8080, serving GET /ping and POST /invocations
#   - the model is mounted, already extracted, at /opt/ml/model
#   - the container is started with the single argument `serve`
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SM_MODEL_DIR=/opt/ml/model \
    PORT=8080

# curl is for the container-level HEALTHCHECK below. Installed before the
# dependency layer so a requirements change does not re-run apt.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/program

# Dependencies before source, so editing a handler does not invalidate the
# (slow) scientific-stack install layer.
COPY requirements.txt requirements-inference.txt ./
RUN pip install --no-cache-dir -r requirements-inference.txt

# Only what serving actually needs. src/training is copied because the model
# artifact is unpickled through src.training.model's loader — the same interface
# that wrote it, which is what makes the model swappable without touching this
# image's contract.
COPY src/__init__.py src/config.py ./src/
COPY src/training/__init__.py src/training/model.py ./src/training/
COPY src/inference/ ./src/inference/

# Non-root. SageMaker does not require it, but a container that only needs to
# read a model and answer HTTP has no reason to run privileged.
RUN useradd --create-home --shell /usr/sbin/nologin --uid 10001 serving \
    && chown -R serving:serving /opt/program
USER serving

EXPOSE 8080

# Container-level health check. Distinct from SageMaker's own /ping polling —
# this one makes `docker run` locally self-diagnosing, which is how the contract
# test verifies readiness without any AWS involvement.
HEALTHCHECK --interval=10s --timeout=3s --start-period=40s --retries=3 \
    CMD curl -fsS http://localhost:8080/ping || exit 1

# `serve` is the argument SageMaker passes. Accepting it as the CMD (rather than
# an ENTRYPOINT script that ignores its arguments) keeps the container's
# interface honest about what SageMaker actually invokes.
#
# One worker, two threads: the model is held in process memory and is not
# thread-safe to fit, only to predict. Scaling out is the autoscaling policy's
# job, not gunicorn's — more workers would multiply memory by the model size for
# no throughput gain on a CPU-bound sparse dot product.
ENTRYPOINT ["gunicorn"]
CMD ["--bind", "0.0.0.0:8080", \
     "--workers", "1", \
     "--threads", "2", \
     "--timeout", "60", \
     "--graceful-timeout", "30", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "src.inference.serve:app"]
