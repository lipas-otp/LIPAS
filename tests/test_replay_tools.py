"""B2 — ToolReplayer / replay-tools tests (P3.2, RFC-001).

Scope:
  - STRICT_TAPE substitute path (output verbatim, full effect triple
    mirrored, tool_calls charged but wall_seconds not).
  - STRICT_TAPE miss → ReplayMissing + 'fail' decision claim, no
    intent/result/spend folded.
  - session_init preamble: logged exactly once, before the first
    per-call decision.
  - HistoryRow ownership of TAG_REPLAY_DECISION + projection counts.
  - Live store isolation: replay never mutates the recording store.

Out of scope here (see TODO at the bottom of this file):
  - 're-execute' and 'refuse' operations, and non-STRICT modes — the
    matrix gating these requires sight of replay_tools.py.
"""
from __future__ import annotations

import pytest
import pytest_asyncio

from lipas.replay_tools import (
    ReplayMissing,
    ReplayMode,
    TAG_REPLAY_DECISION,
    ToolReplayer,
)
from lipas.rows.capability import CapabilityRow
from lipas.rows.effect import EffectRow
from lipas.rows.history import HistoryRow
from lipas.tool_harness import ToolHarness


pytestmark = pytest.mark.asyncio


# ── helpers ───────────────────────────────────────────────────────────

def effect_view(rowset):
    eff = next(r for r in rowset.rows if isinstance(r, EffectRow))
    return eff.project(rowset.store)


def decisions(store):
    return [c for c in store if c.tag == TAG_REPLAY_DECISION]


def operations(store):
    return [c.fields.get("operation") for c in decisions(store)]


def tag_counts(store) -> dict[str, int]:
    out: dict[str, int] = {}
    for c in store:
        out[c.tag] = out.get(c.tag, 0) + 1
    return out


# ── recording fixture ────────────────────────────────────────────────
#
# Two PURE live calls.  Returned tuple is (rowset, out_add, out_multiply)
# so individual tests can compare bytes against the replay run.

@pytest_asyncio.fixture
async def recorded(tools, fresh_rowset):
    rs = fresh_rowset()
    harness = ToolHarness(tools=tools, rowset=rs)
    out_a = await harness.call(
        tool_name="add",      arguments={"a": 12, "b": 7},
    )
    out_b = await harness.call(
        tool_name="multiply", arguments={"a": 19, "b": 3},
    )
    return rs, out_a, out_b


def make_replay_harness(tools, rs_live, fresh_rowset):
    """Build (rs_replay, harness) ready to consume the recorded view."""
    replayer = ToolReplayer(
        view=effect_view(rs_live),
        mode=ReplayMode.STRICT_TAPE,
    )
    rs_replay = fresh_rowset()
    harness = ToolHarness(
        tools=tools, rowset=rs_replay, tool_replayer=replayer,
    )
    return rs_replay, harness


# ── substitute path ──────────────────────────────────────────────────

