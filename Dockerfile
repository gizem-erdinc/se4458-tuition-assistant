# One image, four services, selected with the SERVICE environment variable
# (tuition-api | gateway | mcp-server | agent) — see start.py.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN python -m mcp_server.indexer        # index is built at image build time, never per query
RUN useradd --create-home app && chown -R app /app
USER app
EXPOSE 8000 8001 8080 8765
CMD ["python", "start.py"]
