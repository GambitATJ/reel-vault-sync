import os
import shutil
import tempfile
import logging
from contextlib import contextmanager
from typing import Generator, Optional
import yt_dlp

from schemas import ExtractionResult

logger = logging.getLogger(__name__)


class ExtractionError(Exception):
    """Base exception for all extraction failures."""
    pass


class PostUnavailableError(ExtractionError):
    """Raised when a post is private, deleted, or otherwise inaccessible."""
    pass


class RateLimitError(ExtractionError):
    """Raised when request is throttled or blocked by Instagram."""
    pass


def _classify_yt_dlp_error(error_msg: str) -> ExtractionError:
    lower_msg = error_msg.lower()
    if any(term in lower_msg for term in ["private", "login required", "not found", "deleted", "unavailable", "empty page"]):
        return PostUnavailableError(f"Post is unavailable or private: {error_msg}")
    if any(term in lower_msg for term in ["rate limit", "429", "too many requests", "challenge_required"]):
        return RateLimitError(f"Rate limited by Instagram: {error_msg}")
    return ExtractionError(f"Extraction failed: {error_msg}")


AUDIO_MIME_TYPES = {
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".mp3": "audio/mp3",
    ".webm": "audio/webm",
    ".opus": "audio/opus",
    ".ogg": "audio/ogg",
    ".wav": "audio/wav",
}


def extract_metadata_and_audio(
    url: str,
    output_dir: str,
    download_audio: bool = True
) -> ExtractionResult:
    """
    Extracts metadata (caption, uploader, title) and optionally downloads
    the native compressed audio track from an Instagram Reel without transcoding.
    """
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "extract_flat": False,
        "socket_timeout": 10,
    }

    if download_audio:
        ydl_opts.update({
            "format": "ba/b",
            "outtmpl": os.path.join(output_dir, "audio.%(ext)s"),
        })

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=download_audio)
            if info is None:
                raise PostUnavailableError("No data returned for this Instagram URL.")

            caption = (
                info.get("description")
                or info.get("title")
                or ""
            )
            uploader = (
                info.get("uploader")
                or info.get("channel")
                or info.get("creator")
            )
            title = info.get("title")
            duration = info.get("duration")

            audio_path: Optional[str] = None
            audio_mime_type: Optional[str] = None
            if download_audio:
                # Look for downloaded audio files in the output directory
                for fname in os.listdir(output_dir):
                    fpath = os.path.join(output_dir, fname)
                    if os.path.isfile(fpath):
                        ext = os.path.splitext(fname)[1].lower()
                        if ext in AUDIO_MIME_TYPES:
                            audio_path = fpath
                            audio_mime_type = AUDIO_MIME_TYPES[ext]
                            break

            return ExtractionResult(
                source_url=url,
                caption=caption,
                uploader=uploader,
                title=title,
                audio_path=audio_path,
                audio_mime_type=audio_mime_type,
                duration=duration,
            )

    except yt_dlp.utils.DownloadError as e:
        logger.error(f"yt-dlp DownloadError extracting {url}: {e}")
        raise _classify_yt_dlp_error(str(e)) from e
    except yt_dlp.utils.ExtractorError as e:
        logger.error(f"yt-dlp ExtractorError extracting {url}: {e}")
        raise _classify_yt_dlp_error(str(e)) from e
    except Exception as e:
        if isinstance(e, ExtractionError):
            raise
        logger.error(f"Unexpected error extracting {url}: {e}")
        raise ExtractionError(f"Unexpected extraction error: {e}") from e


@contextmanager
def temporary_extraction(
    url: str,
    download_audio: bool = True
) -> Generator[ExtractionResult, None, None]:
    """
    Context manager that extracts metadata and audio to a temporary folder,
    yields the ExtractionResult, and guarantees complete cleanup of temp files upon exit.
    """
    temp_dir = tempfile.mkdtemp(prefix="reel_extract_")
    try:
        result = extract_metadata_and_audio(url, temp_dir, download_audio=download_audio)
        yield result
    finally:
        try:
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception as cleanup_err:
            logger.warning(f"Failed to cleanly delete temp dir {temp_dir}: {cleanup_err}")