class TestSubstitute:

    async def test_output_byte_equivalent(
        self, tools, fresh_rowset, recorded,
    ):
        rs_live, out_a, out_b = recorded
        rs_replay, h = make_replay_harness(tools, rs_live, fresh_rowset)

        rep_a = await h.call(tool_name="add",      arguments={"a": 12, "b": 7})
        rep_b = await h.call(tool_name="multiply", arguments={"a": 19, "b": 3})

        assert rep_a["content"] == out_a["content"]
        assert rep_b["content"] == out_b["content"]

    async def test_full_effect_triple_mirrored(
        self, tools, fresh_rowset, recorded,
    ):
        """Each substitute folds call_intent + call_result + at
        least one resource_spent (tool_calls bucket)."""
        rs_live, *_ = recorded
        rs_replay, h = make_replay_harness(tools, rs_live, fresh_rowset)

        await h.call(tool_name="add",      arguments={"a": 12, "b": 7})
        await h.call(tool_name="multiply", arguments={"a": 19, "b": 3})

        counts = tag_counts(rs_replay.store)
        assert counts.get("call_intent")  == 2
        assert counts.get("call_result")  == 2
        assert counts.get("resource_spent", 0) >= 2  # tool_calls × 2

    async def test_charges_tool_calls_not_wall_seconds(
        self, tools, fresh_rowset, recorded,
    ):
        """PURE substitute: wall_seconds=0.0 is filtered by the
        amount<=0 guard in _fold_spend (see 06_tool_replay note)."""
        rs_live, *_ = recorded
        rs_replay, h = make_replay_harness(tools, rs_live, fresh_rowset)

        await h.call(tool_name="add",      arguments={"a": 12, "b": 7})
        await h.call(tool_name="multiply", arguments={"a": 19, "b": 3})

        cap = next(r for r in rs_replay.rows if isinstance(r, CapabilityRow))
        proj = cap.project(rs_replay.store)
        assert proj["tool_calls"]["spent"]   == 2.0
        assert proj["wall_seconds"]["spent"] == 0.0

    async def test_decision_per_substituted_call(
        self, tools, fresh_rowset, recorded,
    ):
        rs_live, *_ = recorded
        rs_replay, h = make_replay_harness(tools, rs_live, fresh_rowset)

        await h.call(tool_name="add",      arguments={"a": 12, "b": 7})
        await h.call(tool_name="multiply", arguments={"a": 19, "b": 3})

        assert operations(rs_replay.store).count("substitute") == 2


# ── STRICT_TAPE miss ────────────────────────────────────────────────

class TestStrictTapeMiss:

    async def test_raises_replay_missing(
        self, tools, fresh_rowset, recorded,
    ):
        rs_live, *_ = recorded
        rs_replay, h = make_replay_harness(tools, rs_live, fresh_rowset)

        with pytest.raises(ReplayMissing):
            await h.call(
                tool_name="add", arguments={"a": 999, "b": 999},
            )

    async def test_fail_decision_logged(
        self, tools, fresh_rowset, recorded,
    ):
        rs_live, *_ = recorded
        rs_replay, h = make_replay_harness(tools, rs_live, fresh_rowset)

        with pytest.raises(ReplayMissing):
            await h.call(
                tool_name="add", arguments={"a": 999, "b": 999},
            )

        assert "fail" in operations(rs_replay.store)

    async def test_no_effect_triple_on_miss(
        self, tools, fresh_rowset, recorded,
    ):
        """A miss must not pollute the store with intent/result/spend
        — the call never produced an observed effect."""
        rs_live, *_ = recorded
        rs_replay, h = make_replay_harness(tools, rs_live, fresh_rowset)

        with pytest.raises(ReplayMissing):
            await h.call(
                tool_name="add", arguments={"a": 999, "b": 999},
            )

        counts = tag_counts(rs_replay.store)
        assert counts.get("call_intent",  0) == 0
        assert counts.get("call_result",  0) == 0
        assert counts.get("resource_spent", 0) == 0

    async def test_miss_does_not_consume_remaining_recordings(
        self, tools, fresh_rowset, recorded,
    ):
        """A failed match should not advance the cursor — subsequent
        legitimate calls still substitute correctly."""
        rs_live, out_a, out_b = recorded
        rs_replay, h = make_replay_harness(tools, rs_live, fresh_rowset)

        with pytest.raises(ReplayMissing):
            await h.call(
                tool_name="add", arguments={"a": 999, "b": 999},
            )

        rep_a = await h.call(tool_name="add",      arguments={"a": 12, "b": 7})
        rep_b = await h.call(tool_name="multiply", arguments={"a": 19, "b": 3})
        assert rep_a["content"] == out_a["content"]
        assert rep_b["content"] == out_b["content"]


# ── session_init preamble ───────────────────────────────────────────

