FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

# CPU build of PyTorch: inference does not need a GPU (the reason these models were chosen).
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip setuptools && pip install torch --index-url https://download.pytorch.org/whl/cpu && pip install -r requirements.txt

COPY alembic.ini .
COPY alembic ./alembic
COPY app ./app
COPY ml ./ml
COPY scripts ./scripts

RUN useradd --create-home appuser && mkdir -p /data/uploads /models && chown -R appuser /data /models /app
USER appuser
ENV BT_UPLOAD_DIR=/data/uploads BT_MODEL_DIR=/models BT_ENVIRONMENT=production

EXPOSE 8000
# One process per container; scale by running more containers behind the proxy (NFR-08).
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers"]
