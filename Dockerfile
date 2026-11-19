FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.13 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --extra mssql --extra postgres
ENV PATH="/app/.venv/bin:$PATH"
# Configure with CLARITY_ETL_DB_URL, CLARITY_ETL_SOURCE, SFTP_PASSWORD
ENTRYPOINT ["clarity-etl"]
CMD ["run", "--with-agent"]
