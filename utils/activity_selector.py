"""
Activity Selector Module for Poodle LMS Automation.

Provides similarity-based activity classification and grouping:
- Similarity-based grouping (Read-Aloud, H5P, WordCards, Solo, Generic)
- Group definition mappings for interactive selectors
"""

import sys
import re
from typing import Optional

GROUP_DEFINITIONS = [
    {
        "id": "readaloud",
        "name": "Read - Aloud",
        "description": "Speech pronunciation and reading exercises",
        "match_types": ["readaloud"]
    },
    {
        "id": "hvp",
        "name": "H5P / Interactive Video",
        "description": "Interactive video queries, drag-and-drop & quiz questions",
        "match_types": ["hvp", "h5pactivity"]
    },
    {
        "id": "wordcards",
        "name": "WordCards",
        "description": "Vocabulary recognition and flashcard drills",
        "match_types": ["wordcards"]
    },
    {
        "id": "solo",
        "name": "Solo Speaking",
        "description": "Continuous speech monologue and speaking prompts",
        "match_types": ["solo"]
    },
    {
        "id": "quiz",
        "name": "Quiz & Assessments",
        "description": "Online quizzes, multiple choice and practice tests",
        "match_types": ["quiz"]
    },
    {
        "id": "generic",
        "name": "Other Activities",
        "description": "Standard Moodle course resources and modules",
        "match_types": []
    }
]


def classify_activity(act: dict) -> str:
    """Returns the group ID for an activity based on its type or URL."""
    act_type = str(act.get("type", "")).lower()
    url = str(act.get("url", "")).lower()

    for grp in GROUP_DEFINITIONS:
        for t in grp["match_types"]:
            if t == act_type or f"/mod/{t}/" in url:
                return grp["id"]
    return "generic"


def group_activities(activities: list[dict]) -> list[dict]:
    """
    Groups activities into structured similarity buckets with global 1-based indexing.
    Returns a list of group dictionaries:
    [
        {
            "id": "readaloud",
            "name": "Read - Aloud",
            "description": "...",
            "items": [(1, act1), (2, act2)]
        },
        ...
    ]
    """
    buckets = {grp["id"]: [] for grp in GROUP_DEFINITIONS}

    for idx, act in enumerate(activities, 1):
        grp_id = classify_activity(act)
        buckets[grp_id].append((idx, act))

    result = []
    for grp in GROUP_DEFINITIONS:
        items = buckets[grp["id"]]
        if items:
            result.append({
                "id": grp["id"],
                "name": grp["name"],
                "description": grp["description"],
                "items": items
            })
    return result
