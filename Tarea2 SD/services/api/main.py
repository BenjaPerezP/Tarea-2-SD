import os, json, hashlib, time
from typing import Optional
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import requests
import redis
import psycopg2
import psycopg2.extras

# ========= Config =========
PGHOST = os.getenv("PGHOST", "db")
PGPORT = int(os.getenv("PGPORT", "5432"))
PGUSER = os.getenv("PGUSER", "sd")
PGPASSWORD = os.getenv("PGPASSWORD", "sdpass")
PGDATABASE = os.getenv("PGDATABASE", "sd")

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DB = int(os.getenv("REDIS_DB", "0"))
CACHE_TTL = int(os.getenv("CACHE_TTL", "3600"))  # 1h por defecto

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://host.docker.internal:11434")
GEN_MODEL = os.getenv("GEN_MODEL", "llama3.2:3b")
EVAL_MODEL = os.getenv("EVAL_MODEL", "llama3.2:1b")
NUM_THREAD = int(os.getenv("NUM_THREAD", "2"))
USE_KAFKA = os.getenv("USE_KAFKA", "0").lower() in ("1", "true", "yes")
KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP", "kafka:9092")
TOPIC_REQ = os.getenv("TOPIC_REQ", "llm.requests")

if USE_KAFKA:
    try:
        from kafka import KafkaProducer
        _kafka_producer = KafkaProducer(bootstrap_servers=[KAFKA_BOOTSTRAP], value_serializer=lambda v: json.dumps(v).encode("utf-8"))
    except Exception as e:
        _kafka_producer = None
        print("Warning: kafka-python not available or cannot connect:", e)

# ========= Helpers =========
def qhash(s: str) -> str:
    return hashlib.sha256(s.strip().lower().encode("utf-8")).hexdigest()

def ask_ollama(prompt: str, model: str, num_predict: int = 96, num_ctx: int = 1024, temperature: float = 0.2, top_p: float = 0.9) -> str:
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
    r = requests.post(f"{OLLAMA_HOST}/api/generate", json=payload, timeout=120)
    r.raise_for_status()
    return r.json().get("response", "")

RUBRIC_PROMPT = """Eres un evaluador objetivo. Evalúa la RESPUESTA DEL MODELO para la PREGUNTA dada,
tomando como referencia la RESPUESTA DE REFERENCIA. No penalices el idioma (EN/ES).
Asigna puntajes ENTEROS 0–10 para:
1) Exactitud (40%) 2) Integridad (25%) 3) Claridad (20%) 4) Concisión (10%) 5) Utilidad (5%)
Devuelve SOLO JSON: {"exactitud":int,"integridad":int,"claridad":int,"concision":int,"utilidad":int,"final":int}
"""

def score_with_rubric(question: str, reference_answer: str, candidate_answer: str) -> int:
    prompt = f"""{RUBRIC_PROMPT}

PREGUNTA:
{question}

RESPUESTA DE REFERENCIA:
{reference_answer}

RESPUESTA DEL MODELO:
{candidate_answer}
"""
    out = ask_ollama(prompt, model=EVAL_MODEL, num_predict=96, num_ctx=1536, temperature=0.0, top_p=1.0).strip()
    # extraer JSON robusto
    try:
        data = json.loads(out)
    except Exception:
        import re
        m = re.search(r"\{.*\}", out, flags=re.S)
        data = json.loads(m.group(0)) if m else {}
    def clamp(x): 
        try: return max(0, min(10, int(round(x))))
        except: return 0
    ex = clamp(data.get("exactitud", 0))
    integ = clamp(data.get("integridad", 0))
    clar = clamp(data.get("claridad", 0))
    conc = clamp(data.get("concision", 0))
    util = clamp(data.get("utilidad", 0))
    final = int(round(0.40*ex + 0.25*integ + 0.20*clar + 0.10*conc + 0.05*util))
    return max(1, min(10, final))

def build_prompt(title: str, content: str) -> str:
    base = "Responde brevemente en 1-2 líneas y en español.\n"
    if title and content:
        return f"{base}Question: {title}\nDetail: {content[:350]}"
    q = title if title else content
    return f"{base}Question: {q}"

def pg_conn():
    return psycopg2.connect(
        host=PGHOST, port=PGPORT, user=PGUSER, password=PGPASSWORD, dbname=PGDATABASE
    )


