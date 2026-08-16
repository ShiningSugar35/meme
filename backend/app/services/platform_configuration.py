from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from ..config import PROJECT_ROOT
from ..database import Database, utc_now_iso

ENV_PATH = PROJECT_ROOT / ".env"


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    key: str
    label: str
    env_prefix: str
    multiple: bool = True
    base_url_env: str | None = None
    description: str = ""


PROVIDERS: dict[str, ProviderSpec] = {
    "gmgn": ProviderSpec(
        key="gmgn",
        label="GMGN Data",
        env_prefix="GMGN_API_KEY_",
        multiple=True,
        base_url_env="GMGN_API_BASE_URL",
        description="采集、实时行情、K 线与持仓价格。多个 Key 用于轮换和 fallback；默认仍受 IP 级总限速约束。",
    ),
    "jupiter": ProviderSpec(
        key="jupiter",
        label="Jupiter Quote",
        env_prefix="JUPITER_API_KEY_",
        multiple=True,
        base_url_env="PAPER_JUPITER_QUOTE_URL",
        description="模拟盘退出时的 Token→USDC 可执行只读报价。并发上限自动不超过当前 Key 数。",
    ),
    "alchemy": ProviderSpec(
        key="alchemy",
        label="Alchemy Solana RPC",
        env_prefix="ALCHEMY_API_KEY_",
        multiple=True,
        description="Solana 网络状态主 RPC 池。4 个独立 Free 账号轮换/fallback；仅低频网络状态请求，不做全链宽订阅。",
    ),
    "tabpfn": ProviderSpec(
        key="tabpfn",
        label="TabPFN",
        env_prefix="TABPFN_TOKEN",
        multiple=False,
        description="可选的 TabPFN 模型授权 Token；未配置时继续使用本机已缓存/可用模型版本。",
    ),
}


def _read_env(path: Path = ENV_PATH) -> dict[str, str]:
    if not path.exists():
        return {}
    values = dotenv_values(path)
    return {str(key): str(value) for key, value in values.items() if value not in (None, "")}


def _quote_env_value(value: str) -> str:
    if not value:
        return ""
    return json.dumps(value, ensure_ascii=False)


