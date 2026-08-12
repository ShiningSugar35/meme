"""Optional gmgn-cli provider.

The CLI reads credentials from its own environment/configuration.  This module
never places an API key or private key in the process argument list.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from .errors import LiveTradeError
from .gmgn import _failure_kind, _status
from .models import (
    ExecutionStep,
    FailureKind,
    OrderSnapshot,
    OrderStatus,
    Quote,
    Submission,
    SwapIntent,
)


@dataclass(frozen=True, slots=True)
class CliResult:
    return_code: int
    data: Mapping[str, Any] = field(default_factory=dict)
    error_message: str = ""


class CliRunner(Protocol):
    async def run(self, arguments: Sequence[str]) -> CliResult: ...


def _redact(text: str) -> str:
    # Defensive only: credentials are never supplied as arguments.
    value = re.sub(
        r"(?i)(GMGN_(?:API_KEY|PRIVATE_KEY)|API[_-]?KEY|PRIVATE[_-]?KEY)\s*[=:]\s*\S+",
        r"\1=<redacted>",
        text,
    )
    return value[:500]


def _parse_json_output(output: str) -> Mapping[str, Any]:
    stripped = output.strip()
    if not stripped:
        return {}
    try:
        value = json.loads(stripped)
        return value if isinstance(value, Mapping) else {"data": value}
    except json.JSONDecodeError:
        # Some CLI launchers print an informational line before raw JSON.
        for line in reversed(stripped.splitlines()):
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            return value if isinstance(value, Mapping) else {"data": value}
    return {}


class SubprocessJsonRunner:
    def __init__(
        self,
        command_prefix: Sequence[str] = ("gmgn-cli",),
        *,
        cwd: Path | str | None = None,
    ) -> None:
        if not command_prefix:
            raise ValueError("command_prefix cannot be empty")
        self.command_prefix = tuple(str(value) for value in command_prefix)
        self.cwd = str(cwd) if cwd is not None else None

    async def run(self, arguments: Sequence[str]) -> CliResult:
        # shell=False is essential: no interpolation and no opportunity for a
        # token address to become a shell expression.
        try:
            process = await asyncio.create_subprocess_exec(
                *self.command_prefix,
                *(str(value) for value in arguments),
                cwd=self.cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()
        except Exception as exc:
            raise LiveTradeError(
                "gmgn-cli process could not be started",
                kind=FailureKind.NETWORK,
            ) from exc
        stdout_text = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace")
        return CliResult(
            process.returncode or 0,
            _parse_json_output(stdout_text),
            _redact(stderr_text),
        )


def _data(value: Mapping[str, Any]) -> Mapping[str, Any]:
    nested = value.get("data")
    return nested if isinstance(nested, Mapping) else value


class GmgnCliProvider:
    name = "gmgn_cli"

    def __init__(
        self,
        runner: CliRunner,
        *,
        allow_live_execution: bool = False,
        anti_mev: bool = True,
    ) -> None:
        self.runner = runner
        self.allow_live_execution = allow_live_execution
        self.anti_mev = anti_mev

    @staticmethod
    def _ensure_success(result: CliResult) -> Mapping[str, Any]:
        mapping = result.data
        code = str(mapping.get("code") or "") or None
        message = str(
            mapping.get("message")
            or mapping.get("error_status")
            or mapping.get("error")
            or result.error_message
            or "gmgn-cli request failed"
        )
        lower = f"{code or ''} {message}".lower()
        if "rate_limit" in lower or "too many" in lower or code == "429":
            reset_raw = mapping.get("reset_at") or mapping.get("resetAt")
            try:
                reset_at = int(float(reset_raw))
            except (TypeError, ValueError):
                reset_at = None
            raise LiveTradeError(
                "gmgn-cli is rate limited; do not submit again before reset",
                kind=FailureKind.RATE_LIMIT,
                code=code or "429",
                reset_at=reset_at,
            )
        if result.return_code != 0 or code not in (None, "0", "success", "SUCCESS"):
            raise LiveTradeError(
                f"gmgn-cli request failed: {_redact(message)}",
                kind=_failure_kind(code, message),
                code=code,
            )
        return _data(mapping)

    async def quote(self, intent: SwapIntent, step: ExecutionStep) -> Quote:
        result = await self.runner.run((
            "order",
            "quote",
            "--chain",
            intent.chain.lower(),
            "--from",
            intent.wallet_address,
            "--input-token",
            intent.input_token,
            "--output-token",
            intent.output_token,
            "--amount",
            intent.input_amount_raw,
            "--slippage",
            str(step.slippage),
            "--raw",
        ))
        data = self._ensure_success(result)
        output = str(data.get("output_amount") or "")
        minimum = str(data.get("min_output_amount") or "")
        if not output or not minimum:
            raise LiveTradeError(
                "gmgn-cli quote did not return a usable route",
                kind=FailureKind.NO_ROUTE,
                code="INCOMPLETE_QUOTE",
            )
        return Quote(
            str(data.get("input_token") or intent.input_token),
            str(data.get("output_token") or intent.output_token),
            str(data.get("input_amount") or intent.input_amount_raw),
            output,
            minimum,
            float(data.get("slippage") if data.get("slippage") is not None else step.slippage),
            data,
        )

    async def submit_swap(
        self,
        intent: SwapIntent,
        quote: Quote,
        step: ExecutionStep,
        *,
        submission_client_order_id: str,
    ) -> Submission:
        if not self.allow_live_execution:
            raise LiveTradeError(
                "gmgn-cli live swap is disabled until the application confirms live mode",
                kind=FailureKind.VALIDATION,
                code="LIVE_EXECUTION_NOT_CONFIRMED",
            )
        arguments: list[str] = [
            "swap",
            "--chain",
            intent.chain.lower(),
            "--from",
            intent.wallet_address,
            "--input-token",
            intent.input_token,
            "--output-token",
            intent.output_token,
            "--amount",
            intent.input_amount_raw,
            "--slippage",
            str(step.slippage),
            "--min-output",
            quote.min_output_amount_raw,
            "--priority-fee",
            str(step.priority_fee_sol),
            "--tip-fee",
            str(step.tip_fee_sol),
        ]
        if self.anti_mev:
            arguments.append("--anti-mev")
        arguments.append("--raw")
        # gmgn-cli currently has no client-order-id flag. Logical idempotency
        # remains enforced by OrderJournal; never smuggle IDs through an
        # unsupported CLI argument.
        result = await self.runner.run(arguments)
        data = self._ensure_success(result)
        order_id = str(data.get("order_id") or "")
        if not order_id:
            raise LiveTradeError(
                "gmgn-cli swap omitted order_id",
                kind=FailureKind.API,
                code="MISSING_ORDER_ID",
            )
        status = _status(data.get("status"))
        if status is OrderStatus.UNKNOWN:
            status = OrderStatus.PENDING
        return Submission(
            order_id,
            status,
            str(data.get("hash") or "") or None,
            data,
        )

    async def get_order(self, order_id: str) -> OrderSnapshot:
        result = await self.runner.run((
            "order",
            "get",
            "--chain",
            "sol",
            "--order-id",
            order_id,
            "--raw",
        ))
        data = self._ensure_success(result)
        status = _status(data.get("status"))
        if status is OrderStatus.UNKNOWN:
            status = OrderStatus.PENDING
        code = str(data.get("error_code") or "") or None
        message = str(data.get("error_status") or data.get("message") or "") or None
        kind = None
        if status is OrderStatus.EXPIRED:
            kind = FailureKind.EXPIRED
        elif status is OrderStatus.FAILED:
            kind = _failure_kind(code, message or "")
            if kind is FailureKind.API:
                kind = FailureKind.CHAIN
        report = data.get("report") if isinstance(data.get("report"), Mapping) else {}
        return OrderSnapshot(
            str(data.get("order_id") or order_id),
            status,
            str(data.get("hash") or "") or None,
            kind,
            code,
            message,
            report,
        )
