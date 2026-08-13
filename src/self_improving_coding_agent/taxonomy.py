"""Load the fixed tag taxonomy from YAML into the Taxonomy contract."""

from __future__ import annotations

from pathlib import Path

import yaml

from .contracts import Taxonomy

DEFAULT_TAXONOMY_PATH = Path(__file__).resolve().parents[2] / "knowledge" / "taxonomy.yaml"


def load_taxonomy(path: Path | None = None) -> Taxonomy:
    data = yaml.safe_load((path or DEFAULT_TAXONOMY_PATH).read_text())
    return Taxonomy.model_validate(data)
