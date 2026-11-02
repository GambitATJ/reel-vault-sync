from unittest.mock import patch, AsyncMock, MagicMock
import pytest
import httpx
from main import app, run_reel_pipeline
from config import get_settings
from schemas import VaultIndex


@pytest.fixture(autouse=True)
def override_settings(monkeypatch):
    monkeypatch.setenv("API_AUTH_TOKEN", "test-secret-token")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_health_check():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "healthy"}


@pytest.mark.asyncio
async def test_unauthorized_access_missing_token():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/ingest",
            json={"url": "https://www.instagram.com/reel/C3bX123456/"},
        )
        assert response.status_code == 401
        assert "detail" in response.json()


@pytest.mark.asyncio
async def test_unauthorized_access_wrong_token():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/ingest",
            headers={"Authorization": "Bearer wrong-token"},
            json={"url": "https://www.instagram.com/reel/C3bX123456/"},
        )
        assert response.status_code == 401
        assert response.json()["detail"] == "Invalid authentication token"


@pytest.mark.asyncio
async def test_invalid_instagram_url():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/ingest",
            headers={"Authorization": "Bearer test-secret-token"},
            json={"url": "https://www.youtube.com/watch?v=123456"},
        )
        assert response.status_code == 422


@pytest.mark.asyncio
@patch("main.run_reel_pipeline", new_callable=AsyncMock)
async def test_valid_request_returns_202_and_invokes_background_worker(mock_pipeline):
    reel_url = "https://www.instagram.com/reel/C3bX123456/"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/ingest",
            headers={"Authorization": "Bearer test-secret-token"},
            json={"url": reel_url},
        )

        assert response.status_code == 202
        data = response.json()
        assert data["status"] == "accepted"
        assert data["url"] == "https://www.instagram.com/reel/C3bX123456/"

        # FastAPI BackgroundTasks are executed after the response is sent
        mock_pipeline.assert_called_once_with("https://www.instagram.com/reel/C3bX123456/")


@pytest.mark.asyncio
@patch("main.GitHubVaultSync.commit_note_and_update_index", new_callable=AsyncMock)
@patch("main.GitHubVaultSync.get_vault_index", new_callable=AsyncMock)
@patch("main.ReelAnalyzer.analyze")
@patch("main.temporary_extraction")
async def test_end_to_end_pipeline_success(
    mock_temp_extract, mock_analyze, mock_get_index, mock_commit
):
    from contextlib import contextmanager
    from schemas import ExtractionResult, ReelAnalysis, BroadCategory

    dummy_result = ExtractionResult(
        source_url="https://www.instagram.com/reel/C3bX123456/",
        caption="Building microservices in Go",
        uploader="dev_ops",
    )

    mock_get_index.return_value = VaultIndex(
        urls={},
        authors={},
        taxonomy={"Coding": ["Backend & Cloud"]},
    )

    @contextmanager
    def mock_context(url, download_audio=True):
        yield dummy_result

    mock_temp_extract.side_effect = mock_context
    mock_analyze.return_value = ReelAnalysis(
        title="Microservices in Go",
        author="@dev_ops",
        broad_category=BroadCategory.CODING,
        subcategory="Backend & Cloud",
        tags=["#Coding/Backend", "#Go"],
        quick_takeaway="Go is ideal for high-throughput distributed systems.",
        actionable_content=["Use worker pools", "Context cancellation"],
        links_mentioned=["github.com/gin-gonic/gin"],
    )
    mock_commit.return_value = ("Inbox/Reels/Coding/Backend & Cloud/Microservices in Go.md", {})

    # Run the background pipeline
    await run_reel_pipeline("https://www.instagram.com/reel/C3bX123456/?utm_source=share")

    mock_analyze.assert_called_once_with(
        dummy_result,
        taxonomy=mock_get_index.return_value.taxonomy,
        authors=mock_get_index.return_value.authors,
    )
    mock_commit.assert_called_once()
    commit_args = mock_commit.call_args[1]
    assert commit_args["analysis"].title == "Microservices in Go"
    assert "## Quick Takeaway" in commit_args["markdown_content"]


@pytest.mark.asyncio
@patch("main.GitHubVaultSync.get_vault_index", new_callable=AsyncMock)
@patch("main.temporary_extraction")
@patch("main.ReelAnalyzer.analyze")
async def test_pipeline_deduplication_skips_processing(
    mock_analyze, mock_temp_extract, mock_get_index
):
    # Normalized URL is already present in vault index
    clean_url = "https://www.instagram.com/reel/AlreadyArchived123/"
    mock_get_index.return_value = VaultIndex(
        urls={clean_url: "Inbox/Reels/Coding/System Design/OldNote.md"},
        authors={},
        taxonomy={},
    )

    # Trigger with extra query parameters
    raw_url = "https://www.instagram.com/reel/AlreadyArchived123/?srtk=abc12345"
    await run_reel_pipeline(raw_url)

    # Verify that neither extraction nor Gemini analyzer was invoked
    mock_temp_extract.assert_not_called()
    mock_analyze.assert_not_called()
