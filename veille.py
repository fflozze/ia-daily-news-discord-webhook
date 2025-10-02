import os, textwrap, json, time
from datetime import datetime, timedelta, timezone
import requests

OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
MODEL = os.getenv("MODEL", "gpt-4.1-mini")
HOURS = int(os.getenv("HOURS", "24"))
LOCALE = os.getenv("LOCALE", "fr-FR")
TZ = os.getenv("TIMEZONE", "Europe/Paris")

# Fenêtre temporelle (on donne les bornes à l'IA)
now_utc = datetime.now(timezone.utc)
since_utc = now_utc - timedelta(hours=HOURS)
date_str = now_utc.strftime("%Y-%m-%d")

def post_discord(md: str):
    # Discord ~2000 chars max → on split
    CHUNK = 1800
    for i in range(0, len(md), CHUNK):
        part = md[i:i+CHUNK]
        r = requests.post(DISCORD_WEBHOOK_URL, json={"content": part}, timeout=30)
        r.raise_for_status()

def call_openai_websearch(prompt: str) -> str:
    """
    Appelle l'OpenAI Responses API avec l'outil intégré 'web search'
    pour que le modèle aille chercher les news en ligne et rende un
    résumé + une liste de liens.
    """
    url = "https://api.openai.com/v1/responses"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}

    body = {
        "model": MODEL,
        "input": prompt,
        # Active l'outil de recherche web côté OpenAI
        "tools": [
            {"type": "web_search"}
        ],
        # Laisse le modèle utiliser le web search quand nécessaire
        "tool_choice": "auto",
    }

    r = requests.post(url, headers=headers, json=body, timeout=120)
    r.raise_for_status()
    data = r.json()

    # Extraction robuste du texte (selon la forme renvoyée)
    # 1) Champ pratique souvent présent
    if isinstance(data, dict) and data.get("output_text"):
        return data["output_text"]

    # 2) Fallback générique : on tente de concaténer les blocs textuels
    try:
        chunks = []
        for blk in data.get("output", []):
            for c in blk.get("content", []):
                t = c.get("text")
                if t:
                    chunks.append(t)
        if chunks:
            return "".join(chunks)
    except Exception:
        pass

    # 3) Dernier recours : tout renvoyer en JSON (debug)
    return json.dumps(data, ensure_ascii=False)[:4000]

def build_prompt():
    """
    Prompt clair : veille IA, dernières 24h (ou HOURS), FR, liens cliquables.
    On demande explicitement des sources avec URL, titres courts, et un format Markdown.
    """
    prompt = f"""
    Tu es un agent de veille technologique spécialisé **IA**.

    Tâche :
    - Utilise la **recherche web** pour identifier les **actualités IA** publiées entre
      **{since_utc.strftime("%Y-%m-%d %H:%M UTC")}** et **{now_utc.strftime("%Y-%m-%d %H:%M UTC")}**.
    - Concentre-toi sur : LLMs, modèles, produits IA, régulation IA, recherche, sécurité IA, MLOps.
    - Évite les doublons et les billets purement marketing/SEO à faible valeur.

    Rend en **français ({LOCALE})** un Markdown court et actionnable :
    1) Un titre : "Veille IA — {date_str} (dernières {HOURS} h)".
    2) Un **résumé global** (2–3 phrases).
    3) **5–10 puces** factuelles avec noms précis, versions, chiffres, régions, acteurs.
    4) Une section **Liens** (10–15 items max) au format :
       - Titre court — URL
       (Assure-toi que chaque lien pointe vers la source originale ou un média fiable.)
    5) Ajoute la **date/heure** (UTC ou locale) si disponible pour chaque lien entre parenthèses.
    6) N’invente rien ; **cite les sources** (URLs claires). Si c’est incertain, note-le.

    Contexte :
    - Langue de sortie : français ({LOCALE})
    - Fuseau pertinent : {TZ}
    - Fenêtre : {HOURS} heures
    - Tu peux faire **plusieurs recherches web** si nécessaire pour couvrir l’actualité.
    - Évite les paywalls si possible ; sinon, indique [paywall].

    Format :
    - Strictement en **Markdown**.
    - Pas de code block ``` inutile.
    """
    return textwrap.dedent(prompt).strip()

def main():
    header = f"**Veille IA — {date_str}** (collecte via recherche web • fenêtre: dernières {HOURS} h)"
    post_discord(header)

    prompt = build_prompt()
    try:
        md = call_openai_websearch(prompt)
    except Exception as e:
        md = f"⚠️ Erreur pendant la recherche/synthèse : {e}"

    # Sécurité minimale : si la sortie est très courte, on l'explicite
    if not md or len(md.strip()) < 50:
        md = "Aucune sortie exploitable retournée par l'IA (vérifie la clé API, le modèle ou réessaie plus tard)."

    post_discord(md)

if __name__ == "__main__":
    main()
