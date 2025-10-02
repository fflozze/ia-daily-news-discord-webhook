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

# ---------- Discord limits ----------
DISCORD_MAX_FIELD   = 1024          # max par field.value
DISCORD_MAX_EMBEDS  = 10            # max embeds par message
DISCORD_MAX_EMBED   = 6000          # budget total par embed (title+desc+fields+footer)
DISCORD_MAX_DESC    = 4096
DISCORD_MAX_TITLE   = 256

def _text_len(s: str) -> int:
    return len(s or "")

def chunk_text(txt: str, size: int = DISCORD_MAX_FIELD):
    """Coupe proprement un texte (par lignes) pour respecter la limite Discord par field."""
    txt = (txt or "").strip()
    if not txt:
        return []
    lines, chunks, buf = txt.splitlines(), [], ""
    for line in lines:
        # +1 pour saut de ligne si nécessaire
        add_len = len(line) + (0 if not buf else 1)
        if len(buf) + add_len > size:
            if buf.strip():
                chunks.append(buf.rstrip())
            # si la ligne seule dépasse, on coupe brutalement
            if len(line) > size:
                for i in range(0, len(line), size):
                    part = line[i:i+size]
                    chunks.append(part)
                buf = ""
            else:
                buf = line
        else:
            buf = (buf + "\n" + line) if buf else line
    if buf.strip():
        chunks.append(buf.rstrip())
    return chunks

def _clean_embed(d: dict) -> dict:
    """Supprime les clés None/vides non autorisées par Discord et clamp les tailles de base."""
    out = {}
    for k, v in d.items():
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
                    vv.append({"name": name[:256], "value": value[:DISCORD_MAX_FIELD]})
            if vv:
                out[k] = vv
        else:
            out[k] = v
    return out

def _embed_size(e: dict) -> int:
    """Estime la taille d'un embed (Discord limite ~6000)."""
    size = 0
    size += _text_len(e.get("title"))
    size += _text_len(e.get("description"))
    for f in e.get("fields", []) or []:
        size += _text_len(f.get("name"))
        size += _text_len(f.get("value"))
    # footer.text compte aussi
    footer = e.get("footer", {})
    size += _text_len(footer.get("text"))
    # Petite marge structurelle
    return size

def _fit_fields_into_embeds(base_embed, fields, embeds):
    """
    Insère des fields (liste de dicts name/value) en respectant le budget 6000.
    Crée de nouveaux embeds si besoin (jusqu'à DISCORD_MAX_EMBEDS).
    """
    # commence par l'embed de base
    if "fields" not in base_embed:
        base_embed["fields"] = []
    # essaye d'ajouter chaque field en respectant le budget
    current = base_embed
    for fld in fields:
        if len(embeds) >= DISCORD_MAX_EMBEDS and current is None:
            break  # plus de place
        if current is None:
            # nouveau conteneur
            if len(embeds) >= DISCORD_MAX_EMBEDS:
                break
            current = {"color": COLOR, "fields": []}
            embeds.append(current)

        # tentative d'ajout
        current["fields"].append(fld)
        if _embed_size(_clean_embed(current)) > DISCORD_MAX_EMBED:
            # retirer et créer un nouvel embed
            current["fields"].pop()
            embeds.append({"color": COLOR, "fields": [fld]})
            current = embeds[-1]
            # si même seul il dépasse (improbable car value clampée), on tronque un peu plus
            if _embed_size(_clean_embed(current)) > DISCORD_MAX_EMBED:
                # tronque la value jusqu'à rentrer
                val = current["fields"][0]["value"]
                # coupe 100 caractères à la fois
                while val and _embed_size(_clean_embed(current)) > DISCORD_MAX_EMBED:
                    val = val[:-100]
                    current["fields"][0]["value"] = val
    return embeds