def get_latest_persisted(qh: str):
    """Return (answer, score) if a persisted response exists for question_hash, else None."""
    with pg_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT llm_answer, score FROM response_event
            WHERE question_hash = %s AND llm_answer IS NOT NULL
            ORDER BY ts DESC LIMIT 1
        """, (qh,))
        row = cur.fetchone()
        if row:
            return row[0], row[1]
    return None

def ensure_question(cur, qh: str, title: Optional[str], content: Optional[str], best_answer: Optional[str]):
    cur.execute("""
        INSERT INTO question (question_hash, title, content, best_answer)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (question_hash) DO UPDATE SET
          title = COALESCE(question.title, EXCLUDED.title),
          content = COALESCE(question.content, EXCLUDED.content),
          best_answer = COALESCE(question.best_answer, EXCLUDED.best_answer)
    """, (qh, title, content, best_answer))

def log_event(cur, qh: str, llm_answer: Optional[str], score: Optional[int], hit: bool):
    cur.execute("""
        INSERT INTO response_event (question_hash, llm_answer, score, hit)
        VALUES (%s, %s, %s, %s)
    """, (qh, llm_answer, score, hit))

def bump_counter(cur, qh: str):
    cur.execute("""
        INSERT INTO question_counter (question_hash, times_asked)
        VALUES (%s, 1)
        ON CONFLICT (question_hash) DO UPDATE SET times_asked = question_counter.times_asked + 1
    """, (qh,))

# ========= FastAPI =========
app = FastAPI()

class QueryIn(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    best_answer: Optional[str] = None  # opcional; si viene, se usa como referencia

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/query")
def query(q: QueryIn):
    question_text = (q.title or q.content or "").strip()
    if not question_text:
        raise HTTPException(status_code=400, detail="title o content requerido")

    qh = qhash(question_text)
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, decode_responses=True)

    # 1) Check storage (DB) first for persisted responses
    db_res = get_latest_persisted(qh)
    if db_res is not None:
        answer, score = db_res
        # populate cache for faster access
        try:
            r.setex(f"q:{qh}", CACHE_TTL, json.dumps({"answer": answer, "score": score}))
        except Exception:
            pass
        # persist hit event
        with pg_conn() as conn, conn.cursor() as cur:
            ensure_question(cur, qh, q.title, q.content, q.best_answer)
            bump_counter(cur, qh)
            log_event(cur, qh, None, score, True)
            conn.commit()
        return {"hit": True, "answer": answer, "score": score}

    # 2) Cache fallback (if DB had nothing)
    cache_key = f"q:{qh}"
    cached = r.get(cache_key)
    if cached:
        payload = json.loads(cached)
        # persistir hit
        with pg_conn() as conn, conn.cursor() as cur:
            ensure_question(cur, qh, q.title, q.content, q.best_answer)
            bump_counter(cur, qh)
            log_event(cur, qh, None, payload.get("score"), True)
            conn.commit()
        return {"hit": True, "answer": payload.get("answer"), "score": payload.get("score")}

    # 2) Miss → LLM + Score
    prompt = build_prompt(q.title or "", q.content or "")
    
    if USE_KAFKA and _kafka_producer is not None:
        msg = {"id": qh, "title": q.title, "content": q.content, "best_answer": q.best_answer}
        try:
            _kafka_producer.send(TOPIC_REQ, msg)
            _kafka_producer.flush()
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to enqueue request: {e}")

        # persistir evento de queueo
        with pg_conn() as conn, conn.cursor() as cur:
            ensure_question(cur, qh, q.title, q.content, q.best_answer)
            bump_counter(cur, qh)
            log_event(cur, qh, None, None, False)
            conn.commit()

        return {"queued": True, "id": qh}

    # fallback synchronous path (original behaviour)
    llm_answer = ask_ollama(prompt, model=GEN_MODEL, num_predict=96, num_ctx=1024, temperature=0.2, top_p=0.9)

    reference = q.best_answer or ""  # si no viene, igual puntuamos contra cadena vacía (o podrías leer de DB)
    score = score_with_rubric(question_text, reference, llm_answer)

    # 3) Persistencia + Cache set con TTL
    with pg_conn() as conn, conn.cursor() as cur:
        ensure_question(cur, qh, q.title, q.content, q.best_answer)
        bump_counter(cur, qh)
        log_event(cur, qh, llm_answer, score, False)
        conn.commit()

    r.setex(cache_key, CACHE_TTL, json.dumps({"answer": llm_answer, "score": score}))
    return {"hit": False, "answer": llm_answer, "score": score}

