#!/usr/bin/env python3
"""Storage consumer: persists validated LLM results into Postgres and populates Redis cache.

Consumes topic `llm.validated` (configurable via env). For each message it:
 - ensures question row exists
 - inserts a response_event with llm_answer and score
 - sets Redis cache key `q:{question_hash}` with answer+score
"""
import os
import json
import logging
import hashlib
from kafka import KafkaConsumer
import psycopg2
import redis

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("llm-storage")

# Config
KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP", "kafka:9092")
TOPIC_VALID = os.getenv("TOPIC_VALID", "llm.validated")
PGHOST = os.getenv("PGHOST", "db")
PGPORT = int(os.getenv("PGPORT", "5432"))
PGUSER = os.getenv("PGUSER", "sd")
PGPASSWORD = os.getenv("PGPASSWORD", "sdpass")
PGDATABASE = os.getenv("PGDATABASE", "sd")
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
CACHE_TTL = int(os.getenv("CACHE_TTL", "3600"))


def qhash(s: str) -> str:
    return hashlib.sha256((s or "").strip().lower().encode("utf-8")).hexdigest()


def pg_conn():
    return psycopg2.connect(host=PGHOST, port=PGPORT, user=PGUSER, password=PGPASSWORD, dbname=PGDATABASE)


def run():
    consumer = KafkaConsumer(
        TOPIC_VALID,
        bootstrap_servers=[KAFKA_BOOTSTRAP],
        value_deserializer=lambda m: json.loads(m.decode("utf-8")),
        auto_offset_reset="earliest",
        group_id="llm-storage",
        enable_auto_commit=True,
    )
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0, decode_responses=True)

    logger.info("Storage consumer started: consuming %s", TOPIC_VALID)
    for msg in consumer:
        try:
            data = msg.value
            q_text = (data.get("title") or data.get("content") or "").strip()
            qh = qhash(q_text)
            answer = data.get("answer")
            score = int(data.get("score") or 0)

            with pg_conn() as conn, conn.cursor() as cur:
                # ensure question
                cur.execute("""
                    INSERT INTO question (question_hash, title, content, best_answer)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (question_hash) DO UPDATE SET
                      title = COALESCE(question.title, EXCLUDED.title),
                      content = COALESCE(question.content, EXCLUDED.content),
                      best_answer = COALESCE(question.best_answer, EXCLUDED.best_answer)
                """, (qh, data.get("title"), data.get("content"), data.get("best_answer")))

                # insert response_event
                cur.execute("""
                    INSERT INTO response_event (question_hash, llm_answer, score, hit)
                    VALUES (%s, %s, %s, %s)
                """, (qh, answer, score, False))
                conn.commit()

            
            try:
                r.setex(f"q:{qh}", CACHE_TTL, json.dumps({"answer": answer, "score": score}))
            except Exception:
                logger.exception("Failed to set cache for %s", qh)

            logger.info("Persisted id=%s qh=%s score=%s", data.get("id"), qh, score)
        except Exception as e:
            logger.exception("Error persisting validated message: %s", e)


if __name__ == "__main__":
    run()
