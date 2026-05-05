FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y stockfish libcairo2 libpango-1.0-0 libpangocairo-1.0-0 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py commentary_prompts.py tournaments_config.py tournaments.yaml ./

CMD ["python", "bot.py"]
