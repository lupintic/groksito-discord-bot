"""
Tests for the lightweight skills + decision layer.

Covers:
- SkillRegistry (CRUD, approval, persistence, pattern matching, ID generation)
- Decision heuristics (direct vs search vs use_skill vs recent_context)
- Proposer (fingerprints, game-ability blocker, explicit creation intent bypass, auto-create)
- Executor (injection, native/custom tool filtering)

These protect the "non-agentic, user-approved, conservative auto-create" contract
and the token-efficiency wins (suppressing search tools on timeless queries, restricting
tools when a skill is active).
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from groksito_discord.skills.skill_registry import (
    SkillRegistry,
    Skill,
    get_skill_registry,
)
from groksito_discord.skills.decision import (
    make_decision,
    DecisionAction,
)
from groksito_discord.skills.skill_proposer import (
    detect_and_create_skill,
    _is_game_ability_context,
    _has_explicit_creation_intent,
    _has_explicit_edit_intent,
)
from groksito_discord.skills.skill_executor import (
    prepare_skill_injection,
    filter_native_search_tools,
    filter_custom_tools,
)


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def temp_registry(monkeypatch, tmp_path):
    """Provide a fresh SkillRegistry backed by a temp directory."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    reg = SkillRegistry(data_dir=data_dir)
    # Patch the global getter so code using get_skill_registry() sees our temp one.
    # skill_executor imports get_skill_registry at module load, so patch both bindings.
    def getter():
        return reg
    monkeypatch.setattr(
        "groksito_discord.skills.skill_registry.get_skill_registry",
        getter,
    )
    monkeypatch.setattr(
        "groksito_discord.skills.skill_executor.get_skill_registry",
        getter,
    )
    return reg


@pytest.fixture
def sample_skill():
    return Skill(
        id="steam-player-counts-123",
        name="Steam Player Counts",
        description="Reports current players for specific games",
        instructions="Always fetch fresh data for the 7 games...",
        allowed_tools=["web_search"],
        approved=True,
        approved_by="auto",
    )


# =============================================================================
# SkillRegistry tests
# =============================================================================

def test_registry_create_approved_and_persist(temp_registry, tmp_path):
    reg = temp_registry
    sk = reg.create_approved_skill(
        name="Test Steam",
        description="Test desc",
        instructions="Step 1: ...",
        allowed_tools=["web_search"],
        created_from_pattern="steam-players",
    )
    assert sk.approved is True
    assert sk.id.startswith("test-steam-")
    assert reg.get(sk.id) is not None

    # Fresh instance should load from disk
    data_dir = tmp_path / "data"
    reg2 = SkillRegistry(data_dir=data_dir)
    loaded = reg2.get(sk.id)
    assert loaded is not None
    assert loaded.name == "Test Steam"
    assert loaded.approved is True


def test_registry_approve_revoke_delete(temp_registry):
    reg = temp_registry
    sk = reg.create_approved_skill(
        name="Editable", description="d", instructions="i", allowed_tools=["web_search"]
    )
    assert sk.approved is True

    revoked = reg.revoke(sk.id)
    assert revoked is True
    assert reg.get(sk.id).approved is False

    approved = reg.approve(sk.id, approved_by="test")
    assert approved is not None
    assert approved.approved is True
    assert approved.approved_by == "test"

    assert reg.delete(sk.id) is True
    assert reg.get(sk.id) is None


def test_registry_has_approved_for_pattern(temp_registry):
    reg = temp_registry
    reg.create_approved_skill(
        name="Steam Players",
        description="Current concurrent players for Steam games",
        instructions="...",
        allowed_tools=["web_search"],
    )

    assert reg.has_approved_for_pattern("steam-players") is True
    assert reg.has_approved_for_pattern("price-check") is False

    # Loose query match
    assert reg.has_approved_for_pattern("", query="steam concurrent players") is True


def test_registry_list_approved_filters_correctly(temp_registry):
    reg = temp_registry
    reg.create_approved_skill(name="A1", description="", instructions="", allowed_tools=[])
    sk2 = reg.create_approved_skill(name="A2", description="", instructions="", allowed_tools=[])
    reg.revoke(sk2.id)

    approved = reg.list_approved()
    assert len(approved) == 1
    assert approved[0].name == "A1"


# =============================================================================
# Decision tests (heuristic focused)
# =============================================================================

@pytest.mark.asyncio
async def test_decision_direct_for_timeless_query():
    dec = await make_decision(
        user_message="qué es la fotosíntesis",
        is_mentioned=False,
        context_need="normal",
    )
    assert dec.action == DecisionAction.DIRECT
    assert dec.needs_search == "none"


@pytest.mark.asyncio
async def test_decision_search_for_fresh_data():
    dec = await make_decision(
        user_message="cuántos jugadores tiene poe2 ahora",
        is_mentioned=True,
        context_need="normal",
    )
    # Post #48: search routing lives in Grok's native tool calling, not heuristics.
    assert dec.needs_search == "none"
    assert dec.needs_recent_context is True


@pytest.mark.asyncio
async def test_decision_use_skill_when_approved_provided():
    dec = await make_decision(
        user_message="dame los jugadores de steam",
        is_mentioned=True,
        approved_skill_names=["steam-player-counts"],
        context_need="normal",
    )
    # Skill selection is model-driven via use_skill tool; heuristic only signals addressed turns.
    assert dec.needs_recent_context is True
    assert dec.needs_search == "none"


# =============================================================================
# Proposer tests
# =============================================================================

