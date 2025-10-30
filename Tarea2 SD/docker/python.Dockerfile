# docker/python.Dockerfile
FROM python:3.10-slim


ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app


RUN apt-get update && apt-get install -y --no-install-recommends \
      tini curl ca-certificates \
  && rm -rf /var/lib/apt/lists/*

COPY docker/requirements.txt /app/requirements.txt


RUN python -m pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r /app/requirements.txt


COPY services /app/services

# Entry neutro: el "command:" lo define cada servicio en docker-compose
ENTRYPOINT ["/usr/bin/tini","--"]
CMD ["python","-c","print('worker base image ready')"]