def _atomic_update_env(updates: dict[str, str | None], path: Path = ENV_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []
    managed = set(updates)
    written: set[str] = set()
    output: list[str] = []
    for raw in existing:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or "=" not in raw:
            output.append(raw)
            continue
        key = raw.split("=", 1)[0].strip()
        if key not in managed:
            output.append(raw)
            continue
        if key in written:
            continue
        written.add(key)
        value = updates[key]
        if value is not None:
            output.append(f"{key}={_quote_env_value(str(value))}")
    for key, value in updates.items():
        if key not in written and value is not None:
            output.append(f"{key}={_quote_env_value(str(value))}")
    payload = "\n".join(output).rstrip() + "\n"
    fd, temp_name = tempfile.mkstemp(prefix=".env.", dir=str(path.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def read_provider_credentials(provider: str, path: Path = ENV_PATH) -> tuple[str, ...]:
    spec = PROVIDERS[provider]
    env = _read_env(path)
    if not spec.multiple:
        value = env.get(spec.env_prefix, "").strip()
        return (value,) if value else ()
    pattern = re.compile(rf"^{re.escape(spec.env_prefix)}(\d+)$")
    values: list[tuple[int, str]] = []
    for key, value in env.items():
        match = pattern.match(key)
        if match and value.strip():
            values.append((int(match.group(1)), value.strip()))
    values.sort(key=lambda item: item[0])
    return tuple(value for _, value in values)


def read_provider_base_url(provider: str, path: Path = ENV_PATH) -> str | None:
    spec = PROVIDERS[provider]
    if spec.base_url_env is None:
        return None
    return _read_env(path).get(spec.base_url_env) or None


class PlatformConfigurationService:
    def __init__(self, database: Database, *, env_path: Path | None = None) -> None:
        self.database = database
        self.env_path = env_path or ENV_PATH

    @staticmethod
    def _bounded_float(raw: Any, *, default: float, minimum: float, maximum: float) -> float:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return default
        return min(maximum, max(minimum, value))

    def runtime_values(self) -> dict[str, Any]:
        env = _read_env(self.env_path)
        return {
            "position_monitor_poll_seconds": self._bounded_float(
                env.get("POSITION_MONITOR_POLL_SECONDS"), default=3.0, minimum=1.0, maximum=60.0
            ),
            "regime_poll_seconds": int(self._bounded_float(
                env.get("REGIME_POLL_SECONDS"), default=60.0, minimum=15.0, maximum=3600.0
            )),
            "adaptive_action_interval_minutes": int(self._bounded_float(
                env.get("ADAPTIVE_ACTION_INTERVAL_MINUTES"), default=15.0, minimum=5.0, maximum=60.0
            )),
            "adaptive_min_confidence": self._bounded_float(
                env.get("ADAPTIVE_MIN_CONFIDENCE"), default=0.55, minimum=0.0, maximum=1.0
            ),
            "adaptive_exploration_rate": self._bounded_float(
                env.get("ADAPTIVE_EXPLORATION_RATE"), default=0.0, minimum=0.0, maximum=0.05
            ),
            "gmgn_global_rps": self._bounded_float(
                env.get("GMGN_GLOBAL_RPS"), default=10.0, minimum=0.1, maximum=50.0
            ),
        }

    def provider_credentials(self, provider: str) -> tuple[str, ...]:
        if provider not in PROVIDERS:
            raise ValueError(f"unsupported provider: {provider}")
        return read_provider_credentials(provider, self.env_path)

    @staticmethod
    def _mask(value: str) -> str:
        if len(value) <= 8:
            return "********"
        return f"******{value[-4:]}"

    def configuration(self) -> dict[str, Any]:
        runtime = self.runtime_values()
        providers = []
        for key, spec in PROVIDERS.items():
            credentials = self.provider_credentials(key)
            providers.append(
                {
                    "key": key,
                    "label": spec.label,
                    "description": spec.description,
                    "multiple": spec.multiple,
                    "credential_count": len(credentials),
                    "credentials": [
                        {"slot": index + 1, "masked": self._mask(value)}
                        for index, value in enumerate(credentials)
                    ],
                    "base_url": read_provider_base_url(key, self.env_path),
                }
            )
        gmgn_count = len(self.provider_credentials("gmgn"))
        jupiter_count = len(self.provider_credentials("jupiter"))
        alchemy_count = len(self.provider_credentials("alchemy"))
        row = self.database.fetch_one(
            "SELECT COUNT(DISTINCT token_address) AS count FROM positions WHERE status IN ('open','closing')"
        ) or {}
        open_unique_tokens = int(row.get("count") or 0)
        isolated_market_seconds = open_unique_tokens / runtime["gmgn_global_rps"] if open_unique_tokens else 0.0
        return {
            "runtime": runtime,
            "providers": providers,
            "derived": {
                "gmgn_key_count": gmgn_count,
                "jupiter_key_count": jupiter_count,
                "alchemy_account_count": alchemy_count,
                "rpc_fallback_order": "Alchemy accounts -> Solana public emergency",
                "gmgn_total_rps": runtime["gmgn_global_rps"],
                "jupiter_exit_concurrency": max(1, min(8, jupiter_count or 1)),
                "position_monitor_target_seconds": runtime["position_monitor_poll_seconds"],
                "gmgn_unique_tokens_per_target_cycle": max(
                    1, int(runtime["gmgn_global_rps"] * runtime["position_monitor_poll_seconds"])
                ),
                "open_unique_tokens": open_unique_tokens,
                "estimated_min_cycle_seconds": max(runtime["position_monitor_poll_seconds"], isolated_market_seconds),
                "capacity_state": (
                    "within_target"
                    if isolated_market_seconds <= runtime["position_monitor_poll_seconds"]
                    else "budget_limited"
                ),
                "policy": "GMGN keys rotate/fallback under one global RPS budget; Jupiter exit concurrency <= configured key count",
            },
        }

    def save_runtime(
        self,
        *,
        position_monitor_poll_seconds: float,
        gmgn_global_rps: float,
        regime_poll_seconds: int = 60,
        adaptive_action_interval_minutes: int = 15,
        adaptive_min_confidence: float = 0.55,
        adaptive_exploration_rate: float = 0.0,
        gmgn_base_url: str | None = None,
        jupiter_quote_url: str | None = None,
    ) -> dict[str, Any]:
        poll = self._bounded_float(position_monitor_poll_seconds, default=3.0, minimum=1.0, maximum=60.0)
        rps = self._bounded_float(gmgn_global_rps, default=10.0, minimum=0.1, maximum=50.0)
        regime_poll = int(self._bounded_float(regime_poll_seconds, default=60.0, minimum=15.0, maximum=3600.0))
        action_interval = int(self._bounded_float(adaptive_action_interval_minutes, default=15.0, minimum=5.0, maximum=60.0))
        min_confidence = self._bounded_float(adaptive_min_confidence, default=0.55, minimum=0.0, maximum=1.0)
        exploration_rate = self._bounded_float(adaptive_exploration_rate, default=0.0, minimum=0.0, maximum=0.05)
        updates: dict[str, str | None] = {
            "POSITION_MONITOR_POLL_SECONDS": f"{poll:g}",
            "GMGN_GLOBAL_RPS": f"{rps:g}",
            "REGIME_POLL_SECONDS": str(regime_poll),
            "ADAPTIVE_ACTION_INTERVAL_MINUTES": str(action_interval),
            "ADAPTIVE_MIN_CONFIDENCE": f"{min_confidence:g}",
            "ADAPTIVE_EXPLORATION_RATE": f"{exploration_rate:g}",
        }
        if gmgn_base_url is not None:
            updates["GMGN_API_BASE_URL"] = gmgn_base_url.strip() or None
        if jupiter_quote_url is not None:
            updates["PAPER_JUPITER_QUOTE_URL"] = jupiter_quote_url.strip() or None
        _atomic_update_env(updates, self.env_path)
        self._audit("runtime_saved", {"position_monitor_poll_seconds": poll, "gmgn_global_rps": rps})
        return self.configuration()

    def add_credential(self, provider: str, value: str) -> dict[str, Any]:
        if provider not in PROVIDERS:
            raise ValueError(f"unsupported provider: {provider}")
        value = value.strip()
        if not value:
            raise ValueError("credential must not be empty")
        spec = PROVIDERS[provider]
        existing = list(self.provider_credentials(provider))
        if value in existing:
            raise ValueError("credential already exists")
        if spec.multiple:
            existing.append(value)
            updates = self._numbered_updates(spec, existing)
        else:
            updates = {spec.env_prefix: value}
        _atomic_update_env(updates, self.env_path)
        self._audit("credential_added", {"provider": provider, "credential_count": len(existing) if spec.multiple else 1})
        return self.configuration()

    def delete_credential(self, provider: str, slot: int) -> dict[str, Any]:
        if provider not in PROVIDERS:
            raise ValueError(f"unsupported provider: {provider}")
        spec = PROVIDERS[provider]
        existing = list(self.provider_credentials(provider))
        if slot < 1 or slot > len(existing):
            raise ValueError("credential slot does not exist")
        existing.pop(slot - 1)
        if spec.multiple:
            updates = self._numbered_updates(spec, existing)
        else:
            updates = {spec.env_prefix: None}
        _atomic_update_env(updates, self.env_path)
        self._audit("credential_deleted", {"provider": provider, "credential_count": len(existing)})
        return self.configuration()

    def _numbered_updates(self, spec: ProviderSpec, values: list[str]) -> dict[str, str | None]:
        env = _read_env(self.env_path)
        pattern = re.compile(rf"^{re.escape(spec.env_prefix)}(\d+)$")
        existing_indices = [
            int(match.group(1))
            for key in env
            for match in [pattern.match(key)]
            if match
        ]
        maximum = max(existing_indices + [len(values)], default=0)
        return {
            f"{spec.env_prefix}{index}": values[index - 1] if index <= len(values) else None
            for index in range(1, maximum + 1)
        }

    def _audit(self, action: str, details: dict[str, Any]) -> None:
        self.database.audit(
            category="configuration",
            action=action,
            severity="info",
            entity_type="platform_configuration",
            details={**details, "updated_at": utc_now_iso()},
        )
