from __future__ import annotations

from pathlib import Path

from backend.app.database import Database
from backend.app.services.platform_configuration import PlatformConfigurationService


def make_database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "configuration.db")
    database.initialize()
    return database


def test_configuration_masks_credentials_and_derives_capacity(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "POSITION_MONITOR_POLL_SECONDS=3",
                "GMGN_GLOBAL_RPS=2",
                "GMGN_API_BASE_URL=https://gmgn.example",
                "PAPER_JUPITER_QUOTE_URL=https://jupiter.example/order",
                "GMGN_API_KEY_1=gmgn-alpha-1111",
                "GMGN_API_KEY_2=gmgn-beta-2222",
                "GMGN_API_KEY_3=gmgn-gamma-3333",
                "JUPITER_API_KEY_1=jupiter-alpha-aaaa",
                "JUPITER_API_KEY_2=jupiter-beta-bbbb",
                "TABPFN_TOKEN=tabpfn-token-cccc",
            ]
        ) + "\n",
        encoding="utf-8",
    )
    service = PlatformConfigurationService(make_database(tmp_path), env_path=env_path)

    payload = service.configuration()

    assert payload["runtime"] == {"position_monitor_poll_seconds": 3.0, "gmgn_global_rps": 2.0}
    assert payload["derived"]["gmgn_key_count"] == 3
    assert payload["derived"]["jupiter_exit_concurrency"] == 2
    assert payload["derived"]["gmgn_unique_tokens_per_target_cycle"] == 6
    serialized = str(payload)
    assert "gmgn-alpha-1111" not in serialized
    assert "jupiter-alpha-aaaa" not in serialized
    assert "tabpfn-token-cccc" not in serialized
    assert "******1111" in serialized


def test_configuration_add_delete_and_runtime_save_are_atomic_and_dynamic(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "# keep this comment\nGMGN_API_KEY_1=first-value-1111\nGMGN_API_KEY_2=second-value-2222\n",
        encoding="utf-8",
    )
    database = make_database(tmp_path)
    service = PlatformConfigurationService(database, env_path=env_path)

    after_add = service.add_credential("gmgn", "third-value-3333")
    assert after_add["derived"]["gmgn_key_count"] == 3
    assert service.provider_credentials("gmgn") == (
        "first-value-1111",
        "second-value-2222",
        "third-value-3333",
    )

    after_delete = service.delete_credential("gmgn", 2)
    assert after_delete["derived"]["gmgn_key_count"] == 2
    assert service.provider_credentials("gmgn") == ("first-value-1111", "third-value-3333")

    saved = service.save_runtime(
        position_monitor_poll_seconds=2.5,
        gmgn_global_rps=1.8,
        gmgn_base_url="https://gmgn.changed",
        jupiter_quote_url="https://jupiter.changed/order",
    )
    assert saved["runtime"]["position_monitor_poll_seconds"] == 2.5
    assert saved["runtime"]["gmgn_global_rps"] == 1.8
    text = env_path.read_text(encoding="utf-8")
    assert "# keep this comment" in text
    assert "GMGN_API_KEY_3" not in text
    assert service.provider_credentials("gmgn") == ("first-value-1111", "third-value-3333")
    assert saved["runtime"]["position_monitor_poll_seconds"] == 2.5
    assert saved["runtime"]["gmgn_global_rps"] == 1.8
