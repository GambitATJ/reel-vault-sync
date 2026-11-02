import logging
import secrets
from typing import Optional
from fastapi import FastAPI, Depends, HTTPException, status, BackgroundTasks
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from config import get_settings, Settings
from schemas import IngestRequest
from extractor import temporary_extraction, ExtractionError
from analyzer import ReelAnalyzer, generate_obsidian_markdown
from vault_sync import GitHubVaultSync, VaultSyncError

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("reels_vault")

app = FastAPI(
    title="Instagram Reel to Obsidian Vault Service",
    description="Autonomous microservice extracting Instagram Reels, analyzing with Gemini, and syncing to GitHub Obsidian vault.",
    version="1.0.0",
)

security = HTTPBearer(auto_error=False)


async def verify_auth_token(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    settings: Settings = Depends(get_settings),
) -> str:
    """
    Validates Bearer token against API_AUTH_TOKEN using constant-time comparison.
    """
    if not credentials or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Bearer authentication header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not secrets.compare_digest(credentials.credentials, settings.API_AUTH_TOKEN):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return credentials.credentials


from schemas import IngestRequest, normalize_reel_url


async def run_reel_pipeline(url: str) -> None:
    """
    Asynchronous end-to-end background worker:
    1. Normalizes URL and checks for cached state deduplication (0ms if exists)
    2. Extracts audio & metadata in a temporary context (auto-cleaned on exit)
    3. Analyzes multimodal content using Gemini with dynamic taxonomy & series context
    4. Commits note and write-through updates .reel_index.json on GitHub
    """
    clean_url = normalize_reel_url(url)
    logger.info(f"Starting pipeline execution for reel: {clean_url}")
    try:
        vault_sync = GitHubVaultSync()
        vault_index = await vault_sync.get_vault_index()

        # O(1) early deduplication check
        if clean_url in vault_index.urls:
            existing_path = vault_index.urls[clean_url]
            logger.info(f"Reel already archived at {existing_path}. Skipping pipeline.")
            return

        with temporary_extraction(clean_url, download_audio=True) as extraction:
            logger.info(
                f"Extraction completed for {clean_url}. Caption length: {len(extraction.caption or '')}"
            )

            analyzer = ReelAnalyzer()
            analysis = analyzer.analyze(
                extraction,
                taxonomy=vault_index.taxonomy,
                authors=vault_index.authors,
            )
            logger.info(
                f"Gemini analysis completed for {clean_url}: [{analysis.broad_category}] {analysis.subcategory} - {analysis.title} (Author: {analysis.author}, Series: {analysis.is_part_of_series})"
            )

            markdown_content = generate_obsidian_markdown(analysis, source_url=clean_url)

            committed_path, _ = await vault_sync.commit_note_and_update_index(
                analysis=analysis,
                reel_url=clean_url,
                markdown_content=markdown_content,
            )
            logger.info(f"Successfully processed and committed reel to {committed_path}")

    except ExtractionError as e:
        logger.error(f"Extraction failed for {clean_url}: {e}")
    except VaultSyncError as e:
        logger.error(f"GitHub vault sync failed for {clean_url}: {e}")
    except Exception as e:
        logger.exception(f"Unhandled pipeline error processing {clean_url}: {e}")


@app.get("/health", status_code=status.HTTP_200_OK)
async def health_check():
    """Health check endpoint for Cloud Run and container orchestrators."""
    return {"status": "healthy"}


@app.post("/api/ingest", status_code=status.HTTP_202_ACCEPTED)
async def ingest_reel(
    request: IngestRequest,
    background_tasks: BackgroundTasks,
    _: str = Depends(verify_auth_token),
):
    """
    Accepts an Instagram reel URL, validates it, and triggers asynchronous processing.
    Returns immediate HTTP 202 Accepted.
    """
    background_tasks.add_task(run_reel_pipeline, request.url)
    return {
        "status": "accepted",
        "url": request.url,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=True)
