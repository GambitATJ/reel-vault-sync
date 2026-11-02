import json
from unittest.mock import MagicMock, patch
import pytest
import yaml

from analyzer import ReelAnalyzer, generate_obsidian_markdown
from schemas import BroadCategory, ExtractionResult, ReelAnalysis, TAXONOMY


@pytest.fixture
def sample_analysis():
    return ReelAnalysis(
        title="Deploying FastAPI to Cloud Run",
        broad_category=BroadCategory.CODING,
        subcategory="Backend & Cloud",
        tags=["#Coding/Backend", "#FastAPI", "#Docker", "#GCP"],
        quick_takeaway="Deploying containerized FastAPI applications to Cloud Run is cost-effective and provides automatic scaling. Use a multi-stage Dockerfile.",
        actionable_content=[
            "Create Dockerfile with python:3.11-slim",
            "Expose port 8080",
            "Deploy with gcloud run deploy --image gcr.io/proj/app",
        ],
        links_mentioned=[
            "https://cloud.google.com/run",
            "fastapi.tiangolo.com",
        ],
    )


def test_markdown_output_yaml_frontmatter(sample_analysis):
    url = "https://www.instagram.com/reel/C3bX123456/"
    markdown = generate_obsidian_markdown(sample_analysis, source_url=url)

    # Check start and end of frontmatter
    assert markdown.startswith("---\n")
    parts = markdown.split("---\n")
    assert len(parts) >= 3

    # Parse YAML frontmatter
    frontmatter_raw = parts[1]
    parsed_yaml = yaml.safe_load(frontmatter_raw)

    assert parsed_yaml["source"] == url
    assert parsed_yaml["category"] == "Coding"
    assert parsed_yaml["subcategory"] == "Backend & Cloud"
    assert parsed_yaml["tags"] == ["#Coding/Backend", "#FastAPI", "#Docker", "#GCP"]
    assert "created_at" in parsed_yaml
    assert parsed_yaml["status"] == "inbox"

    # Check Markdown headings
    body = "---\n".join(parts[2:])
    assert "# Deploying FastAPI to Cloud Run" in body
    assert "## Quick Takeaway (2-3 sentences)" in body
    assert "## Key Details / Steps / Recipe (Bulleted action items, code snippets, or ingredients)" in body
    assert "- Create Dockerfile with python:3.11-slim" in body
    assert "## Referenced Links & Tools (Extracted tool names, repositories, or URLs)" in body
    assert "- https://cloud.google.com/run" in body


def test_categorization_strictly_within_allowed_enums():
    # Verify all enum values match the 4 required categories
    expected_categories = {
        "Coding",
        "Food Recipes",
        "Cool Tools & Websites",
        "Learning & Concepts",
    }
    actual_categories = {cat.value for cat in BroadCategory}
    assert actual_categories == expected_categories

    # Verify subcategories match the required taxonomy
    assert "System Design" in TAXONOMY[BroadCategory.CODING]
    assert "Pasta & Mains" in TAXONOMY[BroadCategory.FOOD_RECIPES]
    assert "Developer Productivity" in TAXONOMY[BroadCategory.COOL_TOOLS]
    assert "Computer Science" in TAXONOMY[BroadCategory.LEARNING]


@patch("analyzer.genai.Client")
def test_analyzer_with_audio_and_mock_gemini(mock_client_class, tmp_path):
    mock_client = MagicMock()
    mock_client_class.return_value = mock_client

    # Mock audio upload & cleanup
    mock_uploaded_file = MagicMock()
    mock_uploaded_file.name = "files/test_audio_123"
    mock_client.files.upload.return_value = mock_uploaded_file

    # Mock response from Gemini 1.5 Flash
    mock_response = MagicMock()
    mock_response.text = json.dumps({
        "title": "Creamy Tuscan Garlic Pasta",
        "broad_category": "Food Recipes",
        "subcategory": "Pasta & Mains",
        "tags": ["#FoodRecipes/Pasta-and-Mains", "#DinnerIdeas"],
        "quick_takeaway": "A rich and savory Tuscan garlic pasta made in under 20 minutes.",
        "actionable_content": [
            "Boil fettuccine until al dente",
            "Saute garlic and sun-dried tomatoes in olive oil",
            "Add heavy cream and parmesan",
        ],
        "links_mentioned": [],
    })
    mock_client.models.generate_content.return_value = mock_response

    # Dummy audio file
    dummy_audio = tmp_path / "audio.m4a"
    dummy_audio.write_bytes(b"dummy audio")

    extraction = ExtractionResult(
        source_url="https://www.instagram.com/reel/C3bX123456/",
        caption="Quick garlic pasta recipe!",
        uploader="chef_mario",
        title="Creamy Tuscan Garlic Pasta",
        audio_path=str(dummy_audio),
        audio_mime_type="audio/mp4",
        duration=35.0,
    )

    analyzer = ReelAnalyzer(api_key="mock_key")
    result = analyzer.analyze(extraction)

    assert result.title == "Creamy Tuscan Garlic Pasta"
    assert result.broad_category == BroadCategory.FOOD_RECIPES
    assert result.subcategory == "Pasta & Mains"
    assert len(result.actionable_content) == 3

    # Check audio was uploaded with mime_type and subsequently deleted
    mock_client.files.upload.assert_called_once_with(
        file=str(dummy_audio),
        config={"mime_type": "audio/mp4"},
    )
    mock_client.files.delete.assert_called_once_with(name="files/test_audio_123")


