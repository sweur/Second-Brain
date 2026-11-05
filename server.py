#!/usr/bin/env python3
"""Second Brain: local Ollama + Obsidian vault + live dashboard. Python stdlib only.
Usage: python3 server.py /path/to/vault   (env: OLLAMA, EMBED_MODEL, PORT)"""
import json, math, os, re, sqlite3, sys, threading, time, urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

OLLAMA = os.environ.get("OLLAMA", "http://127.0.0.1:11434")
EMBED = os.environ.get("EMBED_MODEL", "nomic-embed-text")
PORT = int(os.environ.get("PORT", 8787))
VAULT = Path(os.path.expanduser(sys.argv[1] if len(sys.argv) > 1 else os.environ.get("VAULT", "~/vault")))
HERE = Path(__file__).parent
db = sqlite3.connect(HERE / "brain.db", check_same_thread=False)
lock = threading.RLock()
db.executescript("""create table if not exists chunks(path text, i int, mtime real, text text, emb text, primary key(path,i));
create table if not exists runs(ts real, model text, q text, p_tok int, o_tok int, ctx int, tps real, ms int, notes int);""")


def ollama(path, body=None):
    req = urllib.request.Request(OLLAMA + path, json.dumps(body).encode() if body is not None else None,
                                 {"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=600)


def embed(texts):
    try:
        return json.load(ollama("/api/embed", {"model": EMBED, "input": texts}))["embeddings"]
    except Exception:
        return [None] * len(texts)  # no embed model: keyword search still works


def chunks(text):
    out, cur = [], ""
    for para in re.split(r"\n\s*\n", text):
        if cur and len(cur) + len(para) > 900:
            out.append(cur.strip()); cur = ""
        cur += para + "\n\n"
    if cur.strip():
        out.append(cur.strip())
    return out


def reindex():
    seen, updated = set(), 0
    with lock:
        for f in VAULT.rglob("*.md"):
            rel = f.relative_to(VAULT)
            if any(p.startswith(".") for p in rel.parts):
                continue  # skips .obsidian etc.
            rel, m = str(rel), f.stat().st_mtime
            seen.add(rel)
            old = db.execute("select max(mtime) from chunks where path=?", (rel,)).fetchone()[0]
            if old and old >= m:
                continue
            parts = chunks(f.read_text(errors="ignore"))
            embs = embed([rel + "\n" + p for p in parts]) if parts else []
            db.execute("delete from chunks where path=?", (rel,))
            for i, (p, e) in enumerate(zip(parts, embs)):
                db.execute("insert into chunks values(?,?,?,?,?)", (rel, i, m, p, json.dumps(e) if e else None))
            updated += 1
        for (p,) in db.execute("select distinct path from chunks").fetchall():
            if p not in seen:
                db.execute("delete from chunks where path=?", (p,))
        db.commit()
        n = db.execute("select count(distinct path), count(*), sum(emb is not null) from chunks").fetchone()
    return {"updated": updated, "notes": n[0], "chunks": n[1], "embedded": n[2] or 0}


tok = lambda s: re.findall(r"\w+", s.lower())


def retrieve(q, k=5):
    with lock:
        rows = db.execute("select path,i,text,emb from chunks").fetchall()
    if not rows:
        return []
    qt, docs = set(tok(q)), [tok(r[2]) for r in rows]
    N, avg = len(rows), (sum(map(len, docs)) / len(rows) or 1)
    sets = [set(d) for d in docs]
    df = {t: sum(t in s for s in sets) for t in qt}
    bm = [sum(math.log(1 + (N - df[t] + .5) / (df[t] + .5)) * d.count(t) * 2.2 /
              (d.count(t) + 1.2 * (.25 + .75 * len(d) / avg)) for t in qt if df[t]) for d in docs]
    mx, qe, res = max(bm) or 1, embed([q])[0], []
    for r, b in zip(rows, bm):
        s = b / mx
        if qe and r[3]:
            e = json.loads(r[3])
            s += sum(x * y for x, y in zip(qe, e)) / ((math.sqrt(sum(x * x for x in qe)) * math.sqrt(sum(y * y for y in e))) or 1)
        res.append((s, r[0], r[2]))
    res.sort(reverse=True)
    return [{"path": p, "text": t, "score": round(s, 3)} for s, p, t in res[:k] if s > 0.05]


def learn(q, a, hits):
    """Self-growing memory: every answer becomes a linked note in the vault, then gets indexed."""
    d = VAULT / "_brain"
    d.mkdir(exist_ok=True)
    slug = re.sub(r"\W+", "-", q.lower())[:40].strip("-") or "note"
    f = d / f"{time.strftime('%Y-%m-%d-%H%M')}-{slug}.md"
    links = "\n".join(f"- [[{Path(p).stem}]]" for p in dict.fromkeys(h["path"] for h in hits))
    f.write_text(f"---\ntags: [brain, auto]\n---\n# {q}\n\n{a}\n\n## Sources\n{links}\n")
    reindex()
    return str(f.relative_to(VAULT))


def chat(h, d):
    model, q, ctx = d["model"], d["q"], int(d.get("ctx", 8192))
    def send(ev, **kw):
        h.wfile.write(f"data: {json.dumps({'ev': ev, **kw})}\n\n".encode()); h.wfile.flush()
    t0 = time.time()
    hits = retrieve(q)
    send("retrieve", hits=[{"path": x["path"], "score": x["score"]} for x in hits], ms=int((time.time() - t0) * 1000))
    notes = "\n\n".join(f"[{x['path']}]\n{x['text']}" for x in hits) or "(no matching notes)"
    msgs = [{"role": "system", "content": "You are the user's second brain. Answer from their notes below and say so "
             "when the notes don't cover it. Cite note names in [[brackets]].\n\nNOTES:\n" + notes},
            {"role": "user", "content": q}]
    ans, st = "", {}
    with ollama("/api/chat", {"model": model, "messages": msgs, "stream": True, "options": {"num_ctx": ctx}}) as r:
        for line in r:
            j = json.loads(line)
            piece = j.get("message", {}).get("content", "")
            if piece:
                ans += piece; send("token", t=piece)
            if j.get("done"):
                st = j
    p, o = st.get("prompt_eval_count", 0), st.get("eval_count", 0)
    tps = o / (st["eval_duration"] / 1e9) if st.get("eval_duration") else 0
    ms = int((time.time() - t0) * 1000)
    saved = learn(q, ans, hits) if d.get("learn", True) and ans else None
    with lock:
        db.execute("insert into runs values(?,?,?,?,?,?,?,?,?)", (time.time(), model, q, p, o, ctx, tps, ms, len(hits)))
        db.commit()
    send("done", p=p, o=o, ctx=ctx, tps=round(tps, 1), ms=ms, saved=saved)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, body, ctype="application/json", code=200):
        b = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        if self.path == "/":
            self.reply((HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/api/models":
            try:
                self.reply([m["name"] for m in json.load(ollama("/api/tags"))["models"]])
            except Exception as e:
                self.reply({"error": str(e)}, code=502)
        elif self.path == "/api/stats":
            with lock:
                runs = db.execute("select ts,model,q,p_tok,o_tok,ctx,tps,ms,notes from runs order by ts desc limit 40").fetchall()
                c = db.execute("select count(distinct path), count(*) from chunks").fetchone()
            self.reply({"runs": runs[::-1], "notes": c[0], "chunks": c[1]})
        else:
            self.send_error(404)

    def do_POST(self):
        d = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if self.path == "/api/reindex":
            self.reply(reindex())
        elif self.path == "/api/chat":
            self.send_response(200); self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache"); self.end_headers()
            try:
                chat(self, d)
            except Exception as e:
                self.wfile.write(f"data: {json.dumps({'ev': 'error', 'msg': str(e)})}\n\n".encode())
        else:
            self.send_error(404)


if __name__ == "__main__":
    VAULT.mkdir(parents=True, exist_ok=True)
    print(f"vault  {VAULT}\nollama {OLLAMA}\nopen   http://127.0.0.1:{PORT}")
    threading.Thread(target=reindex, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
