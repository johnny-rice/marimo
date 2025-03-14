# Copyright 2024 Marimo. All rights reserved.
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Optional

from marimo import _loggers
from marimo._utils.data_uri import from_data_uri

LOGGER = _loggers.marimo_logger()


@dataclass
class LayoutConfig:
    # type of layout
    type: str
    # data for layout
    data: dict[str, Any]


def save_layout_config(
    directory: str, app_name: str, config: LayoutConfig
) -> str:
    """
    Save the layout configuration to disk
    at the given directory.

    The layout is saved as a JSON file under
        <directory>/layouts/<app_name>.{type}.json
    This allows:
        - all layouts to be saved in the same directory
        - multiple layouts to be saved for the same app
        - multiple apps can live in the same directory

    Returns: the path to the layout file
    """
    # remove py extension
    app_name_without_ext = app_name.replace(".py", "")
    # relative file path
    filepath = f"layouts/{app_name_without_ext}.{config.type}.json"
    # full file path
    full_filepath = os.path.join(directory, filepath)
    # create directory if it doesn't exist
    os.makedirs(os.path.dirname(full_filepath), exist_ok=True)
    with open(full_filepath, "w", encoding="utf-8") as f:
        json.dump(config.__dict__, f, indent=2)
    return filepath


def read_layout_config(
    directory: str, filename: str
) -> Optional[LayoutConfig]:
    """
    Read the layout configuration from disk.

    Returns: the layout configuration
    """
    # Handle data URI
    if filename.startswith("data:"):
        try:
            # Decode base64
            _mime, data = from_data_uri(filename)
            # Parse as JSON
            data_json = json.loads(data)
            return LayoutConfig(type=data_json["type"], data=data_json["data"])
        except Exception as e:
            LOGGER.warning("Failed to decode data URI: %s", e)
            return None

    filepath = os.path.join(directory, filename)
    if not os.path.exists(filepath):
        LOGGER.warning("Layout file %s does not exist", filepath)
        return None
    if not filepath.endswith(".json"):
        LOGGER.warning("Layout file %s is not a JSON file", filepath)
        return None
    with open(filepath, encoding="utf-8") as f:
        data = json.load(f)
    return LayoutConfig(type=data["type"], data=data["data"])  # type: ignore[call-overload]
