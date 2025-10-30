"""PyFlink job skeleton: consume `llm.responses`, filter low-score and publish to `llm.regenerate`.

This file is a starting point for deploying a PyFlink job. It requires the Kafka connector jars
and proper Flink cluster configuration. For local testing, use `services/monitor/monitor.py`.
"""
"""
PyFlink job: consume `llm.generated`, compute score using the same rubric (via external judge call),
and either publish validated results to `llm.validated` or re-inject to `llm.requests` with attempts++.

This job is a skeleton showing how the Flink job should behave. Running it requires Flink with the
Kafka connector and proper environment (not runnable inside the simple Docker image without extra setup).
For local testing a lightweight Python monitor is provided (`services/monitor/monitor.py`).
"""
import os
import json
import time
import re
import requests
from pyflink.datastream import StreamExecutionEnvironment
from pyflink.common.serialization import SimpleStringSchema
from pyflink.datastream.connectors import FlinkKafkaConsumer, FlinkKafkaProducer


BOOTSTRAP = os.getenv('KAFKA_BOOTSTRAP', 'kafka:9092')
THRESHOLD = int(os.getenv('REGEN_THRESHOLD', '6'))
MAX_ATTEMPTS = int(os.getenv('MAX_ATTEMPTS', '3'))
EVAL_MODEL = os.getenv('EVAL_MODEL', 'llama3.2:1b')
OLLAMA_HOST = os.getenv('OLLAMA_HOST', 'http://127.0.0.1:11434')
NUM_THREAD = int(os.getenv('NUM_THREAD', '2'))


RUBRIC_PROMPT = """Eres un evaluador objetivo. Evalúa la RESPUESTA DEL MODELO para la PREGUNTA dada,
tomando como referencia la RESPUESTA DE REFERENCIA. No penalices el idioma (EN/ES).
Asigna puntajes ENTEROS 0–10 para:
1) Exactitud (40%) 2) Integridad (25%) 3) Claridad (20%) 4) Concisión (10%) 5) Utilidad (5%)
Devuelve SOLO JSON: {"exactitud":int,"integridad":int,"claridad":int,"concision":int,"utilidad":int,"final":int}
"""


def ask_ollama(prompt: str, model: str, num_predict: int = 96, num_ctx: int = 1536, temperature: float = 0.0, top_p: float = 1.0, retries: int = 2, timeout: int = 120) -> str:
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
            return r.json().get('response','')
        except Exception as e:
            last_err = e
            time.sleep(backoff)
            backoff = min(backoff*2, 30)
    raise last_err if last_err else RuntimeError('Ollama request failed')


def safe_json_extract(text: str):
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, flags=re.S)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except Exception:
            return None


def score_with_rubric(question: str, reference: str, candidate: str) -> int:
    prompt = f"""{RUBRIC_PROMPT}

PREGUNTA:
{question}

RESPUESTA DE REFERENCIA:
{reference}

RESPUESTA DEL MODELO:
{candidate}
"""
    out = ask_ollama(prompt, model=EVAL_MODEL, num_predict=96, num_ctx=1536, temperature=0.0, top_p=1.0, retries=2)
    data = safe_json_extract(out) or {}
    def clamp(x):
        try: return max(0, min(10, int(round(x))))
        except: return 0
    ex = clamp(data.get('exactitud',0))
    integ = clamp(data.get('integridad',0))
    clar = clamp(data.get('claridad',0))
    conc = clamp(data.get('concision',0))
    util = clamp(data.get('utilidad',0))
    final = int(round(0.40*ex + 0.25*integ + 0.20*clar + 0.10*conc + 0.05*util))
    return max(0, min(10, final))


def main():
    env = StreamExecutionEnvironment.get_execution_environment()
    props = {'bootstrap.servers': BOOTSTRAP, 'group.id': 'flink-llm'}
    consumer = FlinkKafkaConsumer(topics='llm.generated', deserialization_schema=SimpleStringSchema(), properties=props)
    producer_valid = FlinkKafkaProducer(topic='llm.validated', serialization_schema=SimpleStringSchema(), producer_config={'bootstrap.servers': BOOTSTRAP})
    producer_req = FlinkKafkaProducer(topic='llm.requests', serialization_schema=SimpleStringSchema(), producer_config={'bootstrap.servers': BOOTSTRAP})

    ds = env.add_source(consumer)

    def process_record(x: str):
        obj = safe_json_extract(x)
        if not obj:
            return None
        req_id = obj.get('id')
        attempts = int(obj.get('attempts') or 0)
        question = (obj.get('title') or obj.get('content') or '').strip()
        answer = obj.get('answer') or ''
        reference = obj.get('best_answer') or ''

        try:
            score = score_with_rubric(question, reference, answer)
        except Exception:
            score = 0

        if score >= THRESHOLD:
            return json.dumps({'id': req_id, 'title': obj.get('title'), 'content': obj.get('content'), 'best_answer': reference, 'answer': answer, 'score': int(score), 'attempts': attempts})
        else:
            if attempts + 1 < MAX_ATTEMPTS:
                return json.dumps({'__requeue__': True, 'payload': {'id': req_id, 'title': obj.get('title'), 'content': obj.get('content'), 'best_answer': reference, 'attempts': attempts + 1}})
            else:
                # give up and persist as final
                return json.dumps({'id': req_id, 'title': obj.get('title'), 'content': obj.get('content'), 'best_answer': reference, 'answer': answer, 'score': int(score), 'attempts': attempts, 'final': True})

    mapped = ds.map(lambda s: process_record(s), output_type=SimpleStringSchema())

    # split validated results vs requeues by content check
    def route(x: str):
        try:
            obj = json.loads(x)
        except Exception:
            return None
        if obj.get('__requeue__'):
            return ('requeue', json.dumps(obj.get('payload')))
        else:
            return ('valid', x)

    routed = mapped.map(lambda s: route(s), output_type=SimpleStringSchema())

    # In practice, implement splitting into two streams; here we keep the skeleton
    # TODO: implement split and sinks properly using side outputs

    env.execute('llm-quality-monitor')


if __name__ == '__main__':
    main()
