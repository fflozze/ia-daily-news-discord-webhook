import os, json, textwrap, re
from datetime import datetime, timedelta, timezone
import requests

# ---------- ENV ----------
OPENAI_API_KEY      = os.environ["OPENAI_API_KEY"]
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
MODEL   = os.getenv("MODEL", "gpt-4.1-mini")
HOURS   = int(os.getenv("HOURS", "24"))
LOCALE  = os.getenv("LOCALE", "fr-FR")
TZ      = os.getenv("TIMEZONE", "Europe/Paris")
COLOR   = int(os.getenv("EMBED_COLOR", "5793266"))  # 0x5865F2

now_utc   = datetime.now(timezone.utc)
since_utc = now_utc - timedelta(hours=HOURS)
date_str  = now_utc.strftime("%Y-%m-%d")

# ---------- Discord limits (serrées) ----------
DISCORD_MAX_EMBEDS   = 10     # embeds par message
DISCORD_MAX_EMBED    = 6000   # limite dure Discord
EMBED_TARGET_BUDGET  = 5500   # budget cible (marge)
DISCORD_MAX_TITLE    = 256
DISCORD_MAX_DESC     = 4096
FIELD_HARD_MAX       = 1024   # limite Discord par field.value
FIELD_SOFT_MAX       = 700    # notre cible pour la marge
DESC_SOFT_MAX        = 300    # description serrée

# ---------- Helpers taille ----------
def _text_len(s: str) -> int:
    return len(s or "")

def chunk_text(txt: str, size: int = FIELD_SOFT_MAX):
    """Coupe proprement par lignes, avec fallback brutal si une ligne dépasse."""
    txt = (txt or "").strip()
    if not txt:
        return []
    lines, chunks, buf = txt.splitlines(), [], ""
    for line in lines:
        add_len = len(line) + (0 if not buf else 1)
        if len(buf) + add_len > size:
            if buf.strip():
                chunks.append(buf.rstrip())
            if len(line) > size:
                # coupe brutalement les lignes immenses (URLs, etc.)
                for i in range(0, len(line), size):
                    chunks.append(line[i:i+size])
                buf = ""
            else:
                buf = line
        else:
            buf = (buf + "\n" + line) if buf else line
    if buf.strip():
        chunks.append(buf.rstrip())
    return chunks

def _clean_embed(e: dict) -> dict:
    """Supprime None/vides et clamp basique (title/desc/fields)."""
    out = {}
    for k, v in e.items():
        if v is None:
            continue
        if isinstance(v, str) and not v.strip():
            continue
        if k == "title":
            out[k] = v[:DISCORD_MAX_TITLE]
        elif k == "description":
            out[k] = v[:DISCORD_MAX_DESC]
        elif k == "fields":
            vv = []
            for f in (v or []):
                name = (f.get("name") or "").strip()
                value = (f.get("value") or "").strip()
                if name and value:
                    # clamp soft puis hard
                    if len(value) > FIELD_SOFT_MAX:
                        value = value[:FIELD_SOFT_MAX] + "…"
                    value = value[:FIELD_HARD_MAX]
                    vv.append({"name": name[:256], "value": value})
            if vv:
                out[k] = vv
        else:
            out[k] = v
    return out

def _embed_size(e: dict) -> int:
    """Estimation de la taille totale considérée par l'API."""
    size = 0
    size += _text_len(e.get("title"))
    size += _text_len(e.get("description"))
    for f in e.get("fields") or []:
        size += _text_len(f.get("name"))
        size += _text_len(f.get("value"))
    footer = e.get("footer") or {}
    size += _text_len(footer.get("text"))
    return size

def _shrink_to_fit(e: dict):
    """
    Rétrécit description/field.value jusqu’à passer sous EMBED_TARGET_BUDGET,
    sinon sous DISCORD_MAX_EMBED en dernier recours.
    """
    e = _clean_embed(e)

    # boucle de sécurité
    guard = 0
    while _embed_size(e) > EMBED_TARGET_BUDGET and guard < 100:
        guard += 1
        # 1) raccourcir description
        desc = e.get("description")
        if desc and len(desc) > 120:
            e["description"] = desc[:-80] + "…"
            e = _clean_embed(e)
            continue
        # 2) raccourcir le (seul) field si présent
        if e.get("fields"):
            f = e["fields"][0]
            val = f["value"]
            if len(val) > 120:
                f["value"] = val[:-80] + "…"
                e = _clean_embed(e)
                continue
        break

    # Dernier filet: assure < 6000
    guard = 0
    while _embed_size(e) > DISCORD_MAX_EMBED and guard < 100:
        guard += 1
        desc = e.get("description")
        if desc and len(desc) > 50:
            e["description"] = desc[:-50] + "…"
            e = _clean_embed(e)
            continue
        if e.get("fields"):
            f = e["fields"][0]
            val = f["value"]
            if len(val) > 50:
                f["value"] = val[:-50] + "…"
                e = _clean_embed(e)
                continue
        # si toujours trop gros (très improbable), on retire description
        if e.get("description"):
            e["description"] = ""
            e = _clean_embed(e)
            continue
        # et on retire le field
        if e.get("fields"):
            e["fields"].pop(0)
            e = _clean_embed(e)
            continue
        break

    return e

