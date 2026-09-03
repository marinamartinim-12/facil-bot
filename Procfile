web: gunicorn main:app -k uvicorn.workers.UvicornWorker -w 2 -b 0.0.0.0:$PORT --max-requests 1000 --max-requests-jitter 200 --timeout 90 --graceful-timeout 30 --keep-alive 15