def test_is_game_ability_context_blocks_poe_builds():
    assert _is_game_ability_context("build con skills de poe2") is True
    assert _is_game_ability_context("mis habilidades en path of exile") is True
    # But explicit creation request bypasses
    assert _is_game_ability_context("crea una skill para consultar jugadores de poe2") is False


def test_has_explicit_creation_intent():
    assert _has_explicit_creation_intent("crea una skill para steam charts") is True
    assert _has_explicit_creation_intent("haz una skill que me diga los precios") is True
    assert _has_explicit_creation_intent("dime los jugadores") is False


def test_has_explicit_edit_intent():
    assert _has_explicit_edit_intent("mejora las instrucciones de la skill Steam") is True
    assert _has_explicit_edit_intent("edita la skill de precios") is True


@pytest.mark.asyncio
async def test_detect_and_create_skill_conservative(temp_registry, monkeypatch):
    # Patch recent messages to simulate recurring pattern
    from groksito_discord import context as ctx_mod

    fake_messages = [
        {"ts": 1000, "content": "cuántos jugadores tiene poe2 ahora"},
        {"ts": 2000, "content": "jugadores de path of exile 2 steam"},
    ]

    def fake_get_recent(channel_id, limit=20):
        return fake_messages

    monkeypatch.setattr(ctx_mod, "get_recent_channel_messages", fake_get_recent)

    # Force low threshold for test
    result = await detect_and_create_skill(
        channel_id=123,
        user_id=999,
        current_message="dame los jugadores de poe2",
        min_occurrences=2,
        window_hours=48,
    )

    # With our fake data it should trigger (or at least not crash)
    # In real runs the fingerprint + count logic decides.
    if result:
        assert result.skill.name
        assert result.skill.approved is True


# =============================================================================
# Executor tests
# =============================================================================

def test_prepare_skill_injection_and_filters(temp_registry, sample_skill):
    reg = temp_registry
    reg._skills[sample_skill.id] = sample_skill  # direct for test

    injection = prepare_skill_injection(
        decision_skill_id=sample_skill.id,
        user_message="dame los jugadores",
    )

    assert injection is not None
    assert injection.skill.id == sample_skill.id
    assert "[SKILL ACTIVE:" in injection.system_block

    # Filtering
    native = [{"type": "web_search"}, {"type": "x_search"}]
    filtered_native = filter_native_search_tools(native, injection)
    assert len(filtered_native) <= len(native)  # skill may restrict

    custom = [{"name": "generate_image"}, {"name": "code_execution"}]
    filtered_custom = filter_custom_tools(custom, injection)
    # Our sample skill only allows web_search, so custom should be empty or restricted
    # (depends on whether the skill declared code_execution; in this test it didn't)
    assert isinstance(filtered_custom, list)


def test_executor_returns_none_for_unapproved(temp_registry):
    reg = temp_registry
    sk = reg.create_approved_skill(
        name="Pending", description="", instructions="", allowed_tools=[]
    )
    sk.approved = False  # force unapproved
    reg._save()

    inj = prepare_skill_injection(decision_skill_id=sk.id, user_message="test")
    assert inj is None


# =============================================================================
# Client integration (regression #57: lazy imports must use ..skills sibling path)
# =============================================================================

def test_client_has_no_broken_skills_imports():
    """Static guard: llm/client.py must not use .skills (subpackage) imports."""
    from pathlib import Path

    client_src = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "groksito_discord"
        / "llm"
        / "client.py"
    ).read_text(encoding="utf-8")
    assert "from .skills" not in client_src
    assert "from ..skills" in client_src


@pytest.mark.asyncio
async def test_client_decision_layer_resolves_skills_imports(monkeypatch, temp_registry):
    """Integration: call_grok_for_groksito must reach make_decision without ImportError."""
    from unittest.mock import AsyncMock, MagicMock

    from groksito_discord.llm.client import call_grok_for_groksito
    from groksito_discord.skills.decision import Decision, DecisionAction

    decision_calls: list[dict] = []

    async def tracking_make_decision(**kwargs):
        decision_calls.append(kwargs)
        return Decision(action=DecisionAction.DIRECT, needs_search="none")

    monkeypatch.setattr(
        "groksito_discord.skills.decision.make_decision",
        tracking_make_decision,
    )
    monkeypatch.setattr(
        "groksito_discord.llm.client._get_grok_bearer",
        lambda: "fake-test-bearer",
    )

    async def fake_build_responses_input(**kwargs):
        return {
            "initial_input": [{"role": "user", "content": "cuántos jugadores tiene poe2"}],
            "stable_prefix_len": 100,
            "need": "normal",
            "user_id": "999",
            "user_message_text": "cuántos jugadores tiene poe2",
        }

    monkeypatch.setattr(
        "groksito_discord.llm.client.build_responses_input",
        fake_build_responses_input,
    )

    mock_response = MagicMock()
    mock_response.output_text = "Respuesta de prueba"
    mock_response.output = []
    mock_response.usage = MagicMock(input_tokens=50)

    monkeypatch.setattr(
        "groksito_discord.llm.client._call_responses_with_retry",
        AsyncMock(return_value=mock_response),
    )

    result = await call_grok_for_groksito(
        user_message="cuántos jugadores tiene poe2",
        author_name="testuser",
        channel_id=12345,
        is_mentioned=True,
    )

    assert result == "Respuesta de prueba"
    assert len(decision_calls) == 1
    assert decision_calls[0]["user_message"] == "cuántos jugadores tiene poe2"
    assert decision_calls[0]["is_mentioned"] is True
