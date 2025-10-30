#!/usr/bin/env python3
"""Retry service: consumes llm.errors and re-enqueues requests to llm.requests with backoff.

Handles error types: rate_limit (exponential backoff) and timeout/other (linear backoff).
After MAX_ATTEMPTS the message is sent to llm.failed for manual inspection.
"""
import os
import json
import time
import logging
from kafka import KafkaConsumer, KafkaProducer

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("llm-retry")

KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP", "kafka:9092")
TOPIC_ERR = os.getenv("TOPIC_ERR", "llm.errors")
TOPIC_REQ = os.getenv("TOPIC_REQ", "llm.requests")
TOPIC_FAILED = os.getenv("TOPIC_FAILED", "llm.failed")
MAX_ATTEMPTS = int(os.getenv("MAX_ATTEMPTS", "5"))
BASE_DELAY = float(os.getenv("BASE_DELAY", "1.0"))


def run():
    consumer = KafkaConsumer(
        TOPIC_ERR,
        bootstrap_servers=[KAFKA_BOOTSTRAP],
        value_deserializer=lambda m: json.loads(m.decode("utf-8")),
        auto_offset_reset="earliest",
        group_id="llm-retry",
        enable_auto_commit=True,
    )
    producer = KafkaProducer(
        bootstrap_servers=[KAFKA_BOOTSTRAP],
        value_serializer=lambda v: json.dumps(v).encode("utf-8"),
    )

    logger.info("Retry service started: consuming %s", TOPIC_ERR)
    for msg in consumer:
        try:
            data = msg.value
            req_id = data.get("id")
            attempts = int(data.get("attempts") or 0)
            err_type = data.get("error_type") or "other"

            attempts_next = attempts + 1
            if attempts_next > MAX_ATTEMPTS:
                logger.warning("Max attempts exceeded for id=%s (%s) — sending to %s", req_id, err_type, TOPIC_FAILED)
                producer.send(TOPIC_FAILED, {**data, "final": True})
                continue

            # compute delay
            if err_type == "rate_limit":
                delay = min(BASE_DELAY * (2 ** attempts), 60.0)
            elif err_type == "timeout":
                delay = min(BASE_DELAY * (attempts_next), 60.0)
            else:
                delay = min(BASE_DELAY * (attempts_next), 30.0)

            logger.info("Retrying id=%s type=%s attempt=%s after %s s", req_id, err_type, attempts_next, delay)
            time.sleep(delay)

            # re-enqueue original request with incremented attempts
            req = {"id": req_id, "title": data.get("title"), "content": data.get("content"), "best_answer": data.get("best_answer"), "attempts": attempts_next}
            producer.send(TOPIC_REQ, req)
        except Exception as e:
            logger.exception("Error handling failed message: %s", e)


if __name__ == "__main__":
    run()
