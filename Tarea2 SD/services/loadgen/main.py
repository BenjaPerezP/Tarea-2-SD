# services/loadgen/main.py
import os, time, math, random, requests, psycopg2, psycopg2.extras
from datetime import datetime

API_URL = os.getenv("API_URL", "http://api:8000/query")

# distribución
DISTR = os.getenv("DISTR", "poisson")   # "poisson" | "lognormal"
RATE  = float(os.getenv("RATE", "2"))   # λ para Poisson (req/s)
MU    = float(os.getenv("MU", "-2.0"))  # lognormal mu
SIGMA = float(os.getenv("SIGMA", "1.0"))# lognormal sigma
DUR_S = int(os.getenv("DURATION", "120"))
SEED  = int(os.getenv("SEED", "42"))
SLEEP = os.getenv("SLEEP", "1") == "1"

# DB para cargar preguntas a memoria (rápido y simple)
PGHOST = os.getenv("PGHOST", "db")
PGPORT = int(os.getenv("PGPORT", "5432"))
PGUSER = os.getenv("PGUSER", "sd")
PGPASS = os.getenv("PGPASSWORD", "sdpass")
PGDB   = os.getenv("PGDATABASE", "sd")

random.seed(SEED)

def interarrival():
    if DISTR == "poisson":
        return random.expovariate(RATE) if RATE > 0 else 0.0
    return random.lognormvariate(MU, SIGMA)

def load_questions():
    conn = psycopg2.connect(host=PGHOST, port=PGPORT, user=PGUSER, password=PGPASS, dbname=PGDB)
    with conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("SELECT title, content, best_answer FROM question")
        rows = cur.fetchall()
    conn.close()
    return rows

def pick_q(rows):
    r = rows[random.randrange(len(rows))]
    title = (r["title"] or "").strip()
    content = (r["content"] or "").strip()
    best = (r["best_answer"] or "").strip()
    return title, content, best

def main():
    rows = load_questions()
    t_end = time.time() + DUR_S
    i = 0
    while time.time() < t_end:
        i += 1
        title, content, best = pick_q(rows)
        payload = {"title": title, "content": content, "best_answer": best}
        t0 = time.time()
        try:
            r = requests.post(API_URL, json=payload, timeout=120)
            r.raise_for_status()
            took = time.time() - t0
            data = r.json()
            # Log mínimo para auditar:
            print(f"[{i}] hit={data.get('hit')} score={data.get('score')} latency={took:.2f}s")
        except Exception as e:
            print(f"[{i}] ERROR {e}")
        if SLEEP:
            time.sleep(min(interarrival(), 2.0))

if __name__ == "__main__":
    main()

