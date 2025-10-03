import os, json, textwrap, re
from datetime import datetime, timedelta, timezone
import requests

# ---------- ENV ----------
OPENAI_API_KEY      = os.environ["OPENAI_API_KEY"]
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
MODEL        = os.getenv("MODEL", "gpt-4.1-mini")
HOURS        = int(os.getenv("HOURS", "24"))
LOCALE       = os.getenv("LOCALE", "fr-FR")
SOURCE_LANGS = os.getenv("SOURCE_LANGS", "fr,en")  # FR + EN
TZ           = os.getenv("TIMEZONE", "Europe/Paris")
COLOR        = int(os.getenv("EMBED_COLOR", "5793266"))  # bleu

now_utc   = datetime.now(timezone.utc)
since_utc = now_utc - timedelta(hours=HOURS)
date_str  = now_utc.strftime("%Y-%m-%d")

# ---------- Discord limits (extra safe) ----------
DISCORD_MAX_EMBEDS   = 10
DISCORD_MAX_EMBED    = 6000
EMBED_TARGET_BUDGET  = 3500
DISCORD_MAX_TITLE    = 256
DISCORD_MAX_DESC     = 4096
FIELD_HARD_MAX       = 1024
FIELD_SOFT_MAX       = 450
DESC_SOFT_MAX        = 180

# ---------- Helpers ----------
def _text_len(s: str) -> int:
    return len(s or "")

def _normalize_text(s: str) -> str:
    if not s:
        return ""
    s = s.replace("\r", "")
    s = re.sub(r"^\s*#+\s*", "", s, flags=re.MULTILINE)
    s = re.sub(r"[ \t]{2,}", " ", s)
    return s.strip()

def chunk_text(txt: str, size: int = FIELD_SOFT_MAX):
    txt = _normalize_text(txt)
    if not txt:
        return []
    lines, chunks, buf = txt.splitlines(), [], ""
    for line in lines:
        line = line.strip()
        add_len = len(line) + (0 if not buf else 1)
        if len(buf) + add_len > size:
            if buf:
                chunks.append(buf)
            if len(line) > size:
                for i in range(0, len(line), size):
                    chunks.append(line[i:i+size])
                buf = ""
            else:
                buf = line
        else:
            buf = (buf + "\n" + line) if buf else line
    if buf:
        chunks.append(buf)
    return [c.strip()[:size] for c in chunks if c.strip()]

def _clean_embed(e: dict) -> dict:
    out = {}
    for k, v in e.items():
        if v is None:
            continue
        if isinstance(v, str):
            v = v.strip()
            if not v:
                continue
        if k == "title":
            out[k] = v[:DISCORD_MAX_TITLE]
        elif k == "description":
            out[k] = v[:DISCORD_MAX_DESC]
        elif k == "fields":
            vv = []
            for f in (v or []):
                name = _normalize_text(f.get("name") or "")
                value = _normalize_text(f.get("value") or "")
                if name and value:
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
    size = 0
    size += _text_len(e.get("title"))
    size += _text_len(e.get("description"))
    for f in e.get("fields") or []:
        size += _text_len(f.get("name")) + _text_len(f.get("value"))
    footer = e.get("footer") or {}
    size += _text_len(footer.get("text"))
    return size

def _shrink_to_fit(e: dict, target=EMBED_TARGET_BUDGET):
    e = _clean_embed(e)
    if e.get("description") and len(e["description"]) > DESC_SOFT_MAX:
        e["description"] = e["description"][:DESC_SOFT_MAX] + "…"
        e = _clean_embed(e)

    guard = 0
    while _embed_size(e) > target and guard < 100:
        guard += 1
        desc = e.get("description")
        if desc and len(desc) > 90:
            e["description"] = desc[:-60] + "…"
            e = _clean_embed(e)
            continue
        if e.get("fields"):
            f = e["fields"][0]
            val = f["value"]
            if len(val) > 120:
                f["value"] = val[:-80] + "…"
                e = _clean_embed(e)
                continue
        break

    guard = 0
    while _embed_size(e) > DISCORD_MAX_EMBED and guard < 100:
        guard += 1
        desc = e.get("description")
        if desc and len(desc) > 30:
            e["description"] = desc[:-30] + "…"
            e = _clean_embed(e)
            continue
        if e.get("fields"):
            f = e["fields"][0]
            val = f["value"]
            if len(val) > 50:
                f["value"] = val[:-50] + "…"
                e = _clean_embed(e)
                continue
        if e.get("description"):
            e["description"] = ""
            e = _clean_embed(e)
            continue
        if e.get("fields"):
            e["fields"].pop(0)
            e = _clean_embed(e)
            continue
        break
    return e

