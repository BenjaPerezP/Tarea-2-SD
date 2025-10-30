FROM python:3.10-slim

WORKDIR /app


COPY docker/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Código
COPY services/api /app/services/api

ENV UVICORN_HOST=0.0.0.0 UVICORN_PORT=8000
CMD ["uvicorn", "services.api.main:app", "--host", "0.0.0.0", "--port", "8000"]