class TestSessionInit:

    async def test_logged_before_first_per_call_decision(
        self, tools, fresh_rowset, recorded,
    ):
        rs_live, *_ = recorded
        rs_replay, h = make_replay_harness(tools, rs_live, fresh_rowset)

        await h.call(tool_name="add", arguments={"a": 12, "b": 7})

        ops = operations(rs_replay.store)
        assert ops, "expected at least one decision claim"
        assert ops[0] == "session_init"

    async def test_logged_exactly_once(
        self, tools, fresh_rowset, recorded,
    ):
        rs_live, *_ = recorded
        rs_replay, h = make_replay_harness(tools, rs_live, fresh_rowset)

        await h.call(tool_name="add",      arguments={"a": 12, "b": 7})
        await h.call(tool_name="multiply", arguments={"a": 19, "b": 3})

        assert operations(rs_replay.store).count("session_init") == 1

    async def test_present_even_when_first_call_misses(
        self, tools, fresh_rowset, recorded,
    ):
        """Even if the very first call fails to match, the session
        preamble should still be folded — its purpose is auditing the
        replay session itself, not any specific match."""
        rs_live, *_ = recorded
        rs_replay, h = make_replay_harness(tools, rs_live, fresh_rowset)

        with pytest.raises(ReplayMissing):
            await h.call(
                tool_name="add", arguments={"a": 999, "b": 999},
            )

        assert "session_init" in operations(rs_replay.store)


# ── HistoryRow ownership / projection ───────────────────────────────

class TestHistoryRowIntegration:

    def test_namespace_owns_replay_decision_tag(self):
        assert TAG_REPLAY_DECISION in HistoryRow().namespace

    async def test_decisions_visible_in_projection(
        self, tools, fresh_rowset, recorded,
    ):
        """event_count should include the decision claims (1 init + 2
        substitute = 3 owned events at minimum)."""
        rs_live, *_ = recorded
        rs_replay, h = make_replay_harness(tools, rs_live, fresh_rowset)

        await h.call(tool_name="add",      arguments={"a": 12, "b": 7})
        await h.call(tool_name="multiply", arguments={"a": 19, "b": 3})

        hist = next(r for r in rs_replay.rows if isinstance(r, HistoryRow))
        proj = hist.project(rs_replay.store)
        assert proj["event_count"] >= 3

    async def test_decision_claims_carry_operation_field(
        self, tools, fresh_rowset, recorded,
    ):
        rs_live, *_ = recorded
        rs_replay, h = make_replay_harness(tools, rs_live, fresh_rowset)
        await h.call(tool_name="add", arguments={"a": 12, "b": 7})

        for c in decisions(rs_replay.store):
            assert "operation" in c.fields, \
                f"decision claim missing 'operation': {c.fields}"
            assert isinstance(c.fields["operation"], str)


# ── live-store isolation ────────────────────────────────────────────

class TestStoreIsolation:

    async def test_replay_does_not_mutate_live_store(
        self, tools, fresh_rowset, recorded,
    ):
        rs_live, *_ = recorded
        snapshot = len(rs_live.store)

        rs_replay, h = make_replay_harness(tools, rs_live, fresh_rowset)
        await h.call(tool_name="add",      arguments={"a": 12, "b": 7})
        await h.call(tool_name="multiply", arguments={"a": 19, "b": 3})
        with pytest.raises(ReplayMissing):
            await h.call(tool_name="add", arguments={"a": 1, "b": 1})

        assert len(rs_live.store) == snapshot, \
            "replay must never fold into the recording store"


# ─────────────────────────────────────────────────────────────────────

import warnings as _warnings

from lipas.replay_tools import (
    F_DECISION_DECLARED_CLASS,
    F_DECISION_EFFECTIVE_CLASS,
    F_DECISION_FROZEN_MAX_SEQ,
    F_DECISION_MODE,
    F_DECISION_OPERATION,
    F_DECISION_REASON,
    F_DECISION_SESSION_INIT,
    F_DECISION_SOURCE_EFFECT_ID,
    F_DECISION_TARGET_EFFECT_ID,
    LipasDangerousReplayWarning,
    LipasIdempotencyKeyMissingWarning,
    LipasReplayClassDowngradeError,
    LipasReplayClassDowngradeWarning,
    LipasReplayClassUpgradeWarning,
    ReplayConfigError,
    ReplayDecision,
)
from lipas.tools import SideEffectClass


