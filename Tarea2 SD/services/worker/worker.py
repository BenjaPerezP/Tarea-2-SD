#!/usr/bin/env python3
"""Kafka worker: consume llm.requests, call LLM (generation + eval), publish to llm.responses

This worker is intentionally simple and uses kafka-python. It performs:
 - consume messages from topic `llm.requests`
 - generate an answer using GEN_MODEL
 - evaluate the answer using EVAL_MODEL (same rubric)
 - publish message to `llm.responses` with fields: id, question, title, content, best_answer, answer, score, ts

Env vars:
 - KAFKA_BOOTSTRAP (default: kafka:9092)
 - GEN_MODEL, EVAL_MODEL, OLLAMA_HOST, NUM_THREAD
 - REQUESTS_TIMEOUT (seconds)
"""
import os
import json
import time
import logging
from datetime import datetime
from typing import Optional

import requests
from kafka import KafkaConsumer, KafkaProducer

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("llm-worker")

# Config
KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP", "kafka:9092")
TOPIC_REQ = os.getenv("TOPIC_REQ", "llm.requests")
TOPIC_RES = os.getenv("TOPIC_RES", "llm.generated")
TOPIC_ERR = os.getenv("TOPIC_ERR", "llm.errors")

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
GEN_MODEL = os.getenv("GEN_MODEL", "llama3.2:3b")
EVAL_MODEL = os.getenv("EVAL_MODEL", GEN_MODEL)
NUM_THREAD = int(os.getenv("NUM_THREAD", "2"))
REQUESTS_TIMEOUT = int(os.getenv("REQUESTS_TIMEOUT", "120"))

RUBRIC_PROMPT = """Eres un evaluador objetivo. Evalúa la RESPUESTA DEL MODELO para la PREGUNTA dada,
tomando como referencia la RESPUESTA DE REFERENCIA. No penalices el idioma (EN/ES).
Asigna puntajes ENTEROS 0–10 para:
1) Exactitud (40%) 2) Integridad (25%) 3) Claridad (20%) 4) Concisión (10%) 5) Utilidad (5%)
Devuelve SOLO JSON: {"exactitud":int,"integridad":int,"claridad":int,"concision":int,"utilidad":int,"final":int}
"""


def now_iso():
    return datetime.utcnow().isoformat() + "Z"


def ask_ollama(prompt: str, model: str, num_predict: int = 96, num_ctx: int = 1024, temperature: float = 0.2, top_p: float = 0.9, retries: int = 3, timeout: int = REQUESTS_TIMEOUT) -> str:
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
        except requests.exceptions.HTTPError as e:
            last_err = e
            status = getattr(e.response, 'status_code', None)
            # on 429 wait and retry
            if status == 429:
                logger.warning("Rate limited by Ollama (429), backing off %s s", backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
                continue
            logger.exception("HTTP error calling Ollama: %s", e)
            break
        except requests.RequestException as e:
            last_err = e
            logger.warning("Request exception to Ollama: %s; retrying in %s s", e, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
            continue
    raise last_err if last_err else RuntimeError("Ollama request failed")


def safe_json_extract(text: str) -> Optional[dict]:
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


def clamp_int_0_10(x: int) -> int:
    try:
        return max(0, min(10, int(round(x))))
    except Exception:
        return 0


def score_with_rubric(question: str, reference_answer: str, candidate_answer: str, model: str = EVAL_MODEL) -> int:
    prompt = f"""{RUBRIC_PROMPT}

PREGUNTA:
{question}

RESPUESTA DE REFERENCIA:
{reference_answer}

RESPUESTA DEL MODELO:
{candidate_answer}
"""
    out = ask_ollama(prompt, model=model, num_predict=96, num_ctx=1536, temperature=0.0, top_p=1.0, retries=2)
    data = safe_json_extract(out) or {}
    ex = clamp_int_0_10(data.get("exactitud", 0))
    integ = clamp_int_0_10(data.get("integridad", 0))
    clar = clamp_int_0_10(data.get("claridad", 0))
    conc = clamp_int_0_10(data.get("concision", 0))
    util = clamp_int_0_10(data.get("utilidad", 0))
    final = int(round(0.40 * ex + 0.25 * integ + 0.20 * clar + 0.10 * conc + 0.05 * util))
    return max(1, min(10, final))


def build_prompt(title: Optional[str], content: Optional[str]) -> str:
    title = (title or "").strip()
    content = (content or "").strip()
    base = "Responde brevemente en 1-2 líneas y en español.\n"
    if title and content:
        return f"{base}Question: {title}\nDetail: {content[:350]}"
    q = title if title else content
    return f"{base}Question: {q}"


def run():
    consumer = KafkaConsumer(
        TOPIC_REQ,
        bootstrap_servers=[KAFKA_BOOTSTRAP],
        value_deserializer=lambda m: json.loads(m.decode("utf-8")),
        auto_offset_reset="earliest",
        group_id="llm-workers",
        enable_auto_commit=True,
    )
    producer = KafkaProducer(
        bootstrap_servers=[KAFKA_BOOTSTRAP],
        value_serializer=lambda v: json.dumps(v).encode("utf-8"),
    )

    logger.info("Worker started, listening to %s (bootstrap=%s)", TOPIC_REQ, KAFKA_BOOTSTRAP)
    for msg in consumer:
        try:
            payload = msg.value
            req_id = payload.get("id") or payload.get("qid") or hashlib_safe_id(payload)
            title = payload.get("title")
            content = payload.get("content")
            reference = payload.get("best_answer") or ""
            attempts = int(payload.get("attempts") or 0)
            question_text = (title or content or "").strip()
            logger.info("Processing request id=%s attempts=%s q='%s'", req_id, attempts, question_text[:80])

            prompt = build_prompt(title, content)

            # Generation
            try:
                answer = ask_ollama(prompt, model=GEN_MODEL, num_predict=96, num_ctx=1024, temperature=0.2, top_p=0.9, retries=3)
            except Exception as e:
                # classify error
                err_str = str(e)
                err_type = "other"
                if "429" in err_str or "Rate" in err_str:
                    err_type = "rate_limit"
                elif "timeout" in err_str.lower():
                    err_type = "timeout"
                logger.exception("Generation failed for id=%s (type=%s): %s", req_id, err_type, e)
                # publish to errors topic for retry handling
                producer.send(TOPIC_ERR, {"id": req_id, "title": title, "content": content, "best_answer": reference, "error_type": err_type, "attempts": attempts, "ts": now_iso(), "reason": err_str})
                continue

            # Successful generation: publish generated message (no score)
            out_msg = {
                "id": req_id,
                "title": title,
                "content": content,
                "best_answer": reference,
                "answer": answer,
                "attempts": attempts,
                "ts": now_iso(),
            }
            producer.send(TOPIC_RES, out_msg)
            logger.info("Published generated id=%s attempts=%s", req_id, attempts)
        except Exception as e:
            logger.exception("Unhandled error processing message: %s", e)


def hashlib_safe_id(payload: dict) -> str:
    import hashlib
    s = (payload.get("title") or "") + "|" + (payload.get("content") or "")
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


if __name__ == "__main__":
    run()
