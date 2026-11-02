import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional
import yaml

from google import genai
from google.genai import types

from config import get_settings
from schemas import ExtractionResult, ReelAnalysis, BroadCategory, TAXONOMY

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an expert technical note-taker, culinary researcher, and knowledge curator.
Your task is to analyze the provided Instagram Reel content (audio and/or caption/metadata) and extract deep, high-yield, structured knowledge.

CRITICAL EXTRACTION DIRECTIVES:
1. AUDIO-FIRST ANALYSIS: If audio is provided, listen attentively and transcribe all spoken content internally. Treat spoken speech as the primary source of truth.
2. HIGH-DENSITY, ACTIONABLE DEPTH:
   - Do NOT produce vague or generic summaries (e.g., instead of "the creator discusses database indexing", write "explains B-Tree vs LSM-Tree trade-offs for write-heavy workloads, recommending LSM for write throughput up to 50k ops/sec").
   - For Food Recipes: You MUST extract exact spoken measurements (grams, cups, tsp), temperatures, cooking times, and step-by-step techniques. Never output a generic dish summary without the exact ingredient ratios and steps spoken.
   - For Coding & Systems: Extract exact terminal/CLI commands, package names, configuration flags, algorithmic steps, architecture trade-offs, and code logic.
   - For Tools & Websites: State the exact utility, whether it is free/open-source, installation commands, and specific standout features.
3. COMPLETION OVER BREVITY: Provide an exhaustive breakdown. If the creator provides a 5-step process or lists 6 ingredients, capture all of them in actionable_content.

Taxonomy & Category Rules:
1. Coding (Default Subcategories: System Design, AI Tools & Vibecoding, Backend & Cloud, Frontend & UI, Algorithms)
2. Food Recipes (Default Subcategories: Pasta & Mains, Desserts & Baking, Quick Bites, Sauces & Prep)
3. Cool Tools & Websites (Default Subcategories: Developer Productivity, Web Apps, AI Utilities, Design Resources)
4. Learning & Concepts (Default Subcategories: Computer Science, Productivity, Finance, Science & Tech)
- DYNAMIC TAXONOMY: Inspect the provided active taxonomy context. If an existing subcategory fits, use it. If the reel covers a distinct, novel domain not well-captured by existing subcategories, create a clean, descriptive new subcategory name.

Author & Recurring Series Grouping Rules:
- Identify the creator handle (e.g. @handle) and populate the `author` field.
- If the author has known series notes or if the reel explicitly continues an ongoing series/project (e.g., "Day 4 of building a SaaS" or "Part 2 of sourdough mastery"):
  - Set `is_part_of_series = true`.
  - Provide a concise `series_title` (e.g., "Building Microservices in Go Series").
  - Structure `actionable_content` as an appendable, dated entry detailing today's lesson/update.