# ── helpers for direct-decide tests ──────────────────────────────────

@pytest_asyncio.fixture
async def recorded_all_classes(all_tools, fresh_rowset):
    """Recording with one entry per non-PURE class, plus one PURE."""
    rs = fresh_rowset()
    h = ToolHarness(tools=all_tools, rowset=rs)
    await h.call(tool_name="add",          arguments={"a": 1, "b": 2})
    await h.call(tool_name="read_thing",   arguments={"q": "Q1"})
    await h.call(tool_name="upsert_thing", arguments={"key": "k", "value": "v"})
    await h.call(tool_name="send_thing",   arguments={"target": "t", "payload": "p"})
    return rs


def replayer_for(rs_live, mode, **kw):
    return ToolReplayer(view=effect_view(rs_live), mode=mode, **kw)


def get_tool(registry, name):
    return registry.get(name)  # adjust if your ToolRegistry uses a different accessor


# ── matrix · present branch ──────────────────────────────────────────

class TestDecidePresent:
    """RFC-001 §4: when a recording exists for (tool, args)."""

    @pytest.mark.parametrize("class_name,tool_name,args", [
        ("PURE",             "add",          {"a": 1, "b": 2}),
        ("READ_ONLY",        "read_thing",   {"q": "Q1"}),
        ("IDEMPOTENT_WRITE", "upsert_thing", {"key": "k", "value": "v"}),
        ("EXTERNAL_WRITE",   "send_thing",   {"target": "t", "payload": "p"}),
    ])
    async def test_strict_tape_substitutes_every_class(
        self, all_tools, recorded_all_classes, class_name, tool_name, args,
    ):
        r = replayer_for(recorded_all_classes, ReplayMode.STRICT_TAPE)
        d = r.decide(get_tool(all_tools, tool_name), args)
        assert d.operation == "substitute"
        assert d.declared_class.name == class_name
        assert d.recorded_node is not None

    @pytest.mark.parametrize("tool_name,args", [
        ("add",          {"a": 1, "b": 2}),
        ("read_thing",   {"q": "Q1"}),
        ("upsert_thing", {"key": "k", "value": "v"}),
        ("send_thing",   {"target": "t", "payload": "p"}),
    ])
    async def test_best_effort_substitutes_every_class(
        self, all_tools, recorded_all_classes, tool_name, args,
    ):
        r = replayer_for(recorded_all_classes, ReplayMode.BEST_EFFORT)
        d = r.decide(get_tool(all_tools, tool_name), args)
        assert d.operation == "substitute"

    async def test_live_reroute_pure_re_executes(
        self, all_tools, recorded_all_classes,
    ):
        r = replayer_for(recorded_all_classes, ReplayMode.LIVE_REROUTE)
        d = r.decide(get_tool(all_tools, "add"), {"a": 1, "b": 2})
        assert d.operation == "re-execute"
        assert d.recorded_node is not None  # match was found, just not used

    async def test_live_reroute_read_only_re_executes(
        self, all_tools, recorded_all_classes,
    ):
        r = replayer_for(recorded_all_classes, ReplayMode.LIVE_REROUTE)
        d = r.decide(get_tool(all_tools, "read_thing"), {"q": "Q1"})
        assert d.operation == "re-execute"

    async def test_live_reroute_idempotent_downgrades_to_substitute(
        self, all_tools, recorded_all_classes,
    ):
        r = replayer_for(recorded_all_classes, ReplayMode.LIVE_REROUTE)
        with _warnings.catch_warnings(record=True) as caught:
            _warnings.simplefilter("always")
            d = r.decide(
                get_tool(all_tools, "upsert_thing"),
                {"key": "k", "value": "v"},
            )
        assert d.operation == "substitute"
        assert d.reason.endswith("idem_no_key")
        assert any(
            issubclass(w.category, LipasIdempotencyKeyMissingWarning)
            for w in caught
        )

    async def test_live_reroute_external_write_refuses_by_default(
        self, all_tools, recorded_all_classes,
    ):
        r = replayer_for(recorded_all_classes, ReplayMode.LIVE_REROUTE)
        d = r.decide(
            get_tool(all_tools, "send_thing"),
            {"target": "t", "payload": "p"},
        )
        assert d.operation == "refuse"
        assert d.reason == "replay:refused_external"

    async def test_live_reroute_external_write_with_optin_re_executes(
        self, all_tools, recorded_all_classes,
    ):
        with _warnings.catch_warnings():
            _warnings.simplefilter("ignore", LipasDangerousReplayWarning)
            r = replayer_for(
                recorded_all_classes,
                ReplayMode.LIVE_REROUTE,
                allow_external_write=True,
            )
        with _warnings.catch_warnings(record=True) as caught:
            _warnings.simplefilter("always")
            d = r.decide(
                get_tool(all_tools, "send_thing"),
                {"target": "t", "payload": "p"},
            )
        assert d.operation == "re-execute"
        assert any(
            issubclass(w.category, LipasDangerousReplayWarning)
            for w in caught
        )


