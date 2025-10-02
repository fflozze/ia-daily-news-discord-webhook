import os, json, re, hashlib
from datetime import datetime, timedelta, timezone
import requests, feedparser
from dateutil import parser as dtparse

# --- ENV & constantes ---
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
TOPICS = json.loads(os.environ["TOPICS_JSON"])  # {"IA": ["feed1", "feed2", ...]}

NOW_UTC = datetime.now(timezone.utc)
SINCE = NOW_UTC - timedelta(hours=24)

# --- utilitaires ---
def normalize_url(u):
    return re.sub(r"#.*$", "", (u or "").strip())

def parse_time(entry):
    # Essaie plusieurs champs standard RSS/Atom
    for key in ("published", "updated", "created"):
        val = entry.get(key)
        if val:
            try:
                return dtparse.parse(val).astimezone(timezone.utc)
            except Exception:
                pass
    if entry.get("published_parsed"):
        return datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
    return None  # inconnu

def dedupe(items):
    seen, out = set(), []
    for it in items:
        k = hashlib.sha1(normalize_url(it["link"]).encode()).hexdigest()
        if k not in seen:
            seen.add(k)
            out.append(it)
    return out

def fetch_recent_from_feeds(topic, feeds):
    collected = []
    for url in feeds:
        try:
            d = feedparser.parse(url)
            for e in d.entries:
                ts = parse_time(e)
                if ts and ts >= SINCE:
                    collected.append({
                        "title": e.get("title","(sans titre)"),
                        "link": e.get("link",""),
                        "source": d.feed.get("title", url),
                        "published": ts.isoformat()
                    })
        except Exception as ex:
            print(f"[WARN] {topic} feed error {url}: {ex}")
    # Nettoyage
    items = [i for i in collected if i["link"]]
    return dedupe(items)

def summarize_with_openai(topic, items):
    """Résumé strictement cadré sur l'IA, en français."""
    if not items:
        return f"## {topic}\nAucune actualité détectée dans les dernières 24 h."

    # On transmet au modèle uniquement 25 liens max (suffisant pour un daily)
    lines = "\n".join(
        f"- {it['title']} ({it['source']}) — {it['link']}"
        for it in sorted(items, key=lambda x: x["published"], reverse=True)[:25]
    )

    prompt = f"""
Tu es un analyste qui fait une veille **exclusivement IA** en français.
Consigne: résumer uniquement des **nouvelles sur l'IA** (modèles, recherche, produits IA, régulations IA, sécurité IA, MLOps, LLMs, etc.). Si un lien n'est pas clairement lié à l'IA, ignore-le.

Fenêtre: dernières 24 h (UTC). Date actuelle: {NOW_UTC.date()}.
Sujet: {topic}

Articles (sélection):
{lines}

Rendu attendu (Markdown concis):
1) 2–3 phrases de synthèse globale.
2) 5–8 puces factuelles (noms, versions, chiffres, régions, acteurs).
3) Section "Liens" listant 8–15 liens utiles (titre court + URL).
4) Pas d'invention. Si info manquante: reste neutre.
"""

    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    body = {
        "model": "gpt-4.1-mini",
        "input": prompt,
    }
    try:
        r = requests.post("https://api.openai.com/v1/responses", headers=headers, json=body, timeout=60)
        r.raise_for_status()
        data = r.json()
        # L'API fournit souvent un champ pratique 'output_text'
        content = data.get("output_text")
        if not content:
            # fallback générique (robuste aux variations de structure)
            content = json.dumps(data, ensure_ascii=False)
        return f"## {topic}\n{content}"
    except Exception as e:
        return f"## {topic}\n[Erreur de résumé IA] {e}"

def post_to_discord_markdown(md):
    # Discord limite ~2000 caractères par message
    chunk = 1800  # marge de sécurité
    idx = 0
    while idx < len(md):
        part = md[idx: idx+chunk]
        resp = requests.post(DISCORD_WEBHOOK_URL, json={"content": part})
        try:
            resp.raise_for_status()
        except Exception as e:
            print("Discord error:", e, resp.text)
            raise
        idx += chunk

def main():
    header = f"**Veille IA — {NOW_UTC.strftime('%Y-%m-%d')}** (fenêtre: dernières 24 h)"
    post_to_discord_markdown(header)

    # On force à ne traiter que le sujet IA, même si TOPICS_JSON contient autre chose
    feeds = []
    if "IA" in TOPICS:
        feeds = TOPICS["IA"]
    else:
        # Si jamais l'ENV ne contient pas "IA", on concatène tous les flux mais on résume 'IA'
        for v in TOPICS.values():
            feeds.extend(v)

    items = fetch_recent_from_feeds("IA", feeds)
    summary = summarize_with_openai("IA", items)
    post_to_discord_markdown(summary)

if __name__ == "__main__":
    main()
