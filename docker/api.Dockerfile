# HeyWalk API image: FastAPI orchestration service.
#
# The API manages projects, model requests, and run orchestration. It NEVER
# executes generated controller code itself; runs launch sibling containers
# from the runner image via the host `docker` CLI.
#
# SECURITY: do NOT bake MODEL_API_KEY into this image; provide it at runtime.
# The compose stack mounts the Docker socket into this orchestrator container
# so it can launch sibling run containers. Generated code never runs here:
# run containers themselves get no socket, no network, and a non-root user
# (see services/api/app/runner/docker_runner.py).
#
# Build from the repo root:
#   docker build -f docker/api.Dockerfile -t heywalk-api:latest .
#
# NOTE: the MuJoCo physics engine is NOT installed here. Evaluation works from
# recorded test evidence (app/simulator/mujoco_tests.py imports the engine only
# for type checking); the engine runs in the isolated runner image.

FROM python:3.12-slim

# Docker CLI only (no daemon): lets the API orchestrator launch sibling run
# containers via the mounted Docker socket. Generated code never runs here.
COPY --from=docker:27-cli /usr/local/bin/docker /usr/local/bin/docker

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv/api
COPY services/api/pyproject.toml ./
COPY services/api/app ./app
RUN pip install --no-cache-dir .

RUN useradd --create-home --uid 65532 api \
  && mkdir -p /srv/api/data \
  && chown -R api:api /srv/api
COPY docker/api-entrypoint.py /usr/local/bin/api-entrypoint.py

# Starts as root so the entrypoint can join the socket's group, then drops to
# the api user before exec'ing uvicorn (see api-entrypoint.py).
ENV HEYWALK_DB=/srv/api/data/heywalk.db
EXPOSE 8000
ENTRYPOINT ["python", "/usr/local/bin/api-entrypoint.py"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
