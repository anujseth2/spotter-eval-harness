#!/usr/bin/env python3
"""Streaming Spotter client (SSE) - same transport the Spotter UI uses.
Avoids the synchronous /send gateway cap by streaming /send/stream."""
import sys, os, json, time, urllib.request, urllib.error
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import ts_client


def _create_conv(host, token, timeout=60):
    body = {"metadata_context": {"type": "AUTO_MODE"}, "conversation_settings": {}}
    req = urllib.request.Request(host + "/api/rest/2.0/ai/agent/conversation/create",
                                 data=json.dumps(body).encode(), method="POST")
    for k, v in {"Content-Type": "application/json", "Accept": "application/json",
                 "Authorization": "Bearer " + token}.items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())["conversation_identifier"]


def stream_answer(host, token, question, timeout=300, debug=False):
    """Returns distilled dict; streams so long agentic runs don't hit the sync gateway cap."""
    out = {"produced_answer": False, "chosen_worksheet_id": None, "tml_tokens": None,
           "sage_query": None, "chart_type": None, "answer_title": None, "narrative": None, "error": None}
    conv = _create_conv(host, token)
    url = host + f"/api/rest/2.0/ai/agent/conversation/{conv}/send/stream"
    req = urllib.request.Request(url, data=json.dumps({"messages": [question]}).encode(), method="POST")
    for k, v in {"Content-Type": "application/json", "Accept": "text/event-stream",
                 "Authorization": "Bearer " + token}.items():
        req.add_header(k, v)
    answers, narratives = [], []
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").rstrip("\n")
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    evs = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if isinstance(evs, dict):
                    evs = [evs]
                for e in evs:
                    et = e.get("type")
                    if et in ("text", "text-chunk"):
                        md = e.get("metadata", {}) or {}
                        if md.get("type") == "text":
                            txt = e.get("content") or e.get("text") or ""
                            if txt:
                                narratives.append(txt)
                    elif et == "answer":
                        answers.append(e)
                    elif et == "error":
                        out["error"] = json.dumps(e)[:300]
    except urllib.error.HTTPError as e:
        out["error"] = f"HTTP {e.code}: {e.read().decode()[:150]}"; return out
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"; return out

    if debug and answers:
        print("ANSWER EVENT KEYS:", list(answers[-1].keys()))
        print("ANSWER METADATA KEYS:", list((answers[-1].get('metadata') or {}).keys()))
        print(json.dumps(answers[-1], indent=2)[:1500])

    if answers:
        a = answers[-1]; md = a.get("metadata", {}) or {}
        out.update(produced_answer=True,
                   chosen_worksheet_id=md.get("worksheet_id"),
                   tml_tokens=a.get("tml_tokens") or md.get("tml_tokens") or md.get("tml_phrases"),
                   sage_query=a.get("sage_query") or md.get("sage_query"),
                   chart_type=md.get("chart_type"),
                   answer_title=a.get("title"))
    # narrative: join text chunks, drop json-dump-looking blocks
    joined = "".join(narratives).strip()
    out["narrative"] = joined if joined else None
    return out


if __name__ == "__main__":
    env = ts_client.load_env(); host = env["TS_CLUSTER"].rstrip("/")
    tok = ts_client.mint_token(env, 1800)
    q = os.environ.get("SPOTTER_TEST_QUESTION", "What are my top 10 customers by revenue this year?")
    t0 = time.time()
    r = stream_answer(host, tok, q, debug=True)
    print(f"\nelapsed {round(time.time()-t0,1)}s produced={r['produced_answer']}")
    print("worksheet:", r["chosen_worksheet_id"], "| chart:", r["chart_type"])
    print("sage:", r["sage_query"])
    print("tokens:", r["tml_tokens"])
    print("narrative:", (r["narrative"] or "")[:200])
