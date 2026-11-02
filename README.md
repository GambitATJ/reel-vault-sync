# Instagram Reel to Obsidian Vault Microservice

An autonomous, production-ready microservice that accepts an Instagram Reel URL, extracts audio and metadata with `yt-dlp`, analyzes and classifies the content using Google Gemini 1.5 Flash structured outputs, and commits formatted Markdown notes directly into a private GitHub repository acting as an Obsidian vault.

---

## Architecture & Data Flow

```text
[ iOS Shortcut / Webhook ]
            │
            ▼ (POST /api/ingest with Bearer Token)
     [ FastAPI Server ] ──▶ Returns HTTP 202 Accepted immediately
            │
            ▼ (BackgroundTasks)
   [ yt-dlp Audio/Metadata ] ──▶ Temp .m4a download & auto-cleanup
            │
            ▼ (Multimodal Audio/Text)
  [ Gemini 1.5 Flash API ] ──▶ Structured ReelAnalysis Pydantic Model
            │
            ▼ (Markdown Generation)
 [ YAML Frontmatter + Notes ]
            │
            ▼ (GitHub REST API v3)
 [ Private Obsidian Vault Repo ] ──▶ <VAULT_ROOT_DIR>/<Category>/<Subcategory>/<Title>.md
```

---

## Target Note Taxonomy

| Broad Category | Allowed Subcategories |
| :--- | :--- |
| **`Coding`** | `System Design`, `AI Tools & Vibecoding`, `Backend & Cloud`, `Frontend & UI`, `Algorithms` |
| **`Food Recipes`** | `Pasta & Mains`, `Desserts & Baking`, `Quick Bites`, `Sauces & Prep` |
| **`Cool Tools & Websites`** | `Developer Productivity`, `Web Apps`, `AI Utilities`, `Design Resources` |
| **`Learning & Concepts`** | `Computer Science`, `Productivity`, `Finance`, `Science & Tech` |

---

## Output Markdown Structure

```yaml
---
source: "https://www.instagram.com/reel/..."
category: "Coding"
subcategory: "Backend & Cloud"
tags: ["#Coding/Backend", "#FastAPI", "#Docker"]
created_at: "2026-10-31T07:15:00+00:00"
status: "inbox"
---

# Deploying Microservices with Cloud Run

## Quick Takeaway (2-3 sentences)
Deploying containerized microservices to Google Cloud Run delivers automated horizontal scaling and cost-efficiency. Combining FastAPI with multi-stage Docker builds ensures minimal cold starts.

## Key Details / Steps / Recipe (Bulleted action items, code snippets, or ingredients)
- Multi-stage Docker build targeting python:3.11-slim
- Include system dependencies like ffmpeg
- Expose container port 8080 and run as non-root user

## Referenced Links & Tools (Extracted tool names, repositories, or URLs)
- https://cloud.google.com/run
- https://fastapi.tiangolo.com
```

---

## Environment Variables Configuration

Copy `.env.template` to `.env` and fill in your secrets:

```bash
cp .env.template .env
```

| Variable | Description | Example |
| :--- | :--- | :--- |
| `GEMINI_API_KEY` | Google AI Studio API key | `AIzaSy...` |
| `GITHUB_PERSONAL_ACCESS_TOKEN` | Fine-grained GitHub PAT (`contents:write` on vault repo) | `github_pat_11...` |
| `GITHUB_VAULT_REPO` | Target GitHub repository (`owner/repo`) | `yourname/my-obsidian-vault` |
| `GITHUB_VAULT_BRANCH` | Default repository branch | `main` |
| `VAULT_ROOT_DIR` | Base directory inside repository | `Inbox/Reels` |
| `API_AUTH_TOKEN` | Shared secret token for Bearer authentication | `super-secret-random-token` |

---

## Local Development & Run

### 1. Prerequisites
- Python 3.11+
- `ffmpeg` installed on host (required by `yt-dlp` for local audio processing)

### 2. Setup Virtual Environment
```bash
python -m venv .venv
# Linux/macOS:
source .venv/bin/activate
# Windows:
.venv\Scripts\activate

pip install -r requirements.txt
```

### 3. Run the Microservice
```bash
uvicorn main:app --host 0.0.0.0 --port 8080 --reload
```

### 4. Run Test Suite
```bash
pytest -v
```

---

## Single-Line Cloud Run Deployment

Deploy container directly to Google Cloud Run using `gcloud run deploy`:

```bash
gcloud run deploy reels-vault-sync \
  --source . \
  --platform managed \
  --region us-central1 \
  --allow-unauthenticated \
  --port 8080 \
  --memory 1Gi \
  --cpu 1 \
  --set-env-vars "GEMINI_API_KEY=YOUR_GEMINI_KEY,GITHUB_PERSONAL_ACCESS_TOKEN=YOUR_GH_PAT,GITHUB_VAULT_REPO=username/vault-repo,GITHUB_VAULT_BRANCH=main,VAULT_ROOT_DIR=Inbox/Reels,API_AUTH_TOKEN=YOUR_SHARED_SECRET"
```

*(Tip: For production secrets, use Google Secret Manager with `--set-secrets` instead of plain text environment variables).*

---

## iOS Shortcut Setup

You can save any Instagram Reel directly to your Obsidian vault from the iOS Share Sheet:

1. Open the **Shortcuts** app on iOS and tap **+** (New Shortcut).
2. Set Shortcut Details:
   - Enable **Show in Share Sheet**.
   - Set accepted types to **URLs**.
3. Add Action: **Get Contents of URL**:
   - **URL**: `https://<YOUR-CLOUD-RUN-URL>/api/ingest`
   - **Method**: `POST`
   - **Headers**:
     - Key: `Authorization`
     - Value: `Bearer <YOUR_API_AUTH_TOKEN>`
     - Key: `Content-Type`
     - Value: `application/json`
   - **Request Body**: `JSON`
     - Add field `url` (Type: Text) -> Select **Shortcut Input**
4. Add Action (Optional notification): **Show Notification**:
   - Title: `Reel Ingested`
   - Message: `Reel sent to Obsidian Vault processing queue!`
5. Done! When browsing Instagram, tap **Share** on any Reel -> choose your shortcut name.
