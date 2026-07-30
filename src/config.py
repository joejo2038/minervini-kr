"""설정 로딩. config.yaml을 점 표기법으로 접근할 수 있게 감쌉니다."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent


class Config:
    def __init__(self, data: dict):
        self._data = data

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def get(self, path: str, default: Any = None) -> Any:
        """'vcp.min_contractions' 같은 점 표기법으로 조회합니다."""
        node: Any = self._data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def path(self, path: str, default: Any = None) -> Path:
        """설정값을 프로젝트 루트 기준 절대경로로 변환합니다."""
        value = self.get(path, default)
        p = Path(value)
        return p if p.is_absolute() else ROOT / p

    @property
    def raw(self) -> dict:
        return self._data


def load_config(path: str | Path | None = None) -> Config:
    cfg_path = Path(path) if path else ROOT / "config.yaml"
    with open(cfg_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return Config(data)


def env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)
