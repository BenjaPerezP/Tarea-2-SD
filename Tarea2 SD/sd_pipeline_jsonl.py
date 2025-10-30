#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sd_pipeline_jsonl.py — Monolítico con:
 - Filtro del CSV (para cubrir 15.000 consultas)
 - Generador de tráfico (Poisson/Lognormal)
 - Caché LRU en memoria + LOG de caché en JSONL (stats periódicas)
 - Almacenamiento en JSONL (eventos HIT/MISS y datos relevantes)
 - Cliente Ollama (CPU-only OK)
 - Métrica de calidad por RÚBRICA (1–10) usando el LLM como juez:
     1) Exactitud (40%)
     2) Integridad (25%)
     3) Claridad (20%)
     4) Concisión (10%)
     5) Utilidad (5%)
   Se guarda {"score": <int 1..10>} en cada MISS.

Requisitos:
  pip install pandas requests

Ejemplos:
  1) Filtrar CSV:
     python sd_pipeline_jsonl.py \
       --input test.csv --output-subset test_subset.csv \
       --target 15000 --per-row 3 --sample stratified

  2) Ejecutar tráfico (con juez 1b y generador 3b):
     ollama pull llama3.2:3b
     ollama pull llama3.2:1b
     python sd_pipeline_jsonl.py --run \
       --csv test_subset.csv \
       --target 15000 \
       --distribution poisson --rate 1 --sleep \
       --cache-jsonl cache.jsonl --storage-jsonl storage.jsonl \
       --model llama3.2:3b --eval-model llama3.2:1b \
       --threads 2 --jsonl-stats-every 200 \
       --timeout-gen 300 --timeout-eval 600
