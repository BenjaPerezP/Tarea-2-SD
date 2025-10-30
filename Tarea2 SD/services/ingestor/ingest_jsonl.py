#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os, json, time, sys
from typing import Optional
import psycopg2
import psycopg2.extras

STORAGE = os.getenv("STORAGE_JSONL", "/data/storage.jsonl")

PGHOST = os.getenv("PGHOST", "db")       # dentro de docker-compose usamos el nombre del servicio
PGPORT = int(os.getenv("PGPORT", "5432"))
PGUSER = os.getenv("PGUSER", "sd")
PGPASSWORD = os.getenv("PGPASSWORD", "sdpass")
PGDATABASE = os.getenv("PGDATABASE", "sd")

BATCH_SIZE = int(os.getenv("BATCH_SIZE", "200"))
SLEEP_SECS = float(os.getenv("SLEEP_SECS", "1.0"))
FROM_START = os.getenv("INGEST_FROM_START", "1") == "1"  # 1 = lee desde el inicio

def connect():
    return psycopg2.connect(
        host=PGHOST, port=PGPORT,
        user=PGUSER, password=PGPASSWORD, dbname=PGDATABASE
    )

def ensure_question(cur, rec):
    """Asegura que exista la fila en question. Si 'miss' trae más campos, los usa."""
    qh = rec["question_hash"]
    title = rec.get("question")          # en 'miss' viene la pregunta
    best = rec.get("reference_answer")   # en 'miss' viene la ref
    # puedes ampliar para class/content si los incluyes en el JSONL
    cur.execute("""
        INSERT INTO question (question_hash, title, best_answer)
        VALUES (%s, %s, %s)
        ON CONFLICT (question_hash) DO NOTHING
    """, (qh, title, best))
    return qh

def log_event(cur, rec):
    qh = rec["question_hash"]
    hit = (rec.get("type") == "hit")
    score = rec.get("score")
    ans = rec.get("llm_answer")
    ts = rec.get("ts")  # ISO 8601; Postgres lo puede parsear directo a timestamptz

    if ts is None:
        cur.execute("""
            INSERT INTO response_event (question_hash, llm_answer, score, hit)
            VALUES (%s, %s, %s, %s)
        """, (qh, ans, score, hit))
    else:
        cur.execute("""
            INSERT INTO response_event (ts, question_hash, llm_answer, score, hit)
            VALUES (%s, %s, %s, %s, %s)
        """, (ts, qh, ans, score, hit))

def inc_counter(cur, qh):
    cur.execute("""
        INSERT INTO question_counter (question_hash, times_asked)
        VALUES (%s, 1)
        ON CONFLICT (question_hash)
        DO UPDATE SET times_asked = question_counter.times_asked + 1
    """, (qh,))

def process_line(cur, line: str) -> bool:
    try:
        rec = json.loads(line)
    except Exception:
        return False
    t = rec.get("type")
    if t not in ("hit", "miss"):
        return False

    ensure_question(cur, rec)
    log_event(cur, rec)
    inc_counter(cur, rec["question_hash"])
    return True

def tail_file(path: str, from_start: bool):
    """Generador que sigue el archivo en vivo."""
    # Espera hasta que exista
    while not os.path.exists(path):
        print(f"[ingestor] Esperando {path} ...", file=sys.stderr); time.sleep(1)

    with open(path, "r", encoding="utf-8") as f:
        if not from_start:
            f.seek(0, os.SEEK_END)
        buf = []
        while True:
            line = f.readline()
            if not line:
                if buf:
                    yield from buf; buf.clear()
                time.sleep(SLEEP_SECS)
                continue
            yield line

def main():
    print("[ingestor] Conectando a Postgres...")
    conn = connect()
    conn.autocommit = False
    processed = 0
    try:
        with conn, conn.cursor() as cur:
            for line in tail_file(STORAGE, FROM_START):
                ok = process_line(cur, line)
                if not ok:
                    continue
                processed += 1
                if processed % BATCH_SIZE == 0:
                    conn.commit()
                    print(f"[ingestor] commit batch: {processed}", file=sys.stderr)
            # flush final si termina el archivo
            conn.commit()
    except KeyboardInterrupt:
        conn.commit()
    finally:
        conn.close()
        print(f"[ingestor] terminado. líneas procesadas={processed}")

if __name__ == "__main__":
    main()

