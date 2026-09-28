FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    HF_HOME=/models

WORKDIR /app

# Dependencies first so code changes don't reinstall torch.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --extra local --no-install-project

COPY README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev --extra local --no-editable

RUN useradd --create-home app && mkdir -p /models && chown app /models
USER app

EXPOSE 8000
CMD ["uvicorn", "cka.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
