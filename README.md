# Entrega 2: Pipeline asíncrono con Kafka y Flink (resumen y documentación)

Fecha: 29 de octubre de 2025

## 1. Resumen de cambios respecto a la Tarea 1

- Se desacopló la interacción síncrona con el LLM transformándola en un pipeline asíncrono:
- La API ya no debe llamar al LLM directamente en modo normal; opcionalmente encola peticiones en Kafka.
- Se introdujo Apache Kafka (brokers + Zookeeper) como bus de mensajes para gestionar solicitudes y resultados.
- Se añadió un consumidor `worker` que realiza las llamadas al LLM (generación) y publica resultados o errores.
- Se añadió un módulo `retry` que gestiona reintentos de forma desacoplada (exponential backoff para rate limits,
  backoff lineal para timeouts/otros) y que evita bucles infinitos mediante un contador `attempts`.
- Se añadió un componente de scoring (esqueleto PyFlink + una alternativa local `monitor` para pruebas) que
  evalúa la calidad con la rúbrica de la Tarea 1 y decide si persistir o reenviar para regeneración.
- Se añadió un consumidor `storage` que persiste los resultados validados en Postgres y popula la caché Redis.
- Se actualizaron `docker-compose.yml` y las imágenes para contener todos los servicios necesarios.

Archivos añadidos / modificados (puntos clave):
- `services/api/main.py` — consulta DB antes que cache y encola en `llm.requests` si no hay resultado.
- `services/worker/worker.py` — consumidor Kafka (generación) → publica `llm.generated` o `llm.errors`.
- `services/retry/retry.py` — consumidor de errores aplica backoff y re-enqueuea o mueve a `llm.failed`.
- `services/monitor/monitor.py` — scorer local (actúa como Flink para pruebas): publica `llm.validated` o requeuea.
- `services/storage/storage_consumer.py` — persiste en Postgres y escribe cache Redis `q:{question_hash}`.
- `flink/job.py` — esqueleto PyFlink con la misma lógica (para despliegue en cluster Flink real).
- `docker-compose.yml` — añadidos Kafka/Zookeeper, worker, retry, monitor, storage; ajustadas imágenes.

## 1. Arquitectura y topología de tópicos

Diagrama simplificado del flujo:

Client -> API -> (DB check) -> Cache -> (si no existe) -> Kafka: `llm.requests`
                                    ↓
                                  Worker (genera)
                                    ↓
                          Kafka: `llm.generated`  --> Scoring (Flink / monitor)
                                    ↓                             ↓
                   Si score >= umbral -> `llm.validated` -> Storage -> Persistencia + Redis
                   Si score < umbral and attempts < MAX -> re-queue -> `llm.requests`
                   Si error en generación -> `llm.errors` -> Retry -> re-queue o `llm.failed`

Lista de tópicos y propósito:
- `llm.requests`: peticiones iniciales para generación (id, title, content, best_answer, attempts).
- `llm.generated`: respuestas generadas por el LLM (sin puntaje).
- `llm.errors`: errores detectados durante generación (incluye `error_type`, `attempts`).
- `llm.validated`: respuestas que pasaron el scoring o que se entregan finalmente (incluye `score`).
- `llm.failed`: mensajes que excedieron `MAX_ATTEMPTS` y requieren inspección manual.

Responsabilidades:
- API: productor de `llm.requests` (si no hay resultado persistido).
- Worker: consumidor de `llm.requests`; productor de `llm.generated` o `llm.errors`.
- Retry: consumidor de `llm.errors` y re-enqueuea según política.
- Scoring (Flink/monitor): consume `llm.generated`, aplica rúbrica, y produce `llm.validated` o requeue.
- Storage: consume `llm.validated` y persiste en DB + actualiza Redis.

## 3. Manejo de errores y estrategias de reintento

Errores gestionados explícitamente:
- rate_limit (HTTP 429 u otras señales del proveedor) — política: exponential backoff (delay = base * 2^attempts, capped).
- timeout — política: backoff lineal corta (delay = base * attempts).
- otros errores transitorios — tratamiento conservador similar a timeout.

Implementación práctica:
- `worker` no bloquea reintentos: ante fallo publica en `llm.errors` con `error_type` y `attempts` actuales.
- `retry` consume `llm.errors`, calcula delay y `time.sleep(delay)` localmente, luego re-enqueuea con `attempts+1`.
- Si `attempts` supera `MAX_ATTEMPTS` el mensaje va a `llm.failed`.

Nota: para producción es preferible un mecanismo de delayed messages (Kafka TTL/compact topics o un scheduler)
en vez de `sleep` dentro del consumidor; para la entrega académica, `retry` implementa la lógica y es suficiente.

## 4. Scoring: rúbrica y umbral

Se reutiliza la rúbrica de la Tarea 1 (ponderaciones): Exactitud 40 %, Integridad 25 %, Claridad 20 %,
Concisón 10 %, Utilidad 5 %. El componente de scoring (monitor o Flink) envía un JSON con campos de la rúbrica
y un `final` score que se almacena en `response_event.score`.

El umbral por defecto de regeneración se fijó en 6 (configurable vía env `REGEN_THRESHOLD`).

## 5. Despliegue y pruebas reproducibles (ejemplos)

1) Build y levantar (desde la raíz del repo):

```bash
docker compose build --pull --no-cache
docker compose up -d db redis zookeeper kafka
docker compose up -d api worker retry monitor storage loadgen
```

2) Encolar una petición de prueba (API en modo Kafka):

```bash
resp=$(curl -s -X POST http://localhost:8000/query \
  -H "Content-Type: application/json" \
  -d '{"title":"¿Qué es la fotosíntesis?","content":"","best_answer":"Proceso por el cual las plantas transforman luz en energía"}')
echo "$resp" | python -m json.tool
qh=$(echo "$resp" | python -c "import sys,json; print(json.load(sys.stdin)['id'])")
echo "Queued id: $qh"
```

3) Ver logs para seguir el procesamiento:

```bash
docker compose logs -f worker --tail=200
docker compose logs -f monitor --tail=200
docker compose logs -f storage --tail=200
```

4) Verificar persistencia en Postgres (ejemplo):

```bash
docker compose exec db psql -U sd -d sd -c "SELECT * FROM response_event WHERE question_hash = '$qh' ORDER BY ts DESC LIMIT 5;"
```

5) Verificar caché Redis:

```bash
docker compose exec redis redis-cli GET "q:$qh"
```

## 6. Pruebas realizadas y resultados (ejemplo de flujo observado)

- Envío a la API → respuesta: `{"queued":true, "id": "<qh>"}`
- Worker consumió `llm.requests` y publicó en `llm.generated`.
- Monitor (scoring) consumió `llm.generated`, calculó score = 7 (>= umbral) y publicó en `llm.validated`.
- Storage consumió `llm.validated` y creó un registro en `response_event` y seteó `q:<qh>` en Redis.
- Subsecuentes consultas a la API retornaron `hit: true` y la respuesta cacheada.

## 7. Anexos: consultas útiles y comandos de depuración

- Listar topics Kafka:
  - `docker compose exec kafka kafka-topics --bootstrap-server kafka:9092 --list`
- Consumir mensajes de un topic (desde principio, 10 mensajes):
  - `docker compose exec kafka kafka-console-consumer --bootstrap-server kafka:9092 --topic llm.requests --from-beginning --max-messages 10`
- Ver últimas filas en `response_event`:
  - `docker compose exec db psql -U sd -d sd -c "SELECT * FROM response_event ORDER BY ts DESC LIMIT 20;"`

---


