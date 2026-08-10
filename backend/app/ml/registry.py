from __future__ import annotations

from pathlib import Path
import os
import tempfile

import joblib

from .types import ModelBundle


class ModelRegistry:
    """Small filesystem registry; database metadata can reference these files."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def save(self, bundle: ModelBundle) -> Path:
        target = self.directory / f"{bundle.model_id}.joblib"
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{bundle.model_id}.", suffix=".tmp", dir=self.directory
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            joblib.dump(bundle, temporary)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return target

    def load(self, model_id: str) -> ModelBundle:
        bundle = joblib.load(self.directory / f"{model_id}.joblib")
        if not isinstance(bundle, ModelBundle):
            raise TypeError("registry file does not contain a ModelBundle")
        return bundle