# ── matrix · absent branch ───────────────────────────────────────────

class TestDecideAbsent:
    """RFC-001 §4: no recording matches (tool, args)."""

    async def test_strict_tape_fails_on_any_class(
        self, all_tools, recorded_all_classes,
    ):
        r = replayer_for(recorded_all_classes, ReplayMode.STRICT_TAPE)
        for name, args in [
            ("add",          {"a": 999, "b": 999}),
            ("read_thing",   {"q": "miss"}),
            ("upsert_thing", {"key": "miss", "value": "miss"}),
            ("send_thing",   {"target": "miss", "payload": "miss"}),
        ]:
            d = r.decide(get_tool(all_tools, name), args)
            assert d.operation == "fail"
            assert d.reason == "matrix.absent.strict_tape"

    @pytest.mark.parametrize("mode", [
        ReplayMode.BEST_EFFORT, ReplayMode.LIVE_REROUTE,
    ])
    async def test_pure_and_read_only_re_execute_when_absent(
        self, all_tools, recorded_all_classes, mode,
    ):
        r = replayer_for(recorded_all_classes, mode)
        for name, args in [
            ("add",        {"a": 999, "b": 999}),
            ("read_thing", {"q": "miss"}),
        ]:
            d = r.decide(get_tool(all_tools, name), args)
            assert d.operation == "re-execute"

    @pytest.mark.parametrize("mode", [
        ReplayMode.BEST_EFFORT, ReplayMode.LIVE_REROUTE,
    ])
    async def test_external_write_refuses_when_absent(
        self, all_tools, recorded_all_classes, mode,
    ):
        r = replayer_for(recorded_all_classes, mode)
        d = r.decide(
            get_tool(all_tools, "send_thing"),
            {"target": "miss", "payload": "miss"},
        )
        assert d.operation == "refuse"
        assert d.reason == "replay:refused_external_unrecorded"

    @pytest.mark.parametrize("mode", [
        ReplayMode.BEST_EFFORT, ReplayMode.LIVE_REROUTE,
    ])
    async def test_external_write_with_optin_re_executes_when_absent(
        self, all_tools, recorded_all_classes, mode,
    ):
        with _warnings.catch_warnings():
            _warnings.simplefilter("ignore", LipasDangerousReplayWarning)
            r = replayer_for(
                recorded_all_classes, mode, allow_external_write=True,
            )
        with _warnings.catch_warnings(record=True) as caught:
            _warnings.simplefilter("always")
            d = r.decide(
                get_tool(all_tools, "send_thing"),
                {"target": "miss", "payload": "miss"},
            )
        assert d.operation == "re-execute"
        assert any(
            issubclass(w.category, LipasDangerousReplayWarning)
            for w in caught
        )

    @pytest.mark.parametrize("mode", [
        ReplayMode.BEST_EFFORT, ReplayMode.LIVE_REROUTE,
    ])
    async def test_idempotent_warns_and_re_executes_when_absent(
        self, all_tools, recorded_all_classes, mode,
    ):
        r = replayer_for(recorded_all_classes, mode)
        with _warnings.catch_warnings(record=True) as caught:
            _warnings.simplefilter("always")
            d = r.decide(
                get_tool(all_tools, "upsert_thing"),
                {"key": "miss", "value": "miss"},
            )
        assert d.operation == "re-execute"
        assert any(
            issubclass(w.category, LipasIdempotencyKeyMissingWarning)
            for w in caught
        )


