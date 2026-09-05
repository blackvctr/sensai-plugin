"""Local policy for controlled Claude installation acceptance.

This policy is not read from the public README. It defines the local acceptance
test's canonical new-chat URI and the limited observable facts accepted for a
visible message.
"""

from __future__ import annotations

from dataclasses import dataclass
from unicodedata import category, name
from urllib.parse import quote

CONTROLLED_NEW_CHAT_REQUEST = (
    "Проконсультируйся с Sensai. Сначала задай мне вопросы о моей работе, "  # noqa: RUF001
    "обычных программах и повторяющихся задачах."
)
CONTROLLED_NEW_CHAT_URI = "claude://code/new?q=" + quote(CONTROLLED_NEW_CHAT_REQUEST, safe="")
CONTROLLED_CLAUDE_LINUX_ACTIONS = (
    ("claude", "plugin", "marketplace", "add", "blackvctr/sensai-plugin"),
    ("claude", "plugin", "install", "sensai@sensai", "--scope", "user"),
    ("script", "-q", "-c", "claude mcp login plugin:sensai:sensai", "/dev/null"),
    ("xdg-open", CONTROLLED_NEW_CHAT_URI),
)


@dataclass(frozen=True, slots=True)
class ObservableMessageFacts:
    """In-memory, non-semantic facts about one visible Claude message."""

    non_whitespace_characters: int
    cyrillic_letters: int
    latin_letters: int
    contains_markdown_code: bool


def message_facts(text: str) -> ObservableMessageFacts:
    cyrillic_letters = 0
    latin_letters = 0
    for character in text:
        if not category(character).startswith("L"):
            continue
        unicode_name = name(character, "")
        if "CYRILLIC" in unicode_name:
            cyrillic_letters += 1
        elif "LATIN" in unicode_name:
            latin_letters += 1
    return ObservableMessageFacts(
        non_whitespace_characters=sum(not character.isspace() for character in text),
        cyrillic_letters=cyrillic_letters,
        latin_letters=latin_letters,
        contains_markdown_code="`" in text,
    )


def message_facts_meet_contract(facts: ObservableMessageFacts) -> bool:
    """Accept language and safety shape only; this is never a semantic proof."""

    return (
        facts.non_whitespace_characters > 0
        and facts.cyrillic_letters > facts.latin_letters
        and not facts.contains_markdown_code
    )
