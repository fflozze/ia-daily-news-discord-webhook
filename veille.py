import os, json, textwrap, re
from datetime import datetime, timedelta, timezone
import requests

OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]

MODEL   = os.getenv("MODEL", "gpt-4.1-mini")
HOURS   = int(os.getenv("HOURS", "24"))
LOCALE  = os.getenv("LOCALE", "fr-FR")
TZ      = os.getenv("TIMEZONE", "Europe/Paris")
COLOR   = int(os.getenv("EMBED_COLOR", "5793266"))  # 0x5865F2 en décimal

now_utc   = datetime.now(timezone.utc)
since_utc = now_utc - timedelta(hours=HOURS)
date_str  = now_utc.strftime("%Y-%m-%d")

# ---------- Discord helpers (embeds) ----------
DISCORD_MAX_FIELD = 1024
DISCORD_MAX_EMBEDS = 10

def chunk_text(txt: str, size: int = DISCORD_MAX_FIELD):
    txt = txt.strip()
    if not txt:
        return []
    lines, chunks, buf = txt.splitlines(), [], ""
    for line in lines:
        if len(buf) + len(line) + 1 > size:
            chunks.append(buf.rstrip())
            buf = ""
        buf += line + "\n"
    if buf.strip():
        chunks.append(buf.rstrip())
    # si un mot/grand lien dépasse 1024, on coupe brutalement
    fixed = []
    for c in chunks:
        if len(c) <= size:
            fixed.append(c)
        else:
            for i in range(0, len(c), size):
                fixed.append(c[i:i+size])
    return fixed

def post_discord_embeds(title: str, description: str, bullet_text: str, links_text: str):
    """
    Construit 1..N embeds bien formés, en respectant les limites Discord.
    - title (<=256), description (<=4096)
    - fields value (<=1024), max 25 fields, max 10 embeds
    """
    embeds = []

    base_embed = {
        "title": title[:256],
        "description": description[:4096] if description else None,
        "color": COLOR,
        "footer": {"text": f"Veille IA • {date_str} • Fenêtre: {HOURS}h • {TZ}"},
    }

    fields = []

    # Champ "Synthèse" (bullets)
    if bullet_text:
        bullet_chunks = chunk_text(bullet_text, DISCORD_MAX_FIELD)
        if bullet_chunks:
            # première partie dans le 1er embed
            fields.append({"name": "Synthèse", "value": bullet_chunks[0]})
            # les suites en champs supplémentaires (ou dans embeds suivants si besoin)
            extra_bullet_chunks = bullet_chunks[1:]
        else:
            extra_bullet_chunks = []
    else:
        extra_bullet_chunks = []

    # Champ "Sources"
    extra_link_chunks = []
    if links_text:
        link_chunks = chunk_text(links_text, DISCORD_MAX_FIELD)
        if link_chunks:
            fields.append({"name": "Sources", "value": link_chunks[0]})
            extra_link_chunks = link_chunks[1:]

    # Ajoute le 1er embed
    base_embed["fields"] = fields
    embeds.append(base_embed)

    # Ajoute des embeds supplémentaires si besoin (bullets/links trop longs)
    for ch in extra_bullet_chunks:
        if len(embeds) >= DISCORD_MAX_EMBEDS: break
        embeds.append({
            "color": COLOR,
            "fields": [{"name": "Synthèse (suite)", "value": ch}]
        })

    for ch in extra_link_chunks:
        if len(embeds) >= DISCORD_MAX_EMBEDS: break
        embeds.append({
            "color": COLOR,
            "fields": [{"name": "Sources (suite)", "value": ch}]
        })

    payload = {"embeds": embeds}
    r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=30)
    r.raise_for_status()

# ---------- OpenAI (web search) ----------
def call_openai_websearch(prompt: str) -> str:
    """
    Appelle l'OpenAI Responses API avec l’outil de recherche web.
    Retourne du Markdown.
    """
    url = "https://api.openai.com/v1/responses"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    body = {
        "model": MODEL,
        "input": prompt,
        "tools": [{"type": "web_search"}],
        "tool_choice": "auto",
    }
    r = requests.post(url, headers=headers, json=body, timeout=180)
    r.raise_for_status()
    data = r.json()

    # 1) champ pratique
    if isinstance(data, dict) and data.get("output_text"):
        return data["output_text"].strip()

    # 2) fallback : concat blocs textuels
    try:
        chunks = []
        for blk in data.get("output", []):
            for c in blk.get("content", []):
                t = c.get("text")
                if t:
                    chunks.append(t)
        if chunks:
            return "".join(chunks).strip()
    except Exception:
        pass

    # 3) dernier recours
    return json.dumps(data, ensure_ascii=False)[:4000]