def post_discord_embeds(title: str, description: str, bullet_text: str, links_text: str):
    """
    Construit 1..N embeds valides, en respectant la limite 6000/ embed et 10 embeds par message.
    Stratégie :
      - titre + description courte dans le 1er embed,
      - fields "Synthèse" puis "Sources" (chunkés),
      - débordements -> embeds suivants.
    """
    title = (title or "").strip()[:DISCORD_MAX_TITLE]
    description = (description or "").strip()
    # pour limiter le risque de 6000, on clamp la description à 800 chars par défaut
    if len(description) > 800:
        description = description[:800] + "…"

    # Prépare les chunks de fields
    bullet_chunks = chunk_text(bullet_text, DISCORD_MAX_FIELD) if bullet_text else []
    link_chunks   = chunk_text(links_text,  DISCORD_MAX_FIELD) if links_text else []

    # Premier embed : titre + desc + 0..2 fields (un de chaque si possible)
    base_fields = []
    if bullet_chunks:
        base_fields.append({"name": "Synthèse", "value": bullet_chunks[0]})
        bullet_chunks = bullet_chunks[1:]
    if link_chunks:
        base_fields.append({"name": "Sources", "value": link_chunks[0]})
        link_chunks = link_chunks[1:]

    base_embed = _clean_embed({
        "title": title,
        "description": description if description else None,
        "color": COLOR,
        "footer": {"text": f"Veille IA • {date_str} • Fenêtre: {HOURS}h • {TZ}"},
        "fields": base_fields
    })

    # Assure-toi que le 1er embed respecte 6000 (au cas où le modèle ait donné un pavé énorme)
    while _embed_size(base_embed) > DISCORD_MAX_EMBED:
        # on réduit la description par paliers
        desc = base_embed.get("description") or ""
        if len(desc) > 200:
            base_embed["description"] = desc[:-200] + "…"
            base_embed = _clean_embed(base_embed)
            continue
        # sinon, on retire le dernier field si présent
        if base_embed.get("fields"):
            base_embed["fields"].pop()
            base_embed = _clean_embed(base_embed)
            continue
        # sinon on cesse (rare)
        break

    embeds = [base_embed]

    # Prépare les fields restants
    remaining_fields = []
    for ch in bullet_chunks:
        remaining_fields.append({"name": "Synthèse (suite)", "value": ch})
    for ch in link_chunks:
        remaining_fields.append({"name": "Sources (suite)", "value": ch})

    # Remplis les embeds en respectant 6000
    embeds = _fit_fields_into_embeds(embeds[0], remaining_fields, embeds)

    # Clamp final : au cas où on dépasse le nombre max d'embeds
    if len(embeds) > DISCORD_MAX_EMBEDS:
        embeds = embeds[:DISCORD_MAX_EMBEDS]
        # signale qu'il y a plus de contenu
        last = embeds[-1]
        extra_note = "\n_(Contenu abrégé : voir sources complètes sur les liens listés.)_"
        if last.get("fields"):
            last["fields"][-1]["value"] = (last["fields"][-1]["value"] + extra_note)[:DISCORD_MAX_FIELD]

    payload = {"embeds": [_clean_embed(e) for e in embeds]}
    r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=30)
    if not r.ok:
        raise RuntimeError(f"Discord 400 payload error: {r.status_code} {r.text}")

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

    # 1) Champ direct souvent présent
    if isinstance(data, dict) and data.get("output_text"):
        return data["output_text"].strip()

    # 2) Fallback : concaténer les blocs textuels
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

    # 3) Dernier recours
    return json.dumps(data, ensure_ascii=False)[:4000]

# ---------- Parsing du Markdown retourné ----------
SECTION_LIENS_RE = re.compile(r"^\s*#{0,3}\s*liens?\s*$", re.IGNORECASE | re.MULTILINE)

def split_summary_links(md: str):
    """
    Sépare (si possible) Résumé/Synthèse et Liens.
    Renvoie (title, bullets, links).
    """
    md = (md or "").strip()
    if not md:
        return f"Veille IA — {date_str} (dernières {HOURS} h)", "", ""

    # Titre Markdown en première ligne si présent
    title_match = re.search(r"^\s*#+\s*(.+)", md)
    title = title_match.group(1).strip() if title_match else f"Veille IA — {date_str} (dernières {HOURS} h)"

    # Découpe sur la section "Liens"
    parts = SECTION_LIENS_RE.split(md)
    if len(parts) >= 2:
        before = parts[0].strip()
        after  = parts[1].strip()
        bullets = before
        links   = after
    else:
        bullets = md
        links = ""

    # Retirer un titre en double dans bullets
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
        # Échec : poster l’erreur en embed
        try:
            post_discord_embeds(
                title=f"Veille IA — {date_str} (dernières {HOURS} h)",
                description=f"⚠️ Erreur durant la recherche/synthèse : {e}",
                bullet_text="",
                links_text=""
            )
        finally:
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

    # Description = premier paragraphe du résumé (clampée à ~800 dans post_discord_embeds)
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