# ── class-mismatch resolution (§5.1) ─────────────────────────────────
#
# We provoke mismatch by recording with one tool, then calling decide
# against a tool that has the SAME name+args but a DIFFERENT declared
# class.  We can't easily redefine a tool mid-test, so we patch the
# tool's side_effect attribute on a copy.  This bends the model
# slightly — declared class is normally fixed by the decorator — but
# it's the surgical way to exercise the lattice logic without forging
# EffectNodes by hand.

class TestClassMismatch:

    def _flip_class(self, tool_obj, new_class):
        """Return a shallow copy of tool with side_effect replaced.

        Adjust this helper if Tool is frozen / dataclass — replace
        with dataclasses.replace, attrs.evolve, or a small subclass."""
        import copy
        clone = copy.copy(tool_obj)
        object.__setattr__(clone, "side_effect", new_class)
        return clone

    async def test_upgrade_warns_and_uses_stricter(
        self, all_tools, recorded_all_classes,
    ):
        """Recorded as PURE, current declares READ_ONLY (stricter)."""
        # `add` was recorded as PURE; flip its current class to READ_ONLY.
        flipped = self._flip_class(
            get_tool(all_tools, "add"), SideEffectClass.READ_ONLY,
        )
        r = replayer_for(recorded_all_classes, ReplayMode.BEST_EFFORT)
        with _warnings.catch_warnings(record=True) as caught:
            _warnings.simplefilter("always")
            d = r.decide(flipped, {"a": 1, "b": 2})
        assert d.effective_class == SideEffectClass.READ_ONLY
        assert any(
            issubclass(w.category, LipasReplayClassUpgradeWarning)
            for w in caught
        )

    async def test_strict_tape_rejects_downgrade(
        self, all_tools, recorded_all_classes,
    ):
        """Recorded as EXTERNAL_WRITE, current declares PURE."""
        flipped = self._flip_class(
            get_tool(all_tools, "send_thing"), SideEffectClass.PURE,
        )
        r = replayer_for(recorded_all_classes, ReplayMode.STRICT_TAPE)
        with pytest.raises(LipasReplayClassDowngradeError):
            r.decide(flipped, {"target": "t", "payload": "p"})

    async def test_non_strict_downgrade_requires_optin(
        self, all_tools, recorded_all_classes,
    ):
        flipped = self._flip_class(
            get_tool(all_tools, "send_thing"), SideEffectClass.PURE,
        )
        r = replayer_for(recorded_all_classes, ReplayMode.BEST_EFFORT)
        with pytest.raises(LipasReplayClassDowngradeError):
            r.decide(flipped, {"target": "t", "payload": "p"})

    async def test_non_strict_downgrade_with_optin_warns(
        self, all_tools, recorded_all_classes,
    ):
        flipped = self._flip_class(
            get_tool(all_tools, "send_thing"), SideEffectClass.PURE,
        )
        r = replayer_for(
            recorded_all_classes,
            ReplayMode.BEST_EFFORT,
            allow_class_downgrade=True,
        )
        with _warnings.catch_warnings(record=True) as caught:
            _warnings.simplefilter("always")
            d = r.decide(flipped, {"target": "t", "payload": "p"})
        # stricter wins → recorded (EXTERNAL_WRITE)
        assert d.effective_class == SideEffectClass.EXTERNAL_WRITE
        assert any(
            issubclass(w.category, LipasReplayClassDowngradeWarning)
            for w in caught
        )


