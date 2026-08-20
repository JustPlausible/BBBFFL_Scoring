FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY data ./data

# Scorer decisions must survive container restarts -- mount a volume here.
VOLUME ["/app/data"]

ENV BBBFFL_DB_PATH=/app/data/scorer_decisions.db \
    BBBFFL_TEAMS_CONFIG_PATH=/app/data/grand_final_teams.json

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health').read()"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
