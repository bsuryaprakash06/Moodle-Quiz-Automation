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


def parse_cli_selection(user_input: str, total_count: int, activities: list[dict], tracker=None) -> list[int]:
    """
    Parses CLI user input into a list of 1-based activity indices.
    Supports:
    - 'all', '0', '' -> all indices
    - 'pending' -> indices of activities not marked as completed
    - group keywords ('readaloud', 'hvp', 'wordcards', 'solo')
    - comma-separated numbers and ranges ('1, 3, 5-8')
    """
    raw = user_input.strip().lower()

    if not raw or raw in ("all", "0", "*"):
        return list(range(1, total_count + 1))

    if raw == "pending":
        if not tracker:
            return list(range(1, total_count + 1))
        return [
            i for i, act in enumerate(activities, 1)
            if not tracker.is_completed(act["url"])
        ]

    # Check for group match
    for grp in GROUP_DEFINITIONS:
        if raw == grp["id"] or raw == grp["name"].lower() or raw.replace(" ", "") == grp["id"]:
            matching_indices = []
            for i, act in enumerate(activities, 1):
                if classify_activity(act) == grp["id"]:
                    matching_indices.append(i)
            if matching_indices:
                return matching_indices

    # Parse numerical expressions like '1, 3, 5-8'
    indices = set()
    tokens = [t.strip() for t in raw.split(",") if t.strip()]
    for tok in tokens:
        if "-" in tok:
            parts = tok.split("-", 1)
            try:
                start, end = int(parts[0].strip()), int(parts[1].strip())
                for i in range(min(start, end), max(start, end) + 1):
                    if 1 <= i <= total_count:
                        indices.add(i)
            except ValueError:
                pass
        else:
            try:
                val = int(tok)
                if 1 <= val <= total_count:
                    indices.add(val)
            except ValueError:
                pass

    return sorted(list(indices))


def cli_activity_selector(activities: list[dict], course_name: str = "", tracker=None) -> list[dict]:
    """Console-based fallback selector with similarity grouping and '(all)' option."""
    grouped = group_activities(activities)
    total_count = len(activities)

    print("\n" + "=" * 68)
    print(f"ACTIVITIES SELECTION - {course_name or 'Course'}")
    print("=" * 68)
    print(f"Extracted {total_count} activities across {len(grouped)} groups.\n")
    print("  [0] (all) - Select All Activities Across All Groups")

    for grp in grouped:
        print(f"\n--- {grp['name']} ({len(grp['items'])} activities) ---")
        for idx, act in grp["items"]:
            status_text = "[PENDING]"
            raw_score = tracker.get_score(act["url"]) if tracker else ""
            if tracker and tracker.is_completed(act["url"]):
                status_text = f"[COMPLETED | Score: {raw_score}]"
            elif raw_score:
                status_text = f"[RETRY NEEDED | Score: {raw_score}]"
            print(f"  [{idx:2d}] [{act.get('type', 'act'):10s}] {act['title']} {status_text}")

    print("\n" + "-" * 68)
    print("Selection Options:")
    print("  - Type 'all' or '0' (or press Enter) to automate ALL activities")
    print("  - Type 'pending' to automate only incomplete activities")
    print("  - Type a group name (e.g. 'readaloud', 'hvp', 'wordcards', 'solo')")
    print("  - Type numbers or ranges (e.g. '1, 3, 5' or '1-4')")
    print("  - Type 'none' or 'q' to cancel and exit")
    print("-" * 68)

    while True:
        try:
            choice = input("\nEnter your selection [default: (all)]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n[Activity Selector] Operation cancelled by user.")
            return []

        if choice.lower() in ("none", "q", "cancel", "exit"):
            print("[Activity Selector] Skipping activities upon user request.")
            return []

        selected_indices = parse_cli_selection(choice, total_count, activities, tracker)
        if selected_indices:
            selected = [activities[i - 1] for i in selected_indices]
            print(f"\n[Activity Selector] Selected {len(selected)} / {total_count} activities to automate.")
            return selected
        else:
            print("[!] Invalid selection. Please enter valid numbers, 'all', 'pending', or 'q' to cancel.")


def prompt_activity_selection(
    activities: list[dict],
    course_name: str = "",
    tracker=None,
    headless: bool = False,
    force_cli: bool = False,
    auto_all: bool = False
) -> list[dict]:
    """
    Prompts the user to select which activities to automate.
    Attempts CLI selector based on configuration.
    """
    if not activities:
        return []

    if auto_all:
        print(f"\n[Activity Selector] --all flag specified. Automatically selected all {len(activities)} activities.")
        return list(activities)

    return cli_activity_selector(activities, course_name=course_name, tracker=tracker)
