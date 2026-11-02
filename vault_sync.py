import base64
import json
import logging
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
import httpx

from config import get_settings
from schemas import VaultIndex, ReelAnalysis, BroadCategory, TAXONOMY, normalize_reel_url

logger = logging.getLogger(__name__)

# In-memory module-level cache for 0ms lookup
_VAULT_INDEX_CACHE: Optional[VaultIndex] = None
INDEX_FILE_PATH = ".reel_index.json"


def clear_vault_index_cache() -> None:
    """Clears the in-memory vault index cache (useful for tests)."""
    global _VAULT_INDEX_CACHE
    _VAULT_INDEX_CACHE = None


def get_cached_vault_index() -> Optional[VaultIndex]:
    """Returns the current in-memory cache directly without network calls."""
    return _VAULT_INDEX_CACHE


def sanitize_filename(title: str, max_length: int = 80) -> str:
    """
    Sanitizes a title to be safe for filenames across OS and GitHub paths.
    Removes invalid characters and restricts length.
    """
    cleaned = re.sub(r'[\\/*?:"<>|#^]', "", title)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        cleaned = "Untitled_Note"
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length].rstrip()
    return cleaned


class VaultSyncError(Exception):
    """Raised when GitHub vault synchronization fails."""
    pass


class GitHubVaultSync:
    def __init__(
        self,
        token: Optional[str] = None,
        repo: Optional[str] = None,
        branch: Optional[str] = None,
        root_dir: Optional[str] = None,
        base_url: str = "https://api.github.com",
    ):
        settings = get_settings()
        self.token = token if token is not None else settings.GITHUB_PERSONAL_ACCESS_TOKEN
        self.repo = repo if repo is not None else settings.GITHUB_VAULT_REPO
        self.branch = branch if branch is not None else settings.GITHUB_VAULT_BRANCH
        self.root_dir = (root_dir if root_dir is not None else settings.VAULT_ROOT_DIR).strip("/")
        self.base_url = base_url.rstrip("/")

        if not self.token:
            logger.warning("GITHUB_PERSONAL_ACCESS_TOKEN is not set.")

    def _get_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "Reels-Obsidian-Vault-Sync",
        }

    def build_vault_path(
        self,
        broad_category: str,
        subcategory: str,
        title: str,
        suffix: Optional[str] = None,
    ) -> str:
        clean_title = sanitize_filename(title)
        filename = f"{clean_title}{suffix if suffix else ''}.md"
        return f"{self.root_dir}/{broad_category}/{subcategory}/{filename}"

    async def file_exists(self, client: httpx.AsyncClient, path: str) -> bool:
        """Checks if a file already exists at path on the configured branch."""
        url = f"{self.base_url}/repos/{self.repo}/contents/{path}"
        response = await client.get(url, headers=self._get_headers(), params={"ref": self.branch})
        if response.status_code == 200:
            return True
        elif response.status_code == 404:
            return False
        else:
            logger.warning(
                f"Unexpected status {response.status_code} checking existence for {path}: {response.text}"
            )
            return False

    async def get_file_content_and_sha(
        self, client: httpx.AsyncClient, path: str
    ) -> Optional[Tuple[str, str]]:
        """Fetches existing file content (UTF-8) and its git blob SHA."""
        url = f"{self.base_url}/repos/{self.repo}/contents/{path}"
        response = await client.get(url, headers=self._get_headers(), params={"ref": self.branch})
        if response.status_code == 200:
            data = response.json()
            raw_b64 = data.get("content", "")
            sha = data.get("sha", "")
            decoded = base64.b64decode(raw_b64).decode("utf-8")
            return decoded, sha
        return None

    async def resolve_unique_path(
        self,
        client: httpx.AsyncClient,
        broad_category: str,
        subcategory: str,
        title: str,
    ) -> str:
        """Determines target path. If collision occurs, appends a timestamp suffix."""
        initial_path = self.build_vault_path(broad_category, subcategory, title)
        if not await self.file_exists(client, initial_path):
            return initial_path

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        candidate_path = self.build_vault_path(
            broad_category, subcategory, title, suffix=f"_{timestamp}"
        )
        counter = 1
        while await self.file_exists(client, candidate_path):
            candidate_path = self.build_vault_path(
                broad_category, subcategory, title, suffix=f"_{timestamp}_{counter}"
            )
            counter += 1

        return candidate_path

    async def get_vault_index(self) -> VaultIndex:
        """
        Retrieves the VaultIndex.
        1. If in-memory cache is warm, returns immediately (0ms).
        2. If cold:
           - Tries fetching .reel_index.json from repo root.
           - If 404, runs initial tree scan via Git Trees API, constructs index, and saves it.
        """
        global _VAULT_INDEX_CACHE
        if _VAULT_INDEX_CACHE is not None:
            return _VAULT_INDEX_CACHE

        if not self.token:
            logger.warning("No token configured; returning default empty index.")
            _VAULT_INDEX_CACHE = VaultIndex(
                taxonomy={cat.value: list(subcats) for cat, subcats in TAXONOMY.items()}
            )
            return _VAULT_INDEX_CACHE

        async with httpx.AsyncClient(timeout=30.0) as client:
            index_url = f"{self.base_url}/repos/{self.repo}/contents/{INDEX_FILE_PATH}"
            response = await client.get(
                index_url, headers=self._get_headers(), params={"ref": self.branch}
            )

            if response.status_code == 200:
                data = response.json()
                raw_b64 = data.get("content", "")
                blob_sha = data.get("sha")
                try:
                    decoded = base64.b64decode(raw_b64).decode("utf-8")
                    parsed = json.loads(decoded)
                    parsed["last_synced_sha"] = blob_sha
                    index_obj = VaultIndex.model_validate(parsed)
                    # Merge default taxonomy if missing any broad category
                    for cat, subcats in TAXONOMY.items():
                        if cat.value not in index_obj.taxonomy:
                            index_obj.taxonomy[cat.value] = list(subcats)
                    _VAULT_INDEX_CACHE = index_obj
                    logger.info("Loaded .reel_index.json from GitHub repo into cache.")
                    return _VAULT_INDEX_CACHE
                except Exception as parse_err:
                    logger.error(f"Failed to parse .reel_index.json: {parse_err}")

            # If 404 or unparseable, fall back to initial git tree scan
            logger.info(".reel_index.json not found on repo. Initiating tree scan bootstrap...")
            initial_index = await self._bootstrap_index_from_git_tree(client)
            _VAULT_INDEX_CACHE = initial_index
            return _VAULT_INDEX_CACHE

    async def _bootstrap_index_from_git_tree(self, client: httpx.AsyncClient) -> VaultIndex:
        """Inspects existing repository tree to seed initial VaultIndex and writes .reel_index.json."""
        tree_url = f"{self.base_url}/repos/{self.repo}/git/trees/{self.branch}"
        response = await client.get(
            tree_url, headers=self._get_headers(), params={"recursive": "1"}
        )

        initial_taxonomy: Dict[str, List[str]] = {
            cat.value: list(subcats) for cat, subcats in TAXONOMY.items()
        }
        initial_urls: Dict[str, str] = {}
        initial_authors: Dict[str, List[str]] = {}

        if response.status_code == 200:
            tree_data = response.json().get("tree", [])
            for item in tree_data:
                path = item.get("path", "")
                if path.startswith(self.root_dir) and path.endswith(".md"):
                    parts = path[len(self.root_dir):].strip("/").split("/")
                    if len(parts) >= 3:
                        category = parts[0]
                        subcategory = parts[1]
                        if category in initial_taxonomy:
                            if subcategory not in initial_taxonomy[category]:
                                initial_taxonomy[category].append(subcategory)
                        else:
                            initial_taxonomy.setdefault(category, []).append(subcategory)

        index_model = VaultIndex(
            urls=initial_urls,
            authors=initial_authors,
            taxonomy=initial_taxonomy,
        )

        # Write initial .reel_index.json to repository
        json_bytes = index_model.model_dump_json(indent=2).encode("utf-8")
        b64_content = base64.b64encode(json_bytes).decode("utf-8")
        put_url = f"{self.base_url}/repos/{self.repo}/contents/{INDEX_FILE_PATH}"
        put_payload = {
            "message": "chore(vault): initialize .reel_index.json",
            "content": b64_content,
            "branch": self.branch,
        }
        put_res = await client.put(put_url, headers=self._get_headers(), json=put_payload)
        if put_res.status_code in (200, 201):
            blob_sha = put_res.json().get("content", {}).get("sha")
            index_model.last_synced_sha = blob_sha
            logger.info("Successfully created initial .reel_index.json in GitHub repo.")

        return index_model

    async def _write_through_index_update(
        self,
        client: httpx.AsyncClient,
        target_path: str,
        reel_url: str,
        author: Optional[str],
        category: str,
        subcategory: str,
    ) -> None:
        """Updates in-memory cache and writes through to .reel_index.json on GitHub."""
        global _VAULT_INDEX_CACHE
        if _VAULT_INDEX_CACHE is None:
            _VAULT_INDEX_CACHE = await self.get_vault_index()

        norm_url = normalize_reel_url(reel_url)
        _VAULT_INDEX_CACHE.urls[norm_url] = target_path

        if author:
            clean_handle = f"@{author.lstrip('@')}"
            if target_path not in _VAULT_INDEX_CACHE.authors.setdefault(clean_handle, []):
                _VAULT_INDEX_CACHE.authors[clean_handle].append(target_path)

        if category and subcategory:
            if subcategory not in _VAULT_INDEX_CACHE.taxonomy.setdefault(category, []):
                _VAULT_INDEX_CACHE.taxonomy[category].append(subcategory)

        # Commit updated .reel_index.json
        index_bytes = _VAULT_INDEX_CACHE.model_dump_json(indent=2).encode("utf-8")
        b64_content = base64.b64encode(index_bytes).decode("utf-8")
        put_url = f"{self.base_url}/repos/{self.repo}/contents/{INDEX_FILE_PATH}"
        payload = {
            "message": f"chore(vault): update .reel_index.json [{category}/{subcategory}]",
            "content": b64_content,
            "branch": self.branch,
        }
        if _VAULT_INDEX_CACHE.last_synced_sha:
            payload["sha"] = _VAULT_INDEX_CACHE.last_synced_sha

        put_res = await client.put(put_url, headers=self._get_headers(), json=payload)
        if put_res.status_code in (200, 201):
            new_sha = put_res.json().get("content", {}).get("sha")
            _VAULT_INDEX_CACHE.last_synced_sha = new_sha
            logger.info("Successfully committed updated .reel_index.json to GitHub.")
        else:
            logger.warning(
                f"Failed to update .reel_index.json (status {put_res.status_code}): {put_res.text}"
            )

    async def commit_note_and_update_index(
        self,
        analysis: ReelAnalysis,
        reel_url: str,
        markdown_content: str,
    ) -> Tuple[str, dict]:
        """
        Smart sync handler:
        1. If part of a series and author already has an ongoing series note, appends entry.
        2. Otherwise, creates a standalone note.
        3. Updates in-memory VaultIndex and write-through updates .reel_index.json on GitHub.
        """
        from analyzer import format_series_entry

        if not self.token:
            raise VaultSyncError("GITHUB_PERSONAL_ACCESS_TOKEN is missing or empty.")
        if not self.repo or "/" not in self.repo:
            raise VaultSyncError(f"Invalid GITHUB_VAULT_REPO: {self.repo}")

        clean_url = normalize_reel_url(reel_url)
        vault_index = await self.get_vault_index()

        cat_val = (
            analysis.broad_category.value
            if isinstance(analysis.broad_category, BroadCategory)
            else str(analysis.broad_category)
        )
        subcat_val = analysis.subcategory

        async with httpx.AsyncClient(timeout=30.0) as client:
            target_path = None
            put_payload = {}

            # Check if this should append to an existing author series note
            if analysis.is_part_of_series and analysis.author:
                author_handle = f"@{analysis.author.lstrip('@')}"
                existing_notes = vault_index.authors.get(author_handle, [])
                if existing_notes:
                    # Pick existing series note
                    chosen_path = existing_notes[0]
                    file_info = await self.get_file_content_and_sha(client, chosen_path)
                    if file_info:
                        existing_content, file_sha = file_info
                        series_entry = format_series_entry(analysis, source_url=clean_url)
                        updated_content = existing_content.rstrip() + "\n" + series_entry
                        b64_updated = base64.b64encode(updated_content.encode("utf-8")).decode("utf-8")
                        target_path = chosen_path
                        put_payload = {
                            "message": f"feat(vault): append entry to {author_handle} series - {analysis.title}",
                            "content": b64_updated,
                            "branch": self.branch,
                            "sha": file_sha,
                        }

            # If not appending, resolve a unique standalone note path
            if not target_path:
                target_path = await self.resolve_unique_path(
                    client, cat_val, subcat_val, analysis.title
                )
                b64_content = base64.b64encode(markdown_content.encode("utf-8")).decode("utf-8")
                put_payload = {
                    "message": f"feat(vault): add {cat_val}/{subcat_val} reel note - {analysis.title}",
                    "content": b64_content,
                    "branch": self.branch,
                }

            # Put note content to GitHub
            put_url = f"{self.base_url}/repos/{self.repo}/contents/{target_path}"
            response = await client.put(put_url, headers=self._get_headers(), json=put_payload)
            if response.status_code not in (200, 201):
                err_msg = (
                    f"GitHub API error (status {response.status_code}) committing to {target_path}: "
                    f"{response.text}"
                )
                logger.error(err_msg)
                raise VaultSyncError(err_msg)

            data = response.json()
            logger.info(f"Successfully committed note to GitHub vault: {target_path}")

            # Write-through update to index
            await self._write_through_index_update(
                client,
                target_path=target_path,
                reel_url=clean_url,
                author=analysis.author,
                category=cat_val,
                subcategory=subcat_val,
            )

            return target_path, data

    async def commit_note(
        self,
        broad_category: str,
        subcategory: str,
        title: str,
        markdown_content: str,
    ) -> Tuple[str, dict]:
        """
        Backwards-compatible standalone note committer.
        """
        if not self.token:
            raise VaultSyncError("GITHUB_PERSONAL_ACCESS_TOKEN is missing or empty.")
        if not self.repo or "/" not in self.repo:
            raise VaultSyncError(f"Invalid GITHUB_VAULT_REPO: {self.repo}")

        async with httpx.AsyncClient(timeout=30.0) as client:
            target_path = await self.resolve_unique_path(
                client, broad_category, subcategory, title
            )

            b64_content = base64.b64encode(markdown_content.encode("utf-8")).decode("utf-8")
            commit_payload = {
                "message": f"feat(vault): add {broad_category}/{subcategory} reel note - {title}",
                "content": b64_content,
                "branch": self.branch,
            }

            put_url = f"{self.base_url}/repos/{self.repo}/contents/{target_path}"
            response = await client.put(
                put_url,
                headers=self._get_headers(),
                json=commit_payload,
            )

            if response.status_code not in (200, 201):
                err_msg = (
                    f"GitHub API error (status {response.status_code}) committing to {target_path}: "
                    f"{response.text}"
                )
                logger.error(err_msg)
                raise VaultSyncError(err_msg)

            data = response.json()
            logger.info(f"Successfully committed note to GitHub vault: {target_path}")
            return target_path, data
