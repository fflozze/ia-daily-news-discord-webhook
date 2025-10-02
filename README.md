# AI News Daily (Discord Automation)

Automated daily **AI news monitoring** with **OpenAI web search** and **Discord embeds**.  
This project uses **GitHub Actions** to fetch the latest AI-related news every 24h, summarize it with OpenAI, and post a clean **digest on a Discord channel** via webhook.

---

## ✨ Features

- 🔎 Uses OpenAI **Responses API** with integrated **web search**  
- 📰 Summarizes **the last 24h of AI news** (LLMs, research, regulation, security, MLOps, etc.)  
- 📌 Posts to Discord using **rich embeds** (title, summary, bullet points, sources)  
- ⏰ Runs automatically every day at midnight (Paris time) with GitHub Actions  
- 🛠 No server, no hosting, no bot token → **just GitHub + Discord webhook**  

---

## 📂 Repository structure
.
├── .github/workflows/veille.yml # GitHub Actions workflow (automation)
├── veille.py # Main Python script
├── requirements.txt # Python dependencies
└── README.md # Project documentation

---

## ⚙️ Setup

### 1. Create a Discord Webhook
- Go to your Discord server → Channel settings → *Integrations* → *Webhooks*  
- Create a webhook and copy the **URL**

### 2. Add GitHub Secrets
In your repository → **Settings → Secrets → Actions** → *New repository secret* :

- `OPENAI_API_KEY` → your [OpenAI API key](https://platform.openai.com/api-keys)  
- `DISCORD_WEBHOOK_URL` → the webhook URL you just created  

### 3. Install dependencies (local test)
```bash
pip install -r requirements.txt
```

### 4. Run locally (optional)
```bash
python veille.py
```
### 5. Push to GitHub
- Commit & push → GitHub Actions will automatically run the workflow.  
- Check **Actions tab** → logs.  
- Result should appear in your Discord channel 🎉  

---

## 🕒 Schedule
By default, the workflow runs:
- Every day around **00:00 Paris time**  
- You can also trigger it manually in **Actions → Run workflow**  

---

## 🔧 Configuration (optional)
Environment variables (edit in workflow if needed):

- `MODEL` → default: `gpt-4.1-mini`  
- `HOURS` → time window for news (default: `24`)  
- `TIMEZONE` → e.g. `Europe/Paris`  
- `LOCALE` → output language (default: `fr-FR`)  
- `EMBED_COLOR` → Discord embed color (default: `5793266`)  

---

## 📸 Example Output
A daily post in Discord looks like this:

- **Embed title** → "AI Digest — YYYY-MM-DD (last 24h)"  
- **Summary** (2–3 sentences)  
- **Bullet points** (facts, names, numbers)  
- **Sources** (list of links with titles & dates)  

---
