from __future__ import annotations

import pytest

from proofcounsel_ingest.config import ParserConfig, Settings


def test_parser_config_hash_is_stable() -> None:
    settings = Settings(_env_file=None)
    config = ParserConfig.load(settings.parser_config)
    assert config.config_hash() == config.config_hash()
    assert len(config.config_hash()) == 64


def test_capability_report_is_explicit() -> None:
    capabilities = Settings(_env_file=None).capabilities()
    assert "pdftotext" in capabilities
    assert "ocrmypdf" in capabilities
    assert capabilities["pdftotext"] is not None


def test_semantic_configuration_fails_closed_without_a_complete_descriptor() -> None:
    with pytest.raises(ValueError, match="semantic retrieval requires"):
        Settings(_env_file=None, semantic_retrieval_enabled=True)

    settings = Settings(
        _env_file=None,
        semantic_retrieval_enabled=True,
        semantic_index_model="embed-v1",
        semantic_index_dimensions=768,
        semantic_index_version="2026-01",
    )
    assert settings.semantic_index_descriptor() == {
        "model": "embed-v1",
        "dimensions": 768,
        "version": "2026-01",
    }


def test_user_actor_requires_an_explicit_user_id() -> None:
    with pytest.raises(ValueError, match="user actor_role requires user_id"):
        Settings(_env_file=None, actor_role="user")


def test_request_identity_is_not_part_of_parser_capability_fingerprint() -> None:
    service = Settings(_env_file=None)
    user = Settings(
        _env_file=None,
        actor_role="user",
        user_id="00000000-0000-0000-0000-000000000002",
        matter_id="00000000-0000-0000-0000-000000000003",
    )
    assert service.capabilities() == user.capabilities()