def _send_embeds_in_batches(embeds: list):
    """Envoie par lots de 10 embeds max, en rétrécissant une dernière fois avant envoi."""
    for i in range(0, len(embeds), DISCORD_MAX_EMBEDS):
        batch = embeds[i:i+DISCORD_MAX_EMBEDS]
        # shrink final safety pass sur chaque embed du lot
        safe_batch = [_shrink_to_fit(em) for em in batch]
        payload = {"embeds": [ _clean_embed(em) for em in safe_batch if em.get("title") or em.get("description") or em.get("fields") ]}
        r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=30)
        if not r.ok:
            raise RuntimeError(f"Discord 400 payload error: {r.status_code} {r.text}")

def post_discord_embeds(title: str, description: str, bullet_text: str, links_text: str):
    """
    Stratégie robuste :
      - Embed 1 : titre + description (serrée) + footer (pas de fields)
      - Ensuite : 1 field par embed pour 'Synthèse' (chunks) puis 'Sources' (chunks)
      - Découpage en plusieurs messages si >10 embeds
    """
    title = (title or "").strip()[:DISCORD_MAX_TITLE]
    description = (description or "").strip()
    if len(description) > DESC_SOFT_MAX:
        description = description[:DESC_SOFT_MAX] + "…"

    # 1er embed
    first_embed = {
        "title": title,
        "description": description if description else None,
        "color": COLOR,
        "footer": {"text": f"Veille IA • {date_str} • Window: {HOURS}h • {TZ}"},
    }
    first_embed = _shrink_to_fit(first_embed)

    embeds = [first_embed]

    # Chunks (un field => un embed)
    bullet_chunks = chunk_text(bullet_text, FIELD_SOFT_MAX) if bullet_text else []
    link_chunks   = chunk_text(links_text,  FIELD_SOFT_MAX) if links_text else []

    for idx, ch in enumerate(bullet_chunks):
        e = {"color": COLOR, "fields": [{"name": "Synthèse" if idx == 0 else "Synthèse (suite)", "value": ch}]}
        embeds.append(_shrink_to_fit(e))

    for idx, ch in enumerate(link_chunks):
        e = {"color": COLOR, "fields": [{"name": "Sources" if idx == 0 else "Sources (suite)", "value": ch}]}
        embeds.append(_shrink_to_fit(e))

    _send_embeds_in_batches(embeds)

# ---------- OpenAI (Responses API + web_search tool) ----------
def call_openai_websearch(prompt: str) -> str:
    """
    Appelle OpenAI Responses API avec l’outil 'web_search' activé.
    Retourne du Markdown (texte).
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

    if isinstance(data, dict) and data.get("output_text"):
        return data["output_text"].strip()

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

    return json.dumps(data, ensure_ascii=False)[:4000]

# ---------- Parsing du Markdown retourné ----------
SECTION_LIENS_RE = re.compile(r"^\s*#{0,3}\s*liens?\s*$", re.IGNORECASE | re.MULTILINE)

def split_summary_links(md: str):
    """Renvoie (title, bullets, links)."""
    md = (md or "").strip()
    if not md:
        return f"Veille IA — {date_str} (dernières {HOURS} h)", "", ""

    title_match = re.search(r"^\s*#+\s*(.+)", md)
    title = title_match.group(1).strip() if title_match else f"Veille IA — {date_str} (dernières {HOURS} h)"

    parts = SECTION_LIENS_RE.split(md)
    if len(parts) >= 2:
        before = parts[0].strip()
        after  = parts[1].strip()
        bullets = before
        links   = after
    else:
        bullets = md
        links = ""

    if title_match:
        start = title_match.end()
        if len(md) > start:
            bullets = md[start:].strip()

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
        # poste l’erreur en clair
        err = f"⚠️ Erreur durant la recherche/synthèse : {e}"
        post_discord_embeds(
            title=f"Veille IA — {date_str} (dernières {HOURS} h)",
            description=err,
            bullet_text="",
            links_text=""
        )
        raise

    if not md or len(md.strip()) < 30:
        post_discord_embeds(
            title=f"Veille IA — {date_str} (dernières {HOURS} h)",
            description="Aucune sortie exploitable retournée par l'IA (vérifie la clé API / le modèle).",
            bullet_text="",
            links_text=""
        )
        return

    title, bullets, links = split_summary_links(md)

    # Description : premier paragraphe (serré)
    first_para = re.split(r"\n\s*\n", bullets.strip(), maxsplit=1)[0] if bullets.strip() else ""
    description = first_para
    bullets_body = bullets[len(first_para):].strip() if bullets.strip() else ""

    post_discord_embeds(
        title=title,
        description=description,
        bullet_text=bullets_body,
        links_text=links
    )

if __name__ == "__main__":
    main()
