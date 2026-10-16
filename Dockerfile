FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[otlp]"
COPY datasets ./datasets
ENTRYPOINT ["tracegate"]
CMD ["eval", "--dataset", "datasets/golden.jsonl", "--out", "reports"]
