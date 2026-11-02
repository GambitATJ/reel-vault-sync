import base64
import json
import pytest
import respx
import httpx

from schemas import VaultIndex, ReelAnalysis, BroadCategory, normalize_reel_url
from vault_sync import (
    GitHubVaultSync,
    sanitize_filename,
    VaultSyncError,
    clear_vault_index_cache,
    get_cached_vault_index,
    INDEX_FILE_PATH,
)


@pytest.fixture(autouse=True)
def reset_vault_cache():
    clear_vault_index_cache()
    yield
    clear_vault_index_cache()


def test_sanitize_filename():
    raw_title = 'How to Build an "Autonomous" Agent? [Step-by-Step] / System: Design!'
    clean = sanitize_filename(raw_title)
    assert "/" not in clean
    assert ":" not in clean
    assert "?" not in clean
    assert '"' not in clean
    assert clean == "How to Build an Autonomous Agent [Step-by-Step] System Design!"
    assert sanitize_filename("") == "Untitled_Note"


def test_normalize_reel_url():
    # Various Instagram URL styles and tracking params
    test_cases = [
        ("https://www.instagram.com/reel/DfFXY0wRpXC/?vrfl=aHBmYXA2Zmg0Y3Nz", "https://www.instagram.com/reel/DfFXY0wRpXC/"),
        ("https://instagram.com/reel/DfFXY0wRpXC", "https://www.instagram.com/reel/DfFXY0wRpXC/"),
        ("https://www.instagram.com/p/DdshHpFpqdi/?xtok=MWxub2RzMnRrbDFzaA==", "https://www.instagram.com/reel/DdshHpFpqdi/"),
        ("http://instagram.com/reels/C3bX123456/", "https://www.instagram.com/reel/C3bX123456/"),
        ("https://www.instagram.com/reel/DfH83beRoH0?srtk=NGl5MXE5cWs5aWQ1#heading", "https://www.instagram.com/reel/DfH83beRoH0/"),
    ]
    for raw_url, expected in test_cases:
        assert normalize_reel_url(raw_url) == expected

    with pytest.raises(ValueError, match="Invalid Instagram URL"):
        normalize_reel_url("https://www.youtube.com/watch?v=123456")


@pytest.mark.asyncio
@respx.mock
async def test_get_vault_index_cold_start_and_warm_cache():
    sync = GitHubVaultSync(
        token="test_gh_token",
        repo="user123/my-vault",
        branch="main",
    )

    index_data = {
        "urls": {"https://www.instagram.com/reel/123/": "Inbox/Reels/Coding/Go.md"},
        "authors": {"@dev_guru": ["Inbox/Reels/Coding/Go.md"]},
        "taxonomy": {"Coding": ["Backend & Cloud", "System Design"]},
    }
    b64_index = base64.b64encode(json.dumps(index_data).encode("utf-8")).decode("utf-8")

    # Mock fetching .reel_index.json (Cold Start)
    get_index_route = respx.get(
        f"https://api.github.com/repos/user123/my-vault/contents/{INDEX_FILE_PATH}"
    ).mock(
        return_value=httpx.Response(
            200,
            json={"content": b64_index, "sha": "index_sha_123"},
        )
    )

    # 1. Cold start call
    index_1 = await sync.get_vault_index()
    assert get_index_route.call_count == 1
    assert "https://www.instagram.com/reel/123/" in index_1.urls
    assert index_1.last_synced_sha == "index_sha_123"

    # 2. Warm cache call (0ms, no network request)
    index_2 = await sync.get_vault_index()
    assert get_index_route.call_count == 1  # No additional network call
    assert index_2 is index_1


@pytest.mark.asyncio
@respx.mock
async def test_get_vault_index_fallback_to_git_trees():
    sync = GitHubVaultSync(
        token="test_gh_token",
        repo="user123/my-vault",
        branch="main",
        root_dir="Inbox/Reels",
    )

    # .reel_index.json does not exist -> 404
    respx.get(
        f"https://api.github.com/repos/user123/my-vault/contents/{INDEX_FILE_PATH}"
    ).mock(return_value=httpx.Response(404))

    # Mock git trees response
    tree_route = respx.get(
        "https://api.github.com/repos/user123/my-vault/git/trees/main"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "tree": [
                    {"path": "Inbox/Reels/Coding/System Design/Microservices.md", "type": "blob"},
                    {"path": "Inbox/Reels/Food Recipes/Pasta & Mains/Carbonara.md", "type": "blob"},
                    {"path": "README.md", "type": "blob"},
                ]
            },
        )
    )

    # Mock PUT to save initial .reel_index.json
    put_index_route = respx.put(
        f"https://api.github.com/repos/user123/my-vault/contents/{INDEX_FILE_PATH}"
    ).mock(
        return_value=httpx.Response(
            201,
            json={"content": {"sha": "new_tree_index_sha"}},
        )
    )

    index = await sync.get_vault_index()
    assert tree_route.called
    assert put_index_route.called
    assert "System Design" in index.taxonomy["Coding"]
    assert "Pasta & Mains" in index.taxonomy["Food Recipes"]
    assert index.last_synced_sha == "new_tree_index_sha"


