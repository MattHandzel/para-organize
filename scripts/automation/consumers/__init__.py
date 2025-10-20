"""Consumer registry for automation pipelines."""

from __future__ import annotations

from typing import Dict, Iterable, List, Type

from ..config import AutomationConfig, ConsumerConfig
from .base import Consumer

REGISTRY: Dict[str, Type[Consumer]] = {}


def register(name: str):
    """Decorator to register a consumer class."""

    def decorator(cls: Type[Consumer]) -> Type[Consumer]:
        if name in REGISTRY:
            raise ValueError(f"Consumer type '{name}' already registered.")
        REGISTRY[name] = cls
        return cls

    return decorator


def get_consumer_class(name: str) -> Type[Consumer]:
    try:
        return REGISTRY[name]
    except KeyError as exc:
        raise KeyError(f"Unknown consumer type '{name}'.") from exc


def build_consumers(config: AutomationConfig) -> List[Consumer]:
    instances: List[Consumer] = []
    for consumer_config in config.consumers:
        if not consumer_config.enabled:
            continue
        cls = get_consumer_class(consumer_config.type)
        instances.append(cls(consumer_config, config))
    return instances


__all__ = [
    "Consumer",
    "build_consumers",
    "get_consumer_class",
    "register",
]

# Register built-in consumers.
# noqa import ensures registrations happen on module import.
from . import taskwarrior  # noqa: E402,F401
