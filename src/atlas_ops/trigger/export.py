"""Export App Services trigger definitions from an Atlas project."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from atlas_ops.filenames import json_filename


class AppServicesClientLike(Protocol):
    """Subset of the App Services client used for trigger export."""

    apps: Any
    triggers: Any


@dataclass(frozen=True, slots=True)
class TriggerApp:
    """One App Services app written during a trigger export."""

    app_id: str
    app_name: str
    triggers: int


@dataclass(frozen=True, slots=True)
class TriggerExportResult:
    """App files and triggers written during an export."""

    files: int
    triggers: int
    apps: tuple[TriggerApp, ...]


def export_triggers(
    client: AppServicesClientLike,
    project_id: str,
    directory: Path,
) -> TriggerExportResult:
    """Write one JSON file per App Services app that has triggers.

    Both standard apps and the special ``atlas`` product app are scanned.
    Server-managed trigger ``_id`` values are removed; all other trigger
    definition fields are preserved.
    """
    directory.mkdir(parents=True, exist_ok=True)

    apps_by_id: dict[str, Any] = {}
    for product in (None, "atlas"):
        for app in client.apps.admin_list_applications(group_id=project_id, product=product):
            app_id = getattr(app, "id", None)
            if isinstance(app_id, str) and app_id and app_id not in apps_by_id:
                apps_by_id[app_id] = app

    files = 0
    triggers = 0
    exported: list[TriggerApp] = []
    apps = sorted(
        apps_by_id.values(),
        key=lambda app: (
            str(getattr(app, "name", "") or ""),
            str(getattr(app, "id", "") or ""),
        ),
    )
    for app in apps:
        app_id = getattr(app, "id", None)
        if not isinstance(app_id, str) or not app_id:
            continue
        app_name = getattr(app, "name", None)
        if not isinstance(app_name, str) or not app_name:
            app_name = app_id

        raw_triggers = client.triggers.admin_list_triggers(
            app_id=app_id,
            group_id=project_id,
        )
        definitions = [
            definition
            for trigger in raw_triggers
            if (definition := _trigger_definition(trigger)) is not None
        ]
        if not definitions:
            continue
        definitions.sort(key=lambda item: str(item.get("name") or ""))

        document = {
            "appId": app_id,
            "appName": app_name,
            "triggers": definitions,
        }
        path = directory / json_filename(app_name)
        path.write_text(
            json.dumps(document, indent=2, ensure_ascii=False, default=str) + "\n",
            encoding="utf-8",
        )
        files += 1
        triggers += len(definitions)
        exported.append(TriggerApp(app_id=app_id, app_name=app_name, triggers=len(definitions)))

    return TriggerExportResult(
        files=files,
        triggers=triggers,
        apps=tuple(exported),
    )


def _trigger_definition(trigger: Any) -> dict[str, Any] | None:
    if hasattr(trigger, "model_dump"):
        raw = trigger.model_dump(by_alias=True, exclude_none=True, mode="json")
    elif isinstance(trigger, dict):
        raw = dict(trigger)
    else:
        return None

    raw.pop("_id", None)
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        return None
    return raw
