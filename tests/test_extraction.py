import os
from unittest.mock import MagicMock, patch
import pytest
from pydantic import ValidationError
import yt_dlp

from schemas import IngestRequest, ReelAnalysis, BroadCategory, TAXONOMY
from extractor import (
    extract_metadata_and_audio,
    temporary_extraction,
    ExtractionError,
    PostUnavailableError,
    RateLimitError,
)


def test_ingest_request_url_validation():
    # Valid URLs
    valid_urls = [
        "https://www.instagram.com/reel/C3bX123456/",
        "https://instagram.com/reel/C3bX123456",
        "https://www.instagram.com/p/DFxyz789/",
        "https://instagram.com/p/DFxyz789",
        "https://www.instagram.com/reels/C3bX123456/?utm_source=ig_web_copy_link",
        "http://instagram.com/reel/abcABC123_/",
    ]
    for url in valid_urls:
        req = IngestRequest(url=url)
        assert req.url.startswith("https://www.instagram.com/reel/")
        assert req.url.endswith("/")

    # Invalid URLs
    invalid_urls = [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://twitter.com/user/status/123456",
        "https://www.instagram.com/stories/username/123456/",
        "https://www.instagram.com/explore/",
        "not_a_url",
        "https://instagram.com/",
    ]
    for url in invalid_urls:
        with pytest.raises(ValidationError):
            IngestRequest(url=url)


def test_reel_analysis_taxonomy_validation():
    # Valid model
    analysis = ReelAnalysis(
        title="Building Microservices in Go",
        broad_category=BroadCategory.CODING,
        subcategory="Backend & Cloud",
        tags=["#Coding/Backend", "#Golang"],
        quick_takeaway="Go provides great concurrency primitives for microservices. Use channels carefully.",
        actionable_content=["Use worker pools", "Benchmark channel buffer size"],
        links_mentioned=["github.com/gin-gonic/gin"],
    )
    assert analysis.broad_category == BroadCategory.CODING
    assert analysis.subcategory == "Backend & Cloud"

    # Mismatched subcategory auto-adjusts to closest or valid fallback
    analysis_fallback = ReelAnalysis(
        title="Making Fresh Pasta Dough",
        broad_category=BroadCategory.FOOD_RECIPES,
        subcategory="Pasta",  # Matches "Pasta & Mains"
        tags=["#Food/Pasta"],
        quick_takeaway="Fresh pasta needs only 00 flour and eggs. Knead for 10 minutes.",
        actionable_content=["Knead dough", "Rest 30 min", "Roll thin"],
        links_mentioned=[],
    )
    assert analysis_fallback.subcategory == "Pasta & Mains"


@patch("yt_dlp.YoutubeDL")
def test_successful_extraction_with_native_audio(mock_ydl_class, tmp_path):
    mock_ydl_instance = MagicMock()
    mock_ydl_class.return_value.__enter__.return_value = mock_ydl_instance

    def mock_extract(url, download=True):
        if download:
            # Simulate yt-dlp downloading native m4a stream without transcoding
            audio_file = tmp_path / "audio.m4a"
            audio_file.write_bytes(b"dummy m4a audio content")
        return {
            "id": "test_reel",
            "title": "5 VS Code Tricks You Did Not Know",
            "description": "5 VS Code tricks every developer must know! #coding #vscode",
            "uploader": "dev_guru",
            "duration": 45.0,
        }

    mock_ydl_instance.extract_info.side_effect = mock_extract

    result = extract_metadata_and_audio(
        url="https://www.instagram.com/reel/C3bX123456/",
        output_dir=str(tmp_path),
        download_audio=True,
    )

    # Check ydl configuration
    called_opts = mock_ydl_class.call_args[0][0]
    assert called_opts["format"] == "ba/b"
    assert "postprocessors" not in called_opts
    assert called_opts["socket_timeout"] == 10
    assert called_opts["outtmpl"].endswith("audio.%(ext)s")

    assert result.source_url == "https://www.instagram.com/reel/C3bX123456/"
    assert "VS Code tricks" in result.caption
    assert result.uploader == "dev_guru"
    assert result.duration == 45.0
    assert result.audio_path is not None
    assert result.audio_mime_type == "audio/mp4"
    assert os.path.exists(result.audio_path)