"""

import argparse
import os
import json
import math
import sys
import time
import random
import hashlib
import re
from collections import OrderedDict
from datetime import datetime
from typing import Optional, Dict, Any

import pandas as pd
import requests

# ==========================
# 1) CSV: lectura + filtrado
# ==========================

def read_csv_safely(path: str) -> pd.DataFrame:
    """Lee CSV con fallback de encoding y normaliza cabeceras comunes."""
    for enc in ("utf-8", "latin-1"):
        for header in (0, None):
            try:
                df = pd.read_csv(path, encoding=enc, header=header)
                # Si no hay header y tiene 4 columnas, asume formato Yahoo Answers clásico.
                if header is None and df.shape[1] == 4:
                    df.columns = ["class", "title", "content", "best_answer"]
                return df
            except UnicodeDecodeError:
                continue
            except pd.errors.ParserError:
                continue
    raise RuntimeError("No se pudo leer el CSV (revisa delimitador/encoding/cabecera).")

def ensure_class_column(df: pd.DataFrame) -> Optional[str]:
    """Busca columna de clase/categoría o infiere por cardinalidad baja."""
    for c in ["class", "category", "label", "topic"]:
        if c in df.columns:
            return c
    first = df.columns[0]
    try:
        uniq = pd.to_numeric(df[first], errors="coerce").dropna().astype(int).unique()
        if len(uniq) <= 20:
            return first
    except Exception:
        pass
    return None

def pick_rows(df: pd.DataFrame, target: int, per_row: Optional[int],
              from_column: Optional[str], sample: str, seed: int) -> pd.DataFrame:
    """Elige filas suficientes para cubrir 'target' consultas."""
    if from_column and per_row:
        raise ValueError("Usa --from-column o --per-row, no ambos.")
    if not from_column and not per_row:
        per_row = 1

    if from_column:
        if from_column not in df.columns:
            raise ValueError(f"No existe la columna '{from_column}'.")
        if not pd.api.types.is_numeric_dtype(df[from_column]):
            raise ValueError(f"'{from_column}' debe ser numérica.")
        work = df.copy()
        if sample == "random":
            work = work.sample(frac=1.0, random_state=seed).reset_index(drop=True)
        elif sample == "stratified":
            class_col = ensure_class_column(work)
            if class_col is None:
                work = work.sample(frac=1.0, random_state=seed).reset_index(drop=True)
            else:
                work = (work.groupby(class_col, group_keys=False)
                            .apply(lambda g: g.sample(frac=1.0, random_state=seed))
                            .reset_index(drop=True))
        cum = work[from_column].cumsum()
        idx = (cum >= target).idxmax() if (cum >= target).any() else len(work) - 1
        return work.iloc[: idx + 1].reset_index(drop=True)

    # per_row constante
    assert per_row is not None and per_row > 0, "--per-row debe ser entero positivo"
    needed_rows = math.ceil(target / per_row)

    if sample not in {"head", "random", "stratified"}:
        raise ValueError("--sample debe ser head|random|stratified")

    if sample == "head":
        picked = df.head(needed_rows)
    elif sample == "random":
        picked = df.sample(n=min(needed_rows, len(df)), random_state=seed)
    else:
        class_col = ensure_class_column(df)
        if class_col is None:
            picked = df.sample(n=min(needed_rows, len(df)), random_state=seed)
        else:
            counts = df[class_col].value_counts(normalize=True)
            alloc = (counts * needed_rows).round().astype(int)
            diff = needed_rows - alloc.sum()
            if diff != 0:
                ordered = counts.sort_values(ascending=False).index.tolist()
                i = 0
                while diff != 0 and i < len(ordered):
                    c = ordered[i]
                    alloc[c] += 1 if diff > 0 else -1
                    diff += -1 if diff > 0 else 1
                    i = (i + 1) % len(ordered)
            parts = []
            for c, k in alloc.items():
                if k <= 0:
                    continue
                g = df[df[class_col] == c]
                take = min(k, len(g))
                parts.append(g.sample(n=take, random_state=seed))
            picked = pd.concat(parts, axis=0).sample(frac=1.0, random_state=seed)

    return picked.reset_index(drop=True)

# ===========================
# 2) LLM (Ollama) + utilidades
# ===========================

DEFAULT_MODEL = "llama3.2:3b"

def ask_ollama(prompt: str,
               model: str = DEFAULT_MODEL,
               host: str = "http://127.0.0.1:11434",
               timeout: int = 300,            # ↑ timeout por defecto (generación)
               num_ctx: int = 1024,
               num_predict: int = 64,
               temperature: float = 0.2,
               top_p: float = 0.9,
               num_thread: int = 2,
               keep_alive = -1,
               format_json: bool = False,
               retries: int = 3) -> str:
    """Llama a Ollama con reintentos y timeout configurables."""
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "keep_alive": keep_alive,
        "options": {
            "num_ctx": num_ctx,
            "num_predict": num_predict,
            "temperature": temperature,
            "top_p": top_p,
            "num_thread": num_thread,
        },
    }
    if format_json:
        payload["format"] = "json"

    backoff = 2.0
    last_err = None
    for _ in range(max(1, retries)):
        try:
            r = requests.post(f"{host}/api/generate", json=payload, timeout=timeout)
            r.raise_for_status()
            data = r.json()
            return data.get("response", "")
        except requests.exceptions.ReadTimeout as e:
            last_err = e
            time.sleep(backoff); backoff *= 2
        except requests.RequestException as e:
            last_err = e
            time.sleep(backoff); backoff *= 2
    raise last_err if last_err else RuntimeError("Ollama request failed")

# =============
# 3) Evaluador
# =============

RUBRIC_PROMPT = """Eres un evaluador objetivo. Evalúa la RESPUESTA DEL MODELO para la PREGUNTA dada,
tomando como referencia la RESPUESTA DE REFERENCIA. No penalices el idioma (puede estar en EN o ES);
evalúa el contenido semántico. Asigna puntajes ENTEROS 0–10 para:
1) Exactitud (40%): ¿es correcta la información?
2) Integridad (25%): ¿cubre los puntos clave?
3) Claridad (20%): ¿se entiende?
4) Concisión (10%): ¿va al grano sin relleno?
5) Utilidad (5%): ¿sirve al usuario?

Luego calcula el puntaje FINAL redondeando al entero más cercano de:
0.40*Exactitud + 0.25*Integridad + 0.20*Claridad + 0.10*Concision + 0.05*Utilidad.