def _retry_shrink_and_send(payload, max_retries=3):
    for attempt in range(max_retries + 1):
        r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=30)
        if r.ok:
            return
        if "Embed size exceeds maximum size of 6000" not in r.text:
            raise RuntimeError(f"Discord payload error: {r.status_code} {r.text}")

        next_embeds = []
        for em in payload.get("embeds", []):
            if em.get("description"):
                limits = [120, 80, 50]
                lim = limits[min(attempt, len(limits)-1)]
                em["description"] = _normalize_text(em["description"])[:lim] + "…"
            if em.get("fields"):
                val = em["fields"][0]["value"]
                limits = [350, 250, 200]
                lim = limits[min(attempt, len(limits)-1)]
                em["fields"][0]["value"] = _normalize_text(val)[:lim] + "…"
            em = _shrink_to_fit(em, target=3000)
            next_embeds.append(_clean_embed(em))
        payload = {"embeds": next_embeds}
    raise RuntimeError("Discord 400 after retries: embeds still exceed size after aggressive shrinking.")

def _send_embeds_in_batches(embeds: list):
    for i in range(0, len(embeds), DISCORD_MAX_EMBEDS):
        batch = embeds[i:i+DISCORD_MAX_EMBEDS]
        safe_batch = []
        for j, em in enumerate(batch):
            if j > 0 and "footer" in em:
                em.pop("footer", None)
            em = _shrink_to_fit(em, target=EMBED_TARGET_BUDGET)
            if _embed_size(em) > EMBED_TARGET_BUDGET:
                em = _shrink_to_fit(em, target=3000)
            safe_batch.append(_clean_embed(em))
        payload = {"embeds": [em for em in safe_batch if em.get("title") or em.get("description") or em.get("fields")]}
        _retry_shrink_and_send(payload, max_retries=3)

def post_discord_embeds(title: str, description: str, bullet_text: str, links_text: str):
    title = (title or "").strip()[:DISCORD_MAX_TITLE]
    description = _normalize_text(description)
    if len(description) > DESC_SOFT_MAX:
        description = description[:DESC_SOFT_MAX] + "…"

    first_embed = {
        "title": title,
        "description": description if description else None,
        "color": COLOR,
        "footer": {"text": f"Veille IA • {date_str} • Window: {HOURS}h • {TZ}"},
    }
    first_embed = _shrink_to_fit(first_embed, target=EMBED_TARGET_BUDGET)

    embeds = [first_embed]

    bullet_chunks = chunk_text(bullet_text, FIELD_SOFT_MAX) if bullet_text else []
    link_chunks   = chunk_text(links_text,  FIELD_SOFT_MAX) if links_text else []

    for idx, ch in enumerate(bullet_chunks):
        e = {"color": COLOR, "fields": [{"name": "Synthèse" if idx == 0 else "Synthèse (suite)", "value": ch}]}
        embeds.append(_shrink_to_fit(e, target=EMBED_TARGET_BUDGET))

    for idx, ch in enumerate(link_chunks):
        e = {"color": COLOR, "fields": [{"name": "Sources" if idx == 0 else "Sources (suite)", "value": ch}]}
        embeds.append(_shrink_to_fit(e, target=EMBED_TARGET_BUDGET))

    _send_embeds_in_batches(embeds)

# ---------- OpenAI (Responses API + web_search tool) ----------
def call_openai_websearch(prompt: str) -> str:
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