Output Field Requirements:
- title: A concise, action-oriented title describing the core insight, pattern, or recipe.
- author: Creator handle with @ prefix (e.g., @dev_guru).
- is_part_of_series: Boolean indicating if this is part of an ongoing series or project.
- series_title: Title of recurring series if is_part_of_series is true, else null.
- broad_category: One of the 4 exact broad categories above.
- subcategory: Subcategory name (matching active taxonomy or clean new subcategory).
- tags: List of tags. The first tag MUST be '#<broad_category>/<subcategory>' (with spaces removed or replaced with hyphens, e.g., '#Coding/Backend-and-Cloud' or '#FoodRecipes/Pasta-and-Mains'), followed by 2-5 relevant topic hashtags (e.g., '#Python', '#Postgres', '#FastAPI').
- quick_takeaway: 2-3 sentences summarizing the exact core message and practical value.
- actionable_content: Comprehensive bulleted list containing exact steps, ingredient measurements, implementation commands, or architectural decisions.
- links_mentioned: Names of tools, libraries, GitHub repositories, creators, or URLs explicitly mentioned or shown.
"""


class ReelAnalyzer:
    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        settings = get_settings()
        self.api_key = api_key if api_key is not None else settings.GEMINI_API_KEY
        self.model = model if model is not None else settings.GEMINI_MODEL
        self._client: Optional[genai.Client] = None

    @property
    def client(self) -> genai.Client:
        if self._client is None:
            if not self.api_key:
                raise ValueError("GEMINI_API_KEY is not configured.")
            self._client = genai.Client(api_key=self.api_key)
        return self._client

    def analyze(
        self,
        extraction: ExtractionResult,
        taxonomy: Optional[Dict[str, List[str]]] = None,
        authors: Optional[Dict[str, List[str]]] = None,
    ) -> ReelAnalysis:
        """
        Analyzes the extracted reel using Gemini multimodal models.
        Incorporate active taxonomy and author history to enable dynamic taxonomy
        and recurring series detection.
        """
        uploaded_file = None
        contents = []

        try:
            # Try uploading native audio stream if available
            if extraction.audio_path and os.path.exists(extraction.audio_path):
                try:
                    mime = extraction.audio_mime_type
                    if not mime:
                        ext = os.path.splitext(extraction.audio_path)[1].lower()
                        mime = "audio/mp4" if ext == ".m4a" else f"audio/{ext.lstrip('.') or 'mp4'}"
                    uploaded_file = self.client.files.upload(
                        file=extraction.audio_path,
                        config={"mime_type": mime},
                    )
                    contents.append(uploaded_file)
                    logger.info(f"Successfully uploaded audio file ({mime}): {uploaded_file.name}")
                except Exception as e:
                    logger.warning(
                        f"Failed to upload audio to Gemini ({e}). Falling back to caption text analysis."
                    )

            # Build prompt with metadata, taxonomy, and author context
            metadata_context = (
                f"Instagram Reel URL: {extraction.source_url}\n"
                f"Creator / Uploader: {extraction.uploader or 'Unknown'}\n"
                f"Post Title / Caption:\n{extraction.caption or 'No caption provided.'}\n"
            )

            taxonomy_context = ""
            if taxonomy:
                formatted_tax = "\n".join(
                    f"  - {cat}: {', '.join(subcats)}"
                    for cat, subcats in taxonomy.items()
                )
                taxonomy_context = f"\nActive Vault Taxonomy:\n{formatted_tax}\n"

            author_context = ""
            if authors:
                author_names = list(authors.keys())[:25]
                author_context = f"\nKnown Vault Authors: {', '.join(author_names)}\n"

            prompt_text = (
                f"{SYSTEM_PROMPT}\n\nContext:\n{metadata_context}"
                f"{taxonomy_context}{author_context}"
            )
            contents.append(prompt_text)

            config = types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=ReelAnalysis,
                temperature=0.2,
            )

            response = self.client.models.generate_content(
                model=self.model,
                contents=contents,
                config=config,
            )

            raw_text = response.text or ""
            # Clean possible markdown block wrappers if present
            raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text.strip(), flags=re.IGNORECASE)
            raw_text = re.sub(r"\s*```$", "", raw_text)

            analysis = ReelAnalysis.model_validate_json(raw_text)
            return analysis

        finally:
            if uploaded_file and hasattr(uploaded_file, "name"):
                try:
                    self.client.files.delete(name=uploaded_file.name)
                    logger.info(f"Cleaned up remote Gemini file: {uploaded_file.name}")
                except Exception as del_err:
                    logger.warning(f"Could not delete remote Gemini file {uploaded_file.name}: {del_err}")


def generate_obsidian_markdown(
    analysis: ReelAnalysis,
    source_url: str,
    created_at: Optional[datetime] = None,
) -> str:
    """
    Converts ReelAnalysis into standard Obsidian-compatible Markdown
    with valid YAML frontmatter and structured headers.
    """
    if created_at is None:
        created_at = datetime.now(timezone.utc)

    # Format YAML frontmatter
    frontmatter_dict = {
        "source": source_url,
    }
    if analysis.author:
        frontmatter_dict["author"] = analysis.author
    if analysis.is_part_of_series and analysis.series_title:
        frontmatter_dict["series"] = analysis.series_title

    frontmatter_dict.update({
        "category": analysis.broad_category.value if isinstance(analysis.broad_category, BroadCategory) else str(analysis.broad_category),
        "subcategory": analysis.subcategory,
        "tags": analysis.tags,
        "created_at": created_at.isoformat(),
        "status": "inbox",
    })

    yaml_block = yaml.dump(
        frontmatter_dict,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    ).strip()

    # Format actionable content
    if analysis.actionable_content:
        action_bullets = "\n".join(
            f"- {item.strip()}" if not item.strip().startswith("-") else item.strip()
            for item in analysis.actionable_content
        )
    else:
        action_bullets = "- No actionable steps detailed."

    # Format links
    if analysis.links_mentioned:
        links_bullets = "\n".join(
            f"- {link.strip()}" if not link.strip().startswith("-") else link.strip()
            for link in analysis.links_mentioned
        )
    else:
        links_bullets = "- None mentioned."

    markdown_body = f"""---
{yaml_block}
---

# {analysis.title}

## Quick Takeaway (2-3 sentences)
{analysis.quick_takeaway}

## Key Details / Steps / Recipe (Bulleted action items, code snippets, or ingredients)
{action_bullets}

## Referenced Links & Tools (Extracted tool names, repositories, or URLs)
{links_bullets}
"""
    return markdown_body


def format_series_entry(
    analysis: ReelAnalysis,
    source_url: str,
    created_at: Optional[datetime] = None,
) -> str:
    """
    Formats an appendable dated entry for an ongoing series note.
    """
    if created_at is None:
        created_at = datetime.now(timezone.utc)

    if analysis.actionable_content:
        action_bullets = "\n".join(
            f"- {item.strip()}" if not item.strip().startswith("-") else item.strip()
            for item in analysis.actionable_content
        )
    else:
        action_bullets = "- No actionable steps detailed."

    entry = f"""
---
### Entry: {analysis.title} (Date: {created_at.strftime('%Y-%m-%d %H:%M:%S UTC')})
- **Source:** {source_url}
- **Quick Takeaway:** {analysis.quick_takeaway}

#### Key Takeaways & Steps:
{action_bullets}
"""
    return entry
