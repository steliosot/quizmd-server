from __future__ import annotations

import random
from typing import Iterable

ADJECTIVES = [
    "Sneaky",
    "Dancing",
    "Curious",
    "Brave",
    "Sparkly",
    "Nimble",
    "Witty",
    "Cheeky",
    "Sleepy",
    "Happy",
    "Mighty",
    "Swift",
]

ANIMALS = [
    "Fox",
    "Llama",
    "Koala",
    "Otter",
    "Panda",
    "Falcon",
    "Wolf",
    "Penguin",
    "Tiger",
    "Hedgehog",
    "Dolphin",
    "Eagle",
]


def generate_funny_name(existing: Iterable[str] | None = None) -> str:
    existing_set = {name.strip().lower() for name in (existing or []) if name}
    for _ in range(150):
        candidate = f"{random.choice(ADJECTIVES)} {random.choice(ANIMALS)}"
        if candidate.lower() not in existing_set:
            return candidate

    suffix = random.randint(100, 999)
    return f"Curious Otter {suffix}"


def ensure_unique_name(name: str, existing: Iterable[str] | None = None) -> str:
    existing_set = {item.strip().lower() for item in (existing or []) if item}
    candidate = (name or "").strip()
    if not candidate:
        candidate = generate_funny_name(existing)

    if candidate.lower() not in existing_set:
        return candidate

    suffix = 2
    while True:
        attempt = f"{candidate} #{suffix}"
        if attempt.lower() not in existing_set:
            return attempt
        suffix += 1
