import re
from enum import Enum
from typing import Dict, List, Optional
from pydantic import BaseModel, Field, field_validator, model_validator


class BroadCategory(str, Enum):
    CODING = "Coding"
    FOOD_RECIPES = "Food Recipes"
    COOL_TOOLS = "Cool Tools & Websites"
    LEARNING = "Learning & Concepts"


TAXONOMY: Dict[BroadCategory, List[str]] = {
    BroadCategory.CODING: [
        "System Design",
        "AI Tools & Vibecoding",
        "Backend & Cloud",
        "Frontend & UI",
        "Algorithms",
    ],
    BroadCategory.FOOD_RECIPES: [
        "Pasta & Mains",
        "Desserts & Baking",
        "Quick Bites",
        "Sauces & Prep",
    ],
    BroadCategory.COOL_TOOLS: [
        "Developer Productivity",
        "Web Apps",
        "AI Utilities",
        "Design Resources",
    ],
    BroadCategory.LEARNING: [
        "Computer Science",
        "Productivity",
        "Finance",
        "Science & Tech",
    ],
}

INSTAGRAM_REEL_REGEX = re.compile(
    r"^https?://(?:www\.)?instagram\.com/(?:reel|reels|p)/([a-zA-Z0-9_-]+)",
    re.IGNORECASE,
)


def normalize_reel_url(url: str) -> str:
    """
    Normalizes an Instagram Reel/Post URL to canonical format:
    https://www.instagram.com/reel/<SHORTCODE>/
    Strips query parameters (e.g. ?srtk=..., ?igsh=...), trailing slashes, and anchors.
    """
    clean_url = url.strip()
    match = INSTAGRAM_REEL_REGEX.search(clean_url)
    if not match:
        raise ValueError(
            f"Invalid Instagram URL: '{clean_url}'. Must match format https://www.instagram.com/reel/<id>/ or /p/<id>/"
        )
    shortcode = match.group(1)
    return f"https://www.instagram.com/reel/{shortcode}/"


class IngestRequest(BaseModel):
    url: str = Field(..., description="Instagram Reel or Post URL")

    @field_validator("url")
    @classmethod
    def validate_instagram_url(cls, v: str) -> str:
        # Validates and normalizes URL to canonical format
        return normalize_reel_url(v)


class VaultIndex(BaseModel):
    urls: Dict[str, str] = Field(
        default_factory=dict,
        description="Normalized URL mapping to vault note relative file path",
    )
    authors: Dict[str, List[str]] = Field(
        default_factory=dict,
        description="Creator handle (e.g. @username) mapping to list of note paths",
    )
    taxonomy: Dict[str, List[str]] = Field(
        default_factory=dict,
        description="Broad category to list of active subcategories",
    )
    last_synced_sha: Optional[str] = Field(
        default=None,
        description="Git blob SHA of .reel_index.json",
    )


class ReelAnalysis(BaseModel):
    title: str = Field(
        ...,
        description="Concise, action-oriented title summarizing the reel's core insight or recipe",
    )
    author: Optional[str] = Field(
        default=None,
        description="Extracted creator handle (e.g., @handle)",
    )
    is_part_of_series: bool = Field(
        default=False,
        description="True if this reel continues a prior ongoing project or topic by the creator",
    )
    series_title: Optional[str] = Field(
        default=None,
        description="Title of the series if part of a recurring series",
    )
    broad_category: BroadCategory = Field(
        ...,
        description="The primary classification of the reel content",
    )
    subcategory: str = Field(
        ...,
        description="Appropriate subcategory within the chosen broad category",
    )
    tags: List[str] = Field(
        default_factory=list,
        description="Categorical and thematic tags formatted with hash prefix (e.g. #Coding/Backend, #Python)",
    )
    quick_takeaway: str = Field(
        ...,
        description="2-3 sentence high-impact summary of the main takeaway",
    )
    actionable_content: List[str] = Field(
        default_factory=list,
        description="Bulleted action items, step-by-step instructions, code snippets, or ingredients list",
    )
    links_mentioned: List[str] = Field(
        default_factory=list,
        description="Extracted tool names, library names, GitHub repositories, or external URLs mentioned",
    )

    @model_validator(mode="after")
    def validate_category_taxonomy(self) -> "ReelAnalysis":
        # Format author with @ if provided
        if self.author:
            clean_author = self.author.strip()
            if not clean_author.startswith("@"):
                self.author = f"@{clean_author}"
            else:
                self.author = clean_author

        # Clean subcategory
        self.subcategory = self.subcategory.strip()
        valid_subcategories = TAXONOMY.get(self.broad_category, [])
        matches = [
            sub
            for sub in valid_subcategories
            if sub.lower() == self.subcategory.lower()
            or sub.lower() in self.subcategory.lower()
            or self.subcategory.lower() in sub.lower()
        ]
        if matches:
            self.subcategory = matches[0]
        # If novel domain, keep the clean, descriptive subcategory
        return self


class ExtractionResult(BaseModel):
    source_url: str
    caption: Optional[str] = None
    uploader: Optional[str] = None
    title: Optional[str] = None
    audio_path: Optional[str] = None
    audio_mime_type: Optional[str] = None
    duration: Optional[float] = None