# ── observability-only downgrade (§3.4) ─────────────────────────────

class TestObservabilityOnly:

    async def test_external_write_with_obs_only_treated_as_read_only(
        self, all_tools, recorded_all_classes,
    ):
        tool_obj = get_tool(all_tools, "send_thing")
        object.__setattr__(tool_obj, "observability_only", True)
        try:
            r = replayer_for(recorded_all_classes, ReplayMode.LIVE_REROUTE)
            d = r.decide(tool_obj, {"target": "t", "payload": "p"})
            # Effective class drops to READ_ONLY → re-execute path,
            # no refuse, no idempotency-warning.
            assert d.declared_class  == SideEffectClass.EXTERNAL_WRITE
            assert d.effective_class == SideEffectClass.READ_ONLY
            assert d.operation == "re-execute"
        finally:
            object.__setattr__(tool_obj, "observability_only", False)


# ── frozen window (§visibility) ─────────────────────────────────────

class TestFrozenWindow:

    async def test_explicit_low_cap_hides_all_recordings(
        self, all_tools, recorded_all_classes,
    ):
        # cap = -1 hides everything; STRICT_TAPE then turns every call
        # into a 'fail' rather than a 'substitute'.
        r = ToolReplayer(
            view=effect_view(recorded_all_classes),
            mode=ReplayMode.STRICT_TAPE,
            frozen_max_seq=-1,
        )
        d = r.decide(get_tool(all_tools, "add"), {"a": 1, "b": 2})
        assert d.operation == "fail"

    async def test_construction_captures_view_max_seq(
        self, all_tools, recorded_all_classes,
    ):
        r = replayer_for(recorded_all_classes, ReplayMode.STRICT_TAPE)
        assert isinstance(r.frozen_max_seq, int)
        assert r.frozen_max_seq >= 0

    async def test_post_construction_recordings_invisible(
        self, all_tools, fresh_rowset,
    ):
        """A call recorded AFTER the replayer is constructed must
        not be visible — that is the whole point of frozen_max_seq."""
        rs = fresh_rowset()
        h = ToolHarness(tools=all_tools, rowset=rs)
        await h.call(tool_name="add", arguments={"a": 1, "b": 2})

        r = replayer_for(rs, ReplayMode.STRICT_TAPE)  # captures cap here
        # add another recording AFTER
        await h.call(tool_name="add", arguments={"a": 5, "b": 6})

        d_before = r.decide(get_tool(all_tools, "add"), {"a": 1, "b": 2})
        d_after  = r.decide(get_tool(all_tools, "add"), {"a": 5, "b": 6})
        assert d_before.operation == "substitute"
        assert d_after.operation  == "fail"


# ── construction-time validation ────────────────────────────────────

class TestConstruction:

    def test_strict_tape_requires_finite_seq(self, fresh_rowset):
        rs = fresh_rowset()  # empty
        with pytest.raises(ReplayConfigError):
            ToolReplayer(
                view=effect_view(rs),
                mode=ReplayMode.STRICT_TAPE,
                frozen_max_seq=None,
            )

    def test_strict_tape_empty_view_raises(self, fresh_rowset):
        """Auto-capture on an empty view yields None → error."""
        rs = fresh_rowset()
        with pytest.raises(ReplayConfigError):
            ToolReplayer(view=effect_view(rs), mode=ReplayMode.STRICT_TAPE)

    def test_best_effort_allows_none_cap(self, fresh_rowset):
        rs = fresh_rowset()
        r = ToolReplayer(
            view=effect_view(rs),
            mode=ReplayMode.BEST_EFFORT,
            frozen_max_seq=None,
        )
        assert r.frozen_max_seq is None

    async def test_live_reroute_with_external_optin_warns_at_construction(
        self, all_tools, recorded_all_classes,
    ):
        with _warnings.catch_warnings(record=True) as caught:
            _warnings.simplefilter("always")
            ToolReplayer(
                view=effect_view(recorded_all_classes),
                mode=ReplayMode.LIVE_REROUTE,
                allow_external_write=True,
                frozen_max_seq=None,
            )
        assert any(
            issubclass(w.category, LipasDangerousReplayWarning)
            for w in caught
        )


