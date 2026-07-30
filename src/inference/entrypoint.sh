#!/usr/bin/env bash
# Container entrypoint. SageMaker starts an inference container with the single
# argument `serve`, and a training container with `train`.
#
# This script exists because the obvious Dockerfile — `ENTRYPOINT ["gunicorn"]` with
# the gunicorn arguments in CMD — is wrong in a way that looks right. `docker run
# IMAGE serve` REPLACES CMD, so the container runs `gunicorn serve`, gunicorn treats
# `serve` as the module to import, and the worker dies with
# "ModuleNotFoundError: No module named 'serve'". The image builds, the Dockerfile
# reads correctly, and the container cannot start.
#
# That was the actual bug: it survived local review and was caught by the container
# contract check in CI, which is the only thing that runs the image the way SageMaker
# does.

set -euo pipefail

MODE="${1:-serve}"

case "$MODE" in
  serve)
    # One worker, two threads. The model is held in process memory; it is safe to
    # predict from multiple threads but not to fit. Scaling out is the autoscaling
    # policy's job — more workers would multiply memory by the model size for no
    # throughput gain on a CPU-bound sparse dot product.
    exec gunicorn \
      --bind "0.0.0.0:${PORT:-8080}" \
      --workers "${GUNICORN_WORKERS:-1}" \
      --threads "${GUNICORN_THREADS:-2}" \
      --timeout "${GUNICORN_TIMEOUT:-60}" \
      --graceful-timeout 30 \
      --access-logfile - \
      --error-logfile - \
      src.inference.serve:app
    ;;
  train)
    # Present so the same image can back a SageMaker training job. The training
    # entrypoint reads the SM_CHANNEL_* conventions from the environment.
    exec python -m src.training.train
    ;;
  *)
    # Anything else is passed through, which keeps `docker run IMAGE bash` working
    # for debugging without a separate image.
    exec "$@"
    ;;
esac
