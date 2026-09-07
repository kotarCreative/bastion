FROM python:3.12-slim-bookworm

ARG POETRY_VERSION=2.4.1

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    BASTION_DATA_DIR=/var/lib/bastion \
    BASTION_ROOT=/var/lib/bastion/forge

RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates git gosu \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 bastion \
    && useradd --uid 10001 --gid 10001 --home-dir /var/lib/bastion --no-create-home bastion

WORKDIR /app

COPY pyproject.toml poetry.lock ./
RUN python -m pip install --no-cache-dir "poetry==$POETRY_VERSION" \
    && poetry config virtualenvs.create false \
    && poetry install --only main --no-root --no-interaction \
    && python -m pip uninstall -y poetry poetry-core \
    && python -m pip cache purge

COPY . .

ENTRYPOINT ["/app/deployment/container-entrypoint.sh"]
CMD ["python", "forge.py", "--help"]
