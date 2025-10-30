#!/usr/bin/env python3
"""Simple monitor that mimics the Flink step: consumes `llm.responses`,
computes threshold check and publishes low-quality items to `llm.regenerate`.

This is a lightweight alternative to a PyFlink job for local testing.
"""
import os
import json
import logging
import time
import re
from kafka import KafkaConsumer, KafkaProducer

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("llm-monitor")

KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP", "kafka:9092")
TOPIC_RES = os.getenv("TOPIC_RES", "llm.generated")
TOPIC_VALID = os.getenv("TOPIC_VALID", "llm.validated")
TOPIC_REQ = os.getenv("TOPIC_REQ", "llm.requests")
REGEN_THRESHOLD = int(os.getenv("REGEN_THRESHOLD", "6"))  # regenerate if score < threshold
MAX_ATTEMPTS = int(os.getenv("MAX_ATTEMPTS", "3"))
EVAL_MODEL = os.getenv("EVAL_MODEL", "llama3.2:1b")
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
NUM_THREAD = int(os.getenv("NUM_THREAD", "2"))


def ask_ollama(prompt: str, model: str, num_predict: int = 96, num_ctx: int = 1024, temperature: float = 0.0, top_p: float = 1.0, retries: int = 2, timeout: int = 120) -> str:
    import requests
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "keep_alive": -1,
        "options": {
            "num_ctx": num_ctx,
            "num_predict": num_predict,
            "temperature": temperature,
            "top_p": top_p,
            "num_thread": NUM_THREAD,
        },
    }
    backoff = 1.0
    last_err = None
    for _ in range(max(1, retries)):
        try:
            r = requests.post(f"{OLLAMA_HOST}/api/generate", json=payload, timeout=timeout)
            r.raise_for_status()
            return r.json().get("response", "")
        except Exception as e:
            last_err = e
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
    raise last_err if last_err else RuntimeError("Ollama request failed")


RUBRIC_PROMPT = """Eres un evaluador objetivo. Evalúa la RESPUESTA DEL MODELO para la PREGUNTA dada,
tomando como referencia la RESPUESTA DE REFERENCIA. No penalices el idioma (EN/ES).
Asigna puntajes ENTEROS 0–10 para:
1) Exactitud (40%) 2) Integridad (25%) 3) Claridad (20%) 4) Concisión (10%) 5) Utilidad (5%)
Devuelve SOLO JSON: {"exactitud":int,"integridad":int,"claridad":int,"concision":int,"utilidad":int,"final":int}
"""


def safe_json_extract(text: str):
    try:
        return json.loads(text)
    except Exception:
        import re
        m = re.search(r"\{.*\}", text, flags=re.S)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except Exception:
            return None


def clamp_int_0_10(x):
    try:
        return max(0, min(10, int(round(x))))
    except Exception:
        return 0


def score_with_rubric(question: str, reference_answer: str, candidate_answer: str) -> int:
    prompt = f"""{RUBRIC_PROMPT}

PREGUNTA:
{question}

RESPUESTA DE REFERENCIA:
{reference_answer}

RESPUESTA DEL MODELO:
{candidate_answer}
"""
    out = ask_ollama(prompt, model=EVAL_MODEL, num_predict=96, num_ctx=1536, temperature=0.0, top_p=1.0, retries=2)
    data = safe_json_extract(out) or {}
    ex = clamp_int_0_10(data.get("exactitud", 0))
    integ = clamp_int_0_10(data.get("integridad", 0))
    clar = clamp_int_0_10(data.get("claridad", 0))
    conc = clamp_int_0_10(data.get("concision", 0))
    util = clamp_int_0_10(data.get("utilidad", 0))
    final = int(round(0.40 * ex + 0.25 * integ + 0.20 * clar + 0.10 * conc + 0.05 * util))
    return max(0, min(10, final))


def run():
    consumer = KafkaConsumer(
        TOPIC_RES,
        bootstrap_servers=[KAFKA_BOOTSTRAP],
        value_deserializer=lambda m: json.loads(m.decode("utf-8")),
        auto_offset_reset="earliest",
        group_id="llm-monitor",
        enable_auto_commit=True,
    )
    producer = KafkaProducer(
        bootstrap_servers=[KAFKA_BOOTSTRAP],
        value_serializer=lambda v: json.dumps(v).encode("utf-8"),
    )

    logger.info("Monitor started: consuming %s, scoring and routing (threshold=%s)", TOPIC_RES, REGEN_THRESHOLD)
    for msg in consumer:
        try:
            data = msg.value
            req_id = data.get("id")
            attempts = int(data.get("attempts") or 0)
            question_text = (data.get("title") or data.get("content") or "").strip()
            answer = data.get("answer") or ""
            reference = data.get("best_answer") or ""

            # compute score with LLM judge
            try:
                score = score_with_rubric(question_text, reference, answer)
            except Exception as e:
                logger.exception("Scoring failed for id=%s: %s", req_id, e)
                score = 0

            if score >= REGEN_THRESHOLD:
                logger.info("Validated id=%s score=%s — sending to %s", req_id, score, TOPIC_VALID)
                producer.send(TOPIC_VALID, {"id": req_id, "title": data.get("title"), "content": data.get("content"), "best_answer": reference, "answer": answer, "score": int(score), "attempts": attempts, "ts": time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())})
            else:
                # below threshold: decide to requeue or give up after max attempts
                if attempts + 1 < MAX_ATTEMPTS:
                    logger.info("Low score %s for id=%s — re-queueing (attempt %s)", score, req_id, attempts + 1)
                    req = {"id": req_id, "title": data.get("title"), "content": data.get("content"), "best_answer": reference, "attempts": attempts + 1}
                    producer.send(TOPIC_REQ, req)
                else:
                    logger.info("Low score %s for id=%s but max attempts reached — sending to %s as final", score, req_id, TOPIC_VALID)
                    producer.send(TOPIC_VALID, {"id": req_id, "title": data.get("title"), "content": data.get("content"), "best_answer": reference, "answer": answer, "score": int(score), "attempts": attempts, "final": True, "ts": time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())})
        except Exception as e:
            logger.exception("Error processing generated message: %s", e)


if __name__ == "__main__":
    run()