Devuelve SOLO un JSON con claves en minúscula:
{"exactitud": int, "integridad": int, "claridad": int, "concision": int, "utilidad": int, "final": int}
Nada de texto adicional.
"""

def safe_json_extract(text: str) -> Optional[Dict[str, Any]]:
    """Extrae el primer objeto JSON válido del texto (best-effort)."""
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r'\{.*\}', text, flags=re.S)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except Exception:
            return None

def clamp_int_0_10(x: int) -> int:
    return max(0, min(10, int(round(x))))

def score_with_rubric(question: str, reference_answer: str, candidate_answer: str,
                      model: str = DEFAULT_MODEL, num_thread: int = 2, timeout: int = 600) -> int:
    """Usa el LLM para puntuar según la rúbrica y retorna un entero 1–10."""
    prompt = f"""{RUBRIC_PROMPT}

PREGUNTA:
{question}

RESPUESTA DE REFERENCIA:
{reference_answer}

RESPUESTA DEL MODELO:
{candidate_answer}
"""
    out = ask_ollama(
        prompt,
        model=model,
        num_ctx=1024,
        num_predict=64,       # juez más corto para CPU
        temperature=0.0,
        top_p=1.0,
        num_thread=num_thread,
        keep_alive=-1,
        timeout=timeout,
        format_json=True
    ).strip()

    data = safe_json_extract(out) or {}
    ex = clamp_int_0_10(data.get("exactitud", 0))
    integ = clamp_int_0_10(data.get("integridad", 0))
    clar = clamp_int_0_10(data.get("claridad", 0))
    conc = clamp_int_0_10(data.get("concision", 0))
    util = clamp_int_0_10(data.get("utilidad", 0))

    weighted = 0.40*ex + 0.25*integ + 0.20*clar + 0.10*conc + 0.05*util
    final_int = int(round(weighted))
    final_int = max(1, min(10, final_int))
    return final_int

# ===========================
# 4) Utilidades JSONL + LRU
# ===========================

def now_utc():
    return datetime.utcnow().isoformat() + "Z"

def _key_hash(s: str) -> str:
    return hashlib.sha256(s.strip().lower().encode("utf-8")).hexdigest()

def append_jsonl(path: str, obj: Dict[str, Any]):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")

class JSONLLRUCache:
    """LRU en memoria + logging periódico a JSONL (stats y eventos)."""
    def __init__(self, capacity: int, jsonl_path: str, stats_every_events: int, stats_every_seconds: float):
        self.capacity = capacity
        self.data = OrderedDict()
        self.hits = 0
        self.misses = 0
        self.jsonl_path = jsonl_path
        self.stats_every_events = max(1, stats_every_events)
        self.stats_every_seconds = max(0.1, stats_every_seconds)
        self._events_since = 0
        self._last_stats_ts = time.time()

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        if key in self.data:
            val = self.data.pop(key)
            self.data[key] = val
            self.hits += 1
            self._events_since += 1
            return val
        self.misses += 1
        self._events_since += 1
        return None

    def set(self, key: str, value: Dict[str, Any]) -> None:
        if key in self.data:
            self.data.pop(key)
        self.data[key] = value
        while len(self.data) > self.capacity:
            self.data.popitem(last=False)
        self._events_since += 1

    def maybe_log_stats(self, force: bool=False):
        nowt = time.time()
        if force or self._events_since >= self.stats_every_events or (nowt - self._last_stats_ts) >= self.stats_every_seconds:
            append_jsonl(self.jsonl_path, {
                "type": "cache_stats",
                "ts": now_utc(),
                "hits": self.hits,
                "misses": self.misses,
                "size": len(self.data),
                "capacity": self.capacity
            })
            self._events_since = 0
            self._last_stats_ts = nowt

# ===========================
# 5) Almacenamiento JSONL
# ===========================

class JSONLStorage:
    """Append-only JSONL de eventos (HIT/MISS) y contenido relevante."""
    def __init__(self, jsonl_path: str):
        self.path = jsonl_path

    def log_hit(self, question: str):
        append_jsonl(self.path, {
            "type": "hit",
            "ts": now_utc(),
            "question_hash": _key_hash(question)
        })

    def log_miss(self, question: str, reference: str, llm_answer: str, score_int_1_10: int):
        append_jsonl(self.path, {
            "type": "miss",
            "ts": now_utc(),
            "question_hash": _key_hash(question),
            "question": question,
            "reference_answer": reference,
            "llm_answer": llm_answer,
            "score": int(score_int_1_10)
        })

# ===========================
# 6) Pipeline + Generador
# ===========================

def build_prompt(row: pd.Series) -> str:
    title = str(row.get("title", "")).strip()
    content = str(row.get("content", "")).strip()
    # Cambia a inglés si prefieres comparar con ref EN; aquí lo dejamos en español:
    base = "Responde brevemente en 1-2 líneas y en español.\n"
    if title and content:
        return f"{base}Question: {title}\nDetail: {content[:350]}"
    q = title if title else content
    return f"{base}Question: {q}"

def select_question(row: pd.Series) -> str:
    title = str(row.get("title", "")).strip()
    content = str(row.get("content", "")).strip()
    return title if title else content

def interarrival(dist: str, rate: float, mu: float, sigma: float) -> float:
    if dist == "poisson":
        return random.expovariate(rate) if rate > 0 else 0.0
    elif dist == "lognormal":
        return random.lognormvariate(mu, sigma)
    else:
        return 0.0

class Pipeline:
    def __init__(self, cache_capacity: int, cache_jsonl: str, storage_jsonl: str,
                 model: str, num_thread: int, stats_every_events: int, stats_every_seconds: float,
                 eval_model: Optional[str] = None,
                 timeout_gen: int = 300, timeout_eval: int = 600):
        self.cache = JSONLLRUCache(cache_capacity, cache_jsonl, stats_every_events, stats_every_seconds)
        self.storage = JSONLStorage(storage_jsonl)
        self.model = model
        self.eval_model = eval_model or model
        self.num_thread = num_thread
        self.timeout_gen = timeout_gen
        self.timeout_eval = timeout_eval

    def handle(self, question: str, reference_answer: str, prompt: str) -> Dict[str, Any]:
        k = _key_hash(question)
        cached = self.cache.get(k)
        if cached is not None:
            self.storage.log_hit(question)
            self.cache.maybe_log_stats()
            return {"hit": True, "answer": cached["answer"], "score": cached["score"]}

        # MISS -> LLM -> evaluar con rúbrica -> log -> cache
        llm_answer = ask_ollama(
            prompt,
            model=self.model,
            num_ctx=1024,
            num_predict=96,            # evitar corte sin exagerar
            temperature=0.2,
            top_p=0.9,
            num_thread=self.num_thread,
            keep_alive=-1,
            timeout=self.timeout_gen,
        )

        score_int = score_with_rubric(
            question, reference_answer or "", llm_answer or "",
            model=self.eval_model,
            num_thread=self.num_thread,
            timeout=self.timeout_eval
        )
        self.storage.log_miss(question, reference_answer or "", llm_answer or "", score_int)
        self.cache.set(k, {"answer": llm_answer, "score": score_int})
        self.cache.maybe_log_stats()
        return {"hit": False, "answer": llm_answer, "score": score_int}

# ===========================
# 7) CLI
# ===========================

def main():
    ap = argparse.ArgumentParser(description="Filtro + Tráfico + Caché JSONL + Storage JSONL + Rúbrica (1–10) + Ollama")
    # Filtrado
    ap.add_argument("--input", help="Ruta a test.csv")
    ap.add_argument("--output-subset", default="test_subset.csv", help="Archivo CSV filtrado a generar")
    ap.add_argument("--target", type=int, default=15000, help="N° total de consultas objetivo")
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--per-row", type=int, help="Consultas por fila (constante)")
    group.add_argument("--from-column", type=str, help="Columna numérica con consultas por fila")
    ap.add_argument("--sample", choices=["head","random","stratified"], default="stratified", help="Estrategia de muestreo")

    # Ejecución (tráfico)
    ap.add_argument("--run", action="store_true", help="Ejecuta el generador de tráfico")
    ap.add_argument("--csv", help="CSV a usar para tráfico (por defecto: --output-subset)")
    ap.add_argument("--distribution", choices=["poisson","lognormal"], default="poisson", help="Distribución de arribo")
    ap.add_argument("--rate", type=float, default=5.0, help="λ de Poisson (eventos/seg)")
    ap.add_argument("--mu", type=float, default=-2.0, help="mu Lognormal (log)")
    ap.add_argument("--sigma", type=float, default=1.0, help="sigma Lognormal (log)")
    ap.add_argument("--sleep", action="store_true", help="Dormir entre arrivos simulados")
    ap.add_argument("--report-every", type=int, default=500, help="Frecuencia de logs a consola")

    # Caché/Storage JSONL
    ap.add_argument("--cache-jsonl", default="cache.jsonl", help="Ruta al LOG JSONL de caché")
    ap.add_argument("--storage-jsonl", default="storage.jsonl", help="Ruta al LOG JSONL de almacenamiento")
    ap.add_argument("--cache-capacity", type=int, default=5000, help="Capacidad LRU")
    ap.add_argument("--jsonl-stats-every", type=int, default=200, help="Cada cuántos eventos se agrega línea 'cache_stats'")
    ap.add_argument("--jsonl-stats-seconds", type=float, default=2.5, help="Intervalo máximo (seg) entre líneas 'cache_stats'")

    # Ollama/CPU + juez/tiempos
    ap.add_argument("--model", default=DEFAULT_MODEL, help="Modelo de Ollama para responder")
    ap.add_argument("--eval-model", default=None, help="Modelo para el juez (por defecto usa --model). Ej: llama3.2:1b")
    ap.add_argument("--threads", type=int, default=2, help="num_thread por petición")
    ap.add_argument("--timeout-gen", type=int, default=300, help="Timeout (s) para generación de respuesta")
    ap.add_argument("--timeout-eval", type=int, default=600, help="Timeout (s) para evaluación por rúbrica")

    args = ap.parse_args()

    # Paso 1: filtrado (si se entrega --input)
    if args.input:
        df = read_csv_safely(args.input)
        picked = pick_rows(df, target=args.target, per_row=args.per_row,
                           from_column=args.from_column, sample=args.sample, seed=42)
        picked.to_csv(args.output_subset, index=False, encoding="utf-8")
        print(f"[OK] Escribí {len(picked)} filas a {args.output_subset} para cubrir ~{args.target} consultas.")
        if not args.run:
            return

    # Paso 2: ejecución del tráfico
    if args.run:
        csv_path = args.csv or args.output_subset
        if not os.path.exists(csv_path):
            print(f"[ERROR] No encuentro el CSV para tráfico: {csv_path}", file=sys.stderr)
            sys.exit(1)

        df = pd.read_csv(csv_path)
        # Normalización de cabeceras típicas
        if df.shape[1] == 4 and "title" not in df.columns and "content" not in df.columns:
            df.columns = ["class","title","content","best_answer"]
        if "best_answer" not in df.columns:
            for alias in ["best answer","bestanswer","answer","reference"]:
                if alias in df.columns:
                    df.rename(columns={alias: "best_answer"}, inplace=True)
                    break
        if "title" not in df.columns and "question" in df.columns:
            df.rename(columns={"question": "title"}, inplace=True)

        pipe = Pipeline(
            cache_capacity=args.cache_capacity,
            cache_jsonl=args.cache_jsonl,
            storage_jsonl=args.storage_jsonl,
            model=args.model,
            num_thread=args.threads,
            stats_every_events=args.jsonl_stats_every,
            stats_every_seconds=args.jsonl_stats_seconds,
            eval_model=args.eval_model,
            timeout_gen=args.timeout_gen,
            timeout_eval=args.timeout_eval,
        )

        n = args.target
        rng = random.Random(42)
        start = time.time()

        for i in range(1, n + 1):
            row = df.iloc[rng.randrange(len(df))]
            question = select_question(row)
            prompt = build_prompt(row)
            reference = str(row.get("best_answer", ""))

            t0 = time.time()
            res = pipe.handle(question, reference, prompt)
            latency = time.time() - t0

            if i % args.report_every == 0 or i == n:
                elapsed = time.time() - start
                print(f"[{i}/{n}] hit={res['hit']} score={res['score']} latency={latency:.2f}s elapsed={elapsed:.1f}s")

            if args.sleep:
                dt = interarrival(args.distribution, args.rate, args.mu, args.sigma)
                time.sleep(min(dt, 2.0))

        pipe.cache.maybe_log_stats(force=True)
        print("[OK] Ejecución finalizada. Revisa cache.jsonl y storage.jsonl.")

    else:
        if not args.input:
            print("Nada que hacer: usa --input para filtrar y/o --run para ejecutar tráfico.")
            ap.print_help()

if __name__ == "__main__":
    main()
