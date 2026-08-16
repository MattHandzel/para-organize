"""Automation consumers (spec 06), living inside the core per spec 10 §1 —
``organize run-consumers`` replaces the separate Python package.

Layout:
    base.py            Consumer ABC, ConsumerResult, registry, NotePayload
    store.py           SQLite notes/emissions store + migration (06 §1)
    runner.py          orchestrator: ingestion, emission, checkpointing (06 §1)
    taskwarrior.py     todo captures → Taskwarrior tasks (06 §3.1)
    learn.py           notes → spaced-repetition flashcards (06 §3.2)
    question_answer.py question captures → answered notes (06 §3.3)
    deep_research.py   relationship notes → research subprocess (06 §3.4)
    tag_router.py      auto=True routes applied unattended (11 §1) [Phase 4]
    auto_tagger.py     untagged captures get LLM tags (11 §2) [Phase 4]

Importing this package registers all consumer types (each module calls
``@register`` at import). Registration is the ONLY import side effect;
constructors are pure (06 §1).
"""

# Import each consumer module for its @register side effect.
from organize_core.consumers import (  # noqa: F401
    auto_tagger,
    deep_research,
    learn,
    question_answer,
    tag_router,
    taskwarrior,
)
from organize_core.consumers.base import (  # noqa: F401
    Consumer,
    ConsumerResult,
    NotePayload,
    Status,
    get_consumer_types,
    get_implemented_consumer_types,
    register,
)