# ---------- Parsing léger du Markdown retourné ----------
SECTION_LIENS_RE = re.compile(r"^\s*#{0,3}\s*liens?\s*$", re.IGNORECASE | re.MULTILINE)

def split_summary_links(md: str):
    """
    On tente de séparer « résumé/puces » et « liens ».
    Si on ne trouve pas, on met tout dans 'bullets' et ‘links’ vide.
    """
    md = md.strip()
    if not md:
        return "", "", ""

    # titre (si fourni par le modèle)
    title_match = re.search(r"^\s*#+\s*(.+)", md)
    title = title_match.group(1).strip() if title_match else f"Veille IA — {date_str} (dernières {HOURS} h)"

    # coupe sur la section "Liens"
    parts = SECTION_LIENS_RE.split(md)
    if len(parts) >= 2:
        before = parts[0].strip()
        after  = parts[1].strip()
        bullets = before
        links   = after
    else:
        bullets = md
        links = ""

    # Nettoyage : retirer un éventuel titre en double des bullets
    if title_match:
        bullets = bullets[title_match.end():].strip()

    # Conserver seulement listes/paragraphes
    return title, bullets, links

# ---------- Prompt ----------
def build_prompt():
    return textwrap.dedent(f"""
    Tu es un agent de veille technologique spécialisé **IA**.

    Tâche :
    - Utilise la **recherche web** pour identifier les **actualités IA** publiées entre
      **{since_utc.strftime("%Y-%m-%d %H:%M UTC")}** et **{now_utc.strftime("%Y-%m-%d %H:%M UTC")}**.
    - Concentre-toi sur : LLMs, modèles, produits IA, régulations IA, recherche, sécurité IA, MLOps.
    - Évite les doublons et les billets purement marketing/SEO à faible valeur.

    Rendu en **français ({LOCALE})** et en **Markdown** :
    1) Un titre : "Veille IA — {date_str} (dernières {HOURS} h)".
    2) Un **résumé global** (2–3 phrases).
    3) **5–10 puces** factuelles (noms précis, versions, chiffres, régions, acteurs).
    4) Une section **Liens** (10–15 items max) au format :
       - Titre court — URL (et la **date/heure** si disponible entre parenthèses).
    5) Cite **uniquement des sources réelles avec URL visibles**. Si paywall, indique [paywall].
    6) Pas d'invention. Si incertain, indique-le.

    Contexte :
    - Langue de sortie : français ({LOCALE})
    - Fuseau de référence : {TZ}
    - Fenêtre : {HOURS} heures
    - Tu peux effectuer **plusieurs recherches**.

    Format strictement Markdown. Pas de blocs de code inutiles.
    """).strip()

# ---------- Main ----------
def main():
    prompt = build_prompt()
    try:
        md = call_openai_websearch(prompt)
    except Exception as e:
        # Échec : on poste une erreur en embed
        post_discord_embeds(
            title=f"Veille IA — {date_str} (dernières {HOURS} h)",
            description=f"⚠️ Erreur durant la recherche/synthèse : {e}",
            bullet_text="",
            links_text=""
        )
        return

    if not md or len(md.strip()) < 30:
        post_discord_embeds(
            title=f"Veille IA — {date_str} (dernières {HOURS} h)",
            description="Aucune sortie exploitable retournée par l'IA (vérifie la clé API/modèle).",
            bullet_text="",
            links_text=""
        )
        return

    # Découpage en (title, bullets, links)
    title, bullets, links = split_summary_links(md)

    # Description courte = premières 2–3 phrases du résumé si on arrive à l’isoler
    # sinon on laisse vide et on met tout en champs "Synthèse".
    desc_match = re.split(r"\n\s*\n", bullets.strip(), maxsplit=1)
    description = desc_match[0][:4000] if desc_match else ""
    bullets_body = desc_match[1].strip() if len(desc_match) > 1 else bullets.strip()

    # Discord embeds
    post_discord_embeds(
        title=title,
        description=description,
        bullet_text=bullets_body,
        links_text=links
    )

if __name__ == "__main__":
    main()