@patch("yt_dlp.YoutubeDL")
def test_successful_extraction_with_aac_audio(mock_ydl_class, tmp_path):
    mock_ydl_instance = MagicMock()
    mock_ydl_class.return_value.__enter__.return_value = mock_ydl_instance

    def mock_extract(url, download=True):
        if download:
            audio_file = tmp_path / "audio.aac"
            audio_file.write_bytes(b"dummy aac audio content")
        return {
            "id": "aac_reel",
            "title": "AAC Audio Test",
            "description": "Native AAC stream",
        }

    mock_ydl_instance.extract_info.side_effect = mock_extract

    result = extract_metadata_and_audio(
        url="https://www.instagram.com/reel/C3bX123456/",
        output_dir=str(tmp_path),
        download_audio=True,
    )

    assert result.audio_mime_type == "audio/aac"
    assert result.audio_path is not None
    assert result.audio_path.endswith(".aac")


@patch("yt_dlp.YoutubeDL")
def test_extraction_handles_post_unavailable_error(mock_ydl_class, tmp_path):
    mock_ydl_instance = MagicMock()
    mock_ydl_class.return_value.__enter__.return_value = mock_ydl_instance
    mock_ydl_instance.extract_info.side_effect = yt_dlp.utils.DownloadError(
        "ERROR: [Instagram] This post is private or login required"
    )

    with pytest.raises(PostUnavailableError):
        extract_metadata_and_audio(
            url="https://www.instagram.com/reel/C3bX123456/",
            output_dir=str(tmp_path),
            download_audio=True,
        )


@patch("yt_dlp.YoutubeDL")
def test_extraction_handles_rate_limit_error(mock_ydl_class, tmp_path):
    mock_ydl_instance = MagicMock()
    mock_ydl_class.return_value.__enter__.return_value = mock_ydl_instance
    mock_ydl_instance.extract_info.side_effect = yt_dlp.utils.DownloadError(
        "ERROR: HTTP Error 429: Too Many Requests"
    )

    with pytest.raises(RateLimitError):
        extract_metadata_and_audio(
            url="https://www.instagram.com/reel/C3bX123456/",
            output_dir=str(tmp_path),
            download_audio=True,
        )


@patch("yt_dlp.YoutubeDL")
def test_temporary_extraction_cleans_up_files(mock_ydl_class):
    mock_ydl_instance = MagicMock()
    mock_ydl_class.return_value.__enter__.return_value = mock_ydl_instance

    temp_audio_created = []

    def mock_extract(url, download=True):
        # We need to know which directory it's downloading to
        opts = mock_ydl_class.call_args[0][0]
        outtmpl = opts["outtmpl"]
        out_dir = os.path.dirname(outtmpl)
        audio_file = os.path.join(out_dir, "audio.m4a")
        with open(audio_file, "wb") as f:
            f.write(b"sample audio bytes")
        temp_audio_created.append(audio_file)
        return {
            "id": "12345",
            "title": "Clean test",
            "description": "Caption text",
            "uploader": "clean_coder",
            "duration": 30.0,
        }

    mock_ydl_instance.extract_info.side_effect = mock_extract

    url = "https://www.instagram.com/reel/C3bX123456/"
    with temporary_extraction(url, download_audio=True) as extraction:
        assert extraction.audio_path is not None
        assert os.path.exists(extraction.audio_path)
        temp_dir = os.path.dirname(extraction.audio_path)
        assert os.path.isdir(temp_dir)

    # After exiting context manager, temp directory and all audio files must be deleted
    assert len(temp_audio_created) == 1
    assert not os.path.exists(temp_audio_created[0])
    assert not os.path.exists(temp_dir)


@patch("yt_dlp.YoutubeDL")
def test_temporary_extraction_cleans_up_on_exception(mock_ydl_class):
    mock_ydl_instance = MagicMock()
    mock_ydl_class.return_value.__enter__.return_value = mock_ydl_instance

    captured_path = []

    def mock_extract(url, download=True):
        opts = mock_ydl_class.call_args[0][0]
        outtmpl = opts["outtmpl"]
        out_dir = os.path.dirname(outtmpl)
        audio_file = os.path.join(out_dir, "audio.m4a")
        with open(audio_file, "wb") as f:
            f.write(b"sample audio")
        captured_path.append(out_dir)
        return {"id": "123", "description": "some caption"}

    mock_ydl_instance.extract_info.side_effect = mock_extract

    with pytest.raises(RuntimeError):
        with temporary_extraction("https://www.instagram.com/reel/C3bX123456/"):
            assert len(captured_path) == 1
            assert os.path.isdir(captured_path[0])
            raise RuntimeError("Simulated processing error inside context block")

    # The directory should be completely removed even after an unhandled exception
    assert not os.path.exists(captured_path[0])