# ---------- Parsing ----------
SECTION_LINKS_RE = re.compile(r"^\s*#{0,3}\s*links?\s*$", re.IGNORECASE | re.MULTILINE)
SECTION_LIENS_RE = re.compile(r"^\s*#{0,3}\s*liens?\s*$", re.IGNORECASE | re.MULTILINE)

def split_summary_links(md: str, fallback_title: str):
    md = (md or "").strip()
    if not md:
        return fallback_title, "", ""

    title_match = re.search(r"^\s*#+\s*(.+)", md)
    title = title_match.group(1).strip() if title_match else fallback_title

    parts = SECTION_LINKS_RE.split(md)
    if len(parts) < 2:
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

# ---------- Prompt (AI-only strict) ----------
def build_prompt():
    """
    IA NEWS (strict AI-only):
      IN-SCOPE (AI only): LLMs, modèles (open/closed), entraînement/inférence, benchmarks,
      produits & features IA, agents, multimodal, recherche académique (arXiv/ACL/NeurIPS/ICLR/ICML),
      sécurité IA (prompt injection/model theft), MLOps/outils IA, régulations/politiques publiques IA.
      OUT-OF-SCOPE (exclure): crypto/blockchain, général cloud/devops, data privacy non-IA,
      gadgets/smartphones/IoT, jeux vidéo, hardware non-IA (sauf GPU/accélérateurs **explicitement**
      liés à l’entraînement/inférence de modèles), sécurité non-IA, marché boursier non-IA, VR/AR sans IA.
      Règle d’éligibilité: l’IA doit être **le sujet principal**, pas une mention cosmétique.
      En cas de doute → **EXCLURE**.
    """
    langs_note = SOURCE_LANGS.replace(",", ", ")
    return textwrap.dedent(f"""
    Tu es un analyste de **veille IA**. Ne retiens **que** des nouvelles où **l’IA est le sujet principal**.

    Fenêtre: de **{since_utc.strftime("%Y-%m-%d %H:%M UTC")}** à **{now_utc.strftime("%Y-%m-%d %H:%M UTC")}**.
    Sources FR+EN ({langs_note}) — privilégie **sources primaires** (blogs officiels d’éditeurs IA, arXiv, régulateurs),
    et médias reconnus. **Déduplique** les articles parlant du même événement.

    INCLUS UNIQUEMENT si le contenu traite clairement de: LLMs/modèles, agents, multimodal, produits IA,
    recherche IA, sécurité IA, MLOps/outillage IA, régulation IA. 
    EXCLURE: crypto/blockchain, général cloud/dev, data privacy non-IA, gadgets/IoT/VR/AR sans IA,
    bourse non-IA, hardware non-IA (sauf GPU/TPU liés à l'entraînement/inférence), sécurité non-IA.

    Rend en **français ({LOCALE})** et en **Markdown** :
    1) Titre: "Veille IA — {date_str} (dernières {HOURS} h)".
    2) **Résumé** (2–3 phrases).
    3) **5–10 puces** factuelles (noms précis, versions/modèles, chiffres, dates, acteurs).
    4) Section **Liens / Links** (10–15 max), chaque item:
       - **[FR]** ou **[EN]** — Titre court — URL (avec **date/heure** si dispo).
    5) Si **<3 actus IA valides**, dis-le explicitement ("Aucune actualité IA marquante détectée sur la période.").
    6) Aucune invention ; indique [paywall] si besoin.

    Format strictement Markdown. Pas de blocs de code inutiles.
    """).strip()

# ---------- Main ----------
def main():
    prompt = build_prompt()
    try:
        md = call_openai_websearch(prompt)
    except Exception as e:
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
            description="Aucune sortie exploitable retournée par l'IA (clé API/modèle ?).",
            bullet_text="",
            links_text=""
        )
        return

    fallback_title = f"Veille IA — {date_str} (dernières {HOURS} h)"
    title, bullets, links = split_summary_links(md, fallback_title)

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