@pytest.mark.asyncio
@respx.mock
async def test_commit_note_and_update_index_standalone():
    sync = GitHubVaultSync(
        token="test_gh_token",
        repo="user123/my-vault",
        branch="main",
        root_dir="Inbox/Reels",
    )

    # Warm index cache with empty index
    empty_index = {
        "urls": {},
        "authors": {},
        "taxonomy": {"Coding": ["Backend & Cloud"]},
    }
    b64_index = base64.b64encode(json.dumps(empty_index).encode("utf-8")).decode("utf-8")
    respx.get(
        f"https://api.github.com/repos/user123/my-vault/contents/{INDEX_FILE_PATH}"
    ).mock(return_value=httpx.Response(200, json={"content": b64_index, "sha": "sha_idx_1"}))

    # Note creation routes
    expected_note_path = "Inbox/Reels/Coding/Backend & Cloud/Docker Optimization.md"
    respx.get(
        url__regex=r"https://api\.github\.com/repos/user123/my-vault/contents/Inbox/Reels/Coding/.*Docker%20Optimization\.md"
    ).mock(return_value=httpx.Response(404))

    put_note_route = respx.put(
        url__regex=r"https://api\.github\.com/repos/user123/my-vault/contents/Inbox/Reels/Coding/.*Docker%20Optimization\.md"
    ).mock(
        return_value=httpx.Response(
            201,
            json={"content": {"name": "Docker Optimization.md", "path": expected_note_path}},
        )
    )

    # Index update route
    put_index_route = respx.put(
        f"https://api.github.com/repos/user123/my-vault/contents/{INDEX_FILE_PATH}"
    ).mock(
        return_value=httpx.Response(200, json={"content": {"sha": "sha_idx_2"}})
    )

    analysis = ReelAnalysis(
        title="Docker Optimization",
        author="@cloud_pro",
        is_part_of_series=False,
        broad_category=BroadCategory.CODING,
        subcategory="Backend & Cloud",
        tags=["#Coding/Backend", "#Docker"],
        quick_takeaway="Use multi-stage builds to shrink image size.",
        actionable_content=["Use alpine base", "Cache layers"],
    )

    path, _ = await sync.commit_note_and_update_index(
        analysis=analysis,
        reel_url="https://www.instagram.com/reel/Docker123/?utm=test",
        markdown_content="# Docker Optimization",
    )

    assert path == expected_note_path
    assert put_note_route.called
    assert put_index_route.called

    cached = get_cached_vault_index()
    assert "https://www.instagram.com/reel/Docker123/" in cached.urls
    assert "@cloud_pro" in cached.authors
    assert expected_note_path in cached.authors["@cloud_pro"]


@pytest.mark.asyncio
@respx.mock
async def test_commit_note_series_append():
    sync = GitHubVaultSync(
        token="test_gh_token",
        repo="user123/my-vault",
        branch="main",
        root_dir="Inbox/Reels",
    )

    series_note_path = "Inbox/Reels/Coding/System Design/SaaS_Dev_Series.md"
    existing_content = "# SaaS Dev Series\n\nInitial notes..."
    b64_existing = base64.b64encode(existing_content.encode("utf-8")).decode("utf-8")

    # Warm index with known author and existing series note
    index_data = {
        "urls": {},
        "authors": {"@alex_dev": [series_note_path]},
        "taxonomy": {"Coding": ["System Design"]},
    }
    b64_index = base64.b64encode(json.dumps(index_data).encode("utf-8")).decode("utf-8")
    respx.get(
        f"https://api.github.com/repos/user123/my-vault/contents/{INDEX_FILE_PATH}"
    ).mock(return_value=httpx.Response(200, json={"content": b64_index, "sha": "idx_sha_1"}))

    # Mock getting existing series note
    respx.get(
        f"https://api.github.com/repos/user123/my-vault/contents/{series_note_path}"
    ).mock(
        return_value=httpx.Response(
            200,
            json={"content": b64_existing, "sha": "note_sha_123"},
        )
    )

    # Mock updating series note
    put_series_route = respx.put(
        f"https://api.github.com/repos/user123/my-vault/contents/{series_note_path}"
    ).mock(
        return_value=httpx.Response(200, json={"content": {"path": series_note_path}})
    )

    # Mock updating index
    respx.put(
        f"https://api.github.com/repos/user123/my-vault/contents/{INDEX_FILE_PATH}"
    ).mock(return_value=httpx.Response(200, json={"content": {"sha": "idx_sha_2"}}))

    analysis = ReelAnalysis(
        title="Day 5: Adding Stripe Billing",
        author="@alex_dev",
        is_part_of_series=True,
        series_title="SaaS Dev Series",
        broad_category=BroadCategory.CODING,
        subcategory="System Design",
        tags=["#Coding/SystemDesign"],
        quick_takeaway="Stripe webhooks need signature verification and idempotency keys.",
        actionable_content=["Verify webhook signature", "Record webhook event ID"],
    )

    target_path, _ = await sync.commit_note_and_update_index(
        analysis=analysis,
        reel_url="https://www.instagram.com/reel/Day5Stripe/",
        markdown_content="standalone markdown content",
    )

    assert target_path == series_note_path
    assert put_series_route.called
    put_req = put_series_route.calls.last.request
    body = json.loads(put_req.content)
    assert body["sha"] == "note_sha_123"
    decoded_updated = base64.b64decode(body["content"]).decode("utf-8")
    assert "Day 5: Adding Stripe Billing" in decoded_updated
    assert "Initial notes..." in decoded_updated


@pytest.mark.asyncio
async def test_missing_token_raises_vault_sync_error():
    sync = GitHubVaultSync(token="", repo="owner/repo")
    with pytest.raises(VaultSyncError, match="GITHUB_PERSONAL_ACCESS_TOKEN is missing"):
        await sync.commit_note(
            broad_category="Coding",
            subcategory="Algorithms",
            title="Dijkstra",
            markdown_content="content",
        )