@patch("analyzer.genai.Client")
def test_analyzer_fallback_to_caption_when_audio_fails(mock_client_class):
    mock_client = MagicMock()
    mock_client_class.return_value = mock_client

    # Audio upload raises exception
    mock_client.files.upload.side_effect = RuntimeError("Upload failed")

    mock_response = MagicMock()
    mock_response.text = json.dumps({
        "title": "Top 3 Developer Productivity Extensions",
        "broad_category": "Cool Tools & Websites",
        "subcategory": "Developer Productivity",
        "tags": ["#CoolTools/Developer-Productivity", "#VSCode"],
        "quick_takeaway": "These three extensions will save you hours of debugging each week.",
        "actionable_content": [
            "Install Error Lens",
            "Install GitLens",
            "Configure Prettier formatting on save",
        ],
        "links_mentioned": ["marketplace.visualstudio.com"],
    })
    mock_client.models.generate_content.return_value = mock_response

    extraction = ExtractionResult(
        source_url="https://www.instagram.com/reel/C3bX123456/",
        caption="Top 3 VS Code tools every developer needs",
        uploader="code_daily",
        audio_path="/tmp/non_existent_or_failed.m4a",
    )

    analyzer = ReelAnalyzer(api_key="mock_key")
    result = analyzer.analyze(extraction)

    assert result.title == "Top 3 Developer Productivity Extensions"
    assert result.broad_category == BroadCategory.COOL_TOOLS
    assert result.subcategory == "Developer Productivity"
    mock_client.models.generate_content.assert_called_once()


def test_markdown_output_with_author_and_series():
    analysis = ReelAnalysis(
        title="Microservices Part 3",
        author="@alex_dev",
        is_part_of_series=True,
        series_title="Distributed Systems Series",
        broad_category=BroadCategory.CODING,
        subcategory="System Design",
        tags=["#Coding/SystemDesign"],
        quick_takeaway="Implement idempotent consumers using Redis locks.",
        actionable_content=["Set lock with TTL", "Check execution key"],
    )
    url = "https://www.instagram.com/reel/C3bX123456/"
    markdown = generate_obsidian_markdown(analysis, source_url=url)
    parsed = yaml.safe_load(markdown.split("---\n")[1])
    assert parsed["author"] == "@alex_dev"
    assert parsed["series"] == "Distributed Systems Series"
    assert parsed["category"] == "Coding"


def test_format_series_entry():
    from analyzer import format_series_entry
    analysis = ReelAnalysis(
        title="Episode 2: Message Queues",
        author="@tech_lead",
        is_part_of_series=True,
        series_title="Backend Series",
        broad_category=BroadCategory.CODING,
        subcategory="Backend & Cloud",
        tags=["#Coding/Backend"],
        quick_takeaway="RabbitMQ for complex routing, Kafka for high-volume logs.",
        actionable_content=["Use RabbitMQ for RPC", "Use Kafka for event streaming"],
    )
    entry = format_series_entry(analysis, source_url="https://www.instagram.com/reel/Episode2/")
    assert "### Entry: Episode 2: Message Queues" in entry
    assert "- **Source:** https://www.instagram.com/reel/Episode2/" in entry
    assert "RabbitMQ for complex routing" in entry
    assert "- Use RabbitMQ for RPC" in entry


@patch("analyzer.genai.Client")
def test_analyzer_passes_taxonomy_and_authors_to_prompt(mock_client_class):
    mock_client = MagicMock()
    mock_client_class.return_value = mock_client
    mock_response = MagicMock()
    mock_response.text = json.dumps({
        "title": "Clean Architecture in Rust",
        "author": "@rustacean",
        "is_part_of_series": False,
        "broad_category": "Coding",
        "subcategory": "Algorithms",
        "tags": ["#Coding/Algorithms"],
        "quick_takeaway": "Organize domain entities and use dependency inversion.",
        "actionable_content": ["Define traits for repositories"],
        "links_mentioned": [],
    })
    mock_client.models.generate_content.return_value = mock_response

    analyzer = ReelAnalyzer(api_key="mock_key")
    extraction = ExtractionResult(
        source_url="https://www.instagram.com/reel/Rust123/",
        caption="Domain driven design in Rust",
    )
    taxonomy_context = {"Coding": ["Backend & Cloud", "System Design"]}
    authors_context = {"@rustacean": ["Inbox/Reels/Coding/Rust.md"]}

    result = analyzer.analyze(extraction, taxonomy=taxonomy_context, authors=authors_context)
    assert result.author == "@rustacean"

    # Verify that prompt received taxonomy and authors
    generate_call_args = mock_client.models.generate_content.call_args[1]
    prompt_sent = [c for c in generate_call_args["contents"] if isinstance(c, str)][0]
    assert "Active Vault Taxonomy" in prompt_sent
    assert "Known Vault Authors: @rustacean" in prompt_sent