# ── claim-shape contracts (§6.3) ─────────────────────────────────────

class TestClaimShape:

    async def test_session_init_claim_fields(
        self, all_tools, recorded_all_classes,
    ):
        r = replayer_for(recorded_all_classes, ReplayMode.STRICT_TAPE)
        c = r.session_init_claim()
        assert c.tag == TAG_REPLAY_DECISION
        assert c.fields[F_DECISION_SESSION_INIT] is True
        assert c.fields[F_DECISION_OPERATION]    == "session_init"
        assert c.fields[F_DECISION_MODE]         == "strict_tape"
        assert c.fields[F_DECISION_FROZEN_MAX_SEQ] == r.frozen_max_seq

    async def test_decision_claim_carries_target_and_source_eids(
        self, all_tools, recorded_all_classes,
    ):
        r = replayer_for(recorded_all_classes, ReplayMode.STRICT_TAPE)
        d = r.decide(get_tool(all_tools, "add"), {"a": 1, "b": 2})
        c = r.decision_claim(d, target_effect_id="target-eid-xyz")

        assert c.fields[F_DECISION_SESSION_INIT]    is False
        assert c.fields[F_DECISION_OPERATION]       == "substitute"
        assert c.fields[F_DECISION_TARGET_EFFECT_ID] == "target-eid-xyz"
        # source_eid should be the recorded effect's eid, non-None
        assert c.fields[F_DECISION_SOURCE_EFFECT_ID] is not None
        assert c.fields[F_DECISION_DECLARED_CLASS]  == "pure"
        assert c.fields[F_DECISION_EFFECTIVE_CLASS] == "pure"

    async def test_decision_claim_fail_has_no_source_eid(
        self, all_tools, recorded_all_classes,
    ):
        r = replayer_for(recorded_all_classes, ReplayMode.STRICT_TAPE)
        d = r.decide(get_tool(all_tools, "add"), {"a": 999, "b": 999})
        c = r.decision_claim(d, target_effect_id="t-1")
        assert c.fields[F_DECISION_OPERATION] == "fail"
        assert c.fields[F_DECISION_SOURCE_EFFECT_ID] is None


# ── lookup() contract ───────────────────────────────────────────────

class TestLookup:

    async def test_exact_args_match(self, all_tools, recorded_all_classes):
        r = replayer_for(recorded_all_classes, ReplayMode.STRICT_TAPE)
        node = r.lookup(get_tool(all_tools, "add"), {"a": 1, "b": 2})
        assert node is not None

    async def test_arg_order_does_not_affect_match(
        self, all_tools, recorded_all_classes,
    ):
        """Mappings compare by content, not insertion order."""
        r = replayer_for(recorded_all_classes, ReplayMode.STRICT_TAPE)
        node = r.lookup(get_tool(all_tools, "add"), {"b": 2, "a": 1})
        assert node is not None

    async def test_arg_value_diff_misses(
        self, all_tools, recorded_all_classes,
    ):
        r = replayer_for(recorded_all_classes, ReplayMode.STRICT_TAPE)
        assert r.lookup(get_tool(all_tools, "add"), {"a": 1, "b": 3}) is None

    async def test_tool_name_diff_misses(
        self, all_tools, recorded_all_classes,
    ):
        r = replayer_for(recorded_all_classes, ReplayMode.STRICT_TAPE)
        # `multiply` was never recorded
        assert r.lookup(
            get_tool(all_tools, "multiply"), {"a": 1, "b": 2},
        ) is None
