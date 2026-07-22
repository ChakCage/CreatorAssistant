from __future__ import annotations

import os
import sys
from enum import Enum

DEVELOPER_AI_MODEL = "qwen3.6:35b-a3b"
COMMERCIAL_AI_MODELS = ("qwen3:14b", "qwen3.6:35b-a3b")


class AppEdition(str, Enum):
    DEVELOPER = "developer"
    COMMERCIAL = "commercial"

    @property
    def display_name(self) -> str:
        return "Developer Edition" if self is AppEdition.DEVELOPER else "Commercial Edition"

    @property
    def application_name(self) -> str:
        return "Creator Assistant Developer" if self is AppEdition.DEVELOPER else "Creator Assistant"


class Feature(str, Enum):
    PROJECT_PREPARATION = "project_preparation"
    SHORTS = "shorts"
    LOCAL_AI = "local_ai"
    DIAGNOSTICS = "diagnostics"
    AUTOPILOT = "autopilot"
    PUBLISHING_QUEUE = "publishing_queue"
    YOUTUBE_PUBLISHING = "youtube_publishing"
    LICENSING = "licensing"
    HELP = "help"
    UPDATES = "updates"
    SETUP_WIZARD = "setup_wizard"


def current_edition() -> AppEdition:
    # A packaged edition is sealed into build_info.json. Environment variables
    # cannot turn a Commercial binary into Developer at runtime.
    if getattr(sys, "frozen", False):
        from creator_assistant.infrastructure.build_info import current_build_info

        value = current_build_info().edition
    else:
        value = os.environ.get("CREATOR_ASSISTANT_EDITION", AppEdition.DEVELOPER.value)
    try:
        return AppEdition(str(value).strip().casefold())
    except ValueError as exc:
        raise RuntimeError(f"Unknown Creator Assistant edition: {value!r}") from exc


class FeatureRegistry:
    _common = {
        Feature.PROJECT_PREPARATION,
        Feature.SHORTS,
        Feature.LOCAL_AI,
        Feature.HELP,
        Feature.UPDATES,
    }
    _by_edition = {
        AppEdition.DEVELOPER: _common | {
            Feature.DIAGNOSTICS,
            Feature.AUTOPILOT,
            Feature.PUBLISHING_QUEUE,
            Feature.YOUTUBE_PUBLISHING,
        },
        AppEdition.COMMERCIAL: _common | {Feature.LICENSING, Feature.DIAGNOSTICS, Feature.SETUP_WIZARD},
    }

    @classmethod
    def is_available(cls, feature: Feature, edition: AppEdition | None = None) -> bool:
        return feature in cls._by_edition[edition or current_edition()]

    @classmethod
    def available_features(cls, edition: AppEdition | None = None) -> frozenset[Feature]:
        return frozenset(cls._by_edition[edition or current_edition()])

    @classmethod
    def navigation_tabs(cls, edition: AppEdition | None = None) -> tuple[str, ...]:
        resolved = edition or current_edition()
        tabs = ["Подготовка проекта", "Shorts"]
        if cls.is_available(Feature.AUTOPILOT, resolved):
            tabs.append("Автопилот")
        if cls.is_available(Feature.PUBLISHING_QUEUE, resolved):
            tabs.append("Очередь публикаций")
        return tuple(tabs)


def build_artifact(edition: AppEdition) -> tuple[str, str, str]:
    if edition is AppEdition.DEVELOPER:
        return (
            "CreatorAssistant-Developer",
            "CreatorAssistant-Developer.exe",
            "Creator Assistant Developer",
        )
    return ("CreatorAssistant", "CreatorAssistant.exe", "Creator Assistant")


def qsettings_application_name(edition: AppEdition | None = None) -> str:
    resolved = edition or current_edition()
    return "CreatorAssistantDeveloper" if resolved is AppEdition.DEVELOPER else "CreatorAssistantCommercial"
