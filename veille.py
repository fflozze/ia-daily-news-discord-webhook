import os, json, time, hashlib, re
from datetime import datetime, timedelta, timezone
import requests
import feedparser
from dateutil import parser as dtparse

# --- Config ---
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
TOPICS = json.loads(os.environ["TOPICS_JSON"])
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
NOW_UTC = datetime.now(timezone.utc)
SINCE = NOW_UTC - timedelta(hours=24)

# --- Helpers ---
def normalize_url(u): return re.sub(r"#.*$", "", u.strip())
def item_time(entry):
    # try multiple fields
    for key in ("published", "updated", "created"):
        val = entry.get(key)
        if val:
            try:
                return dtparse.parse(val).astimezone(timezone.utc)
            except: pass
    if entry.get("published_parsed"):
        return datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
    return None

def dedupe(items):
    seen = set(); out = []
    for it in items:
        k = hashlib.sha1(normalize_url(it["link"]).encode()).hexdigest()
        if k not in seen:
            seen.add(k); out.append(it)
    return out

def fetch_topic(topic, feeds):
    collected = []
    for url in feeds:
        try:
            d = feedparser.parse(url)
            for e in d.entries:
                ts = item_time(e)
                if not ts or ts < SINCE: continue
                collected.append({
                    "title": e.get("title","(sans titre)"),
                    "link": e.get("link",""),
                    "source": d.feed.get("title", url),
                    "published": ts.isoformat()
                })
        except Exception as ex:
            print(f"[WARN] {topic} feed error {url}: {ex}")
    return dedupe(collected)

def openai_summarize(topic, items):
    if not items: return f"Rien de nouveau pour **{topic}** dans les dernières 24 h."
    bullets = "\n".join([f"- {it['title']} ({it['source']}) — {it['link']}" for it in items[:25]])
    prompt = f"""
Tu es un analyste qui fait une veille quotidienne en français.
Synthétise en 5-8 puces max les tendances clés, puis liste les liens (max 15).
Sujet: {topic}
Fenêtre: dernières 24 heures (UTC). Date actuelle: {NOW_UTC.date()}.

Articles:
{bullets}

Attendu:
- Un résumé court (2-3 phrases)
- 5-8 puces avec faits concrets
- Puis une section "Liens" avec 10-15 liens utiles.
- Pas d'invention: si c'est ambigu, reste générique.
"""
    # OpenAI Responses API
    # Docs: https://platform.openai.com/docs/api-reference/responses
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    body = {
        "model": "gpt-4.1-mini",
        "input": prompt,
    }
    r = requests.post("https://api.openai.com/v1/responses", headers=headers, json=body, timeout=60)
    r.raise_for_status()
    out = r.json()
    # Some SDKs expose output_text; via raw API, extract from content
    content = ""
    try:
        content = "".join([blk.get("text","") for blk in out["output"][0]["content"]])
    except Exception:
        # fallback to common field if present
        content = out.get("output_text") or str(out)
    return f"## {topic}\n{content}"

def post_to_discord(content):
    # Split if too long (Discord limit ~2000 chars for content)
    chunks = []
    while content:
        chunks.append(content[:1900])
        content = content[1900:]
    for i, ch in enumerate(chunks):
        data = {"content": ch}
        resp = requests.post(DISCORD_WEBHOOK_URL, json=data, timeout=30)
        try:
            resp.raise_for_status()
        except Exception as e:
            print("Discord error:", e, resp.text)
            raise

def main():
    date_str = NOW_UTC.strftime("%Y-%m-%d")
    header = f"**Veille quotidienne — {date_str}** (fenêtre: dernières 24 h)"
    post_to_discord(header)
    for topic, feeds in TOPICS.items():
        items = fetch_topic(topic, feeds)
        summary = openai_summarize(topic, items)
        post_to_discord(summary)

if __name__ == "__main__":
    main()
