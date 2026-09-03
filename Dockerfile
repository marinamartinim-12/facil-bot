FROM python:3.11-slim
WORKDIR /app
# ffmpeg: conversão de áudio (ogg/opus p/ WhatsApp e mp3 p/ o painel)
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8000
# gunicorn gerencia os workers uvicorn: --max-requests renova cada worker a cada
# ~1000-1200 req (limpa acúmulo de memória); --timeout mata e reinicia sozinho um
# worker com event loop travado >90s (auto-recuperação sem restart manual).
CMD ["sh", "-c", "gunicorn main:app -k uvicorn.workers.UvicornWorker -w 2 -b 0.0.0.0:${PORT:-8000} --max-requests 1000 --max-requests-jitter 200 --timeout 90 --graceful-timeout 30 --keep-alive 15"]
