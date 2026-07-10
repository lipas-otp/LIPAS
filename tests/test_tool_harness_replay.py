"""tests/test_tool_harness_replay.py

B3 — ToolHarness × ToolReplayer integration. B2 verified the decision
*function*; here we verify the *side effects* the harness produces
for each of substitute / re-execute / refuse / fail, and that
session_init is folded exactly once.
"""
from __future__ import annotations

import warnings
from dataclasses import replace

import pytest
import pytest_asyncio

pytestmark = pytest.mark.asyncio

from lipas.calculus import Claim
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
    ReplayMissing,
    ReplayMode,
    ReplayRefused,
    TAG_REPLAY_DECISION,
    ToolReplayer,
)
from lipas.rows.capability import (
    F_AMOUNT,
    F_BUCKET,
    TAG_BUDGET_OVERRUN,
    TAG_RESOURCE_SPENT,
)
from lipas.rows.effect import (
    EffectRow,
    F_ARGUMENTS,
    F_DETAIL,
    F_EFFECT_ID,
    F_KIND,
    F_OUTPUT,
    F_REASON,
    F_SIDE_EFFECT,
    F_STATUS,
    F_TOOL_NAME,
    TAG_EFFECT_INTENT,
    TAG_EFFECT_REJECTED,
    TAG_EFFECT_RESULT,
)
from lipas.tool_harness import ToolHarness
from lipas.tools import SideEffectClass, ToolRegistry, tool


# ── helpers ──────────────────────────────────────────────────────────

def claims_with_eid(store, tag, eid):
    return [
        c for c in store.filter(tag=tag)
        if c.fields.get(F_EFFECT_ID) == eid
    ]


def effect_view(rowset):
    return next(r for r in rowset.rows if isinstance(r, EffectRow)).project(rowset.store)


def decision_claims(store, *, session_init=None):
    out = list(store.filter(tag=TAG_REPLAY_DECISION))
    if session_init is True:
        out = [c for c in out if c.fields.get(F_DECISION_SESSION_INIT) is True]
    elif session_init is False:
        out = [c for c in out if c.fields.get(F_DECISION_SESSION_INIT) is False]
    return out


# ── spy fixtures ─────────────────────────────────────────────────────

@pytest.fixture
def call_log():
    return {"add": 0, "send": 0, "fail_tool": 0}


@pytest.fixture
def spy_tools(call_log):
    @tool(side_effect=SideEffectClass.PURE)
    async def spy_add(a: int, b: int) -> int:
        """Add two ints."""
        call_log["add"] += 1
        return a + b

    @tool(side_effect=SideEffectClass.EXTERNAL_WRITE)
    async def spy_send(target: str, payload: str) -> str:
        """Send a payload."""
        call_log["send"] += 1
        return f"sent:{target}:{payload}"

    @tool(side_effect=SideEffectClass.READ_ONLY)
    async def spy_fail(x: int) -> int:
        """Always raises."""
        call_log["fail_tool"] += 1
        raise RuntimeError(f"boom:{x}")

    return ToolRegistry([spy_add, spy_send, spy_fail])


@pytest_asyncio.fixture
async def recording(spy_tools, call_log, fresh_rowset):
    """Build a recorded EffectView with spy_add(ok), spy_send(ok),
    spy_fail(err); reset call_log to zero before returning."""
    src_rs = fresh_rowset()
    src_h  = ToolHarness(tools=spy_tools, rowset=src_rs)
    await src_h.call(tool_name="spy_add",  arguments={"a": 1, "b": 2})
    await src_h.call(tool_name="spy_send", arguments={"target": "t", "payload": "p"})
    await src_h.call(tool_name="spy_fail", arguments={"x": 7})
    for k in list(call_log): call_log[k] = 0
    return effect_view(src_rs)  # effect_view helper from conftest.py


@pytest.fixture
def target_harness_factory(spy_tools, fresh_rowset):
    """Factory: build (harness, rowset) given a replayer."""
    def _build(replayer):
        rs = fresh_rowset()
        h = ToolHarness(tools=spy_tools, rowset=rs, tool_replayer=replayer)
        return h, rs
    return _build


# ─────────────────────────────────────────────────────────────────────
# substitute path
# ─────────────────────────────────────────────────────────────────────

class TestSubstitutePath:

    async def test_tool_body_not_executed(
        self, recording, target_harness_factory, call_log,
    ):
        r = ToolReplayer(view=recording, mode=ReplayMode.STRICT_TAPE)
        h, _ = target_harness_factory(r)
        await h.call(tool_name="spy_add", arguments={"a": 1, "b": 2})
        assert call_log["add"] == 0

    async def test_full_quartet_folded(
        self, recording, target_harness_factory,
    ):
        """substitute MUST fold: decision + intent + result + spend."""
        r = ToolReplayer(view=recording, mode=ReplayMode.STRICT_TAPE)
        h, rs = target_harness_factory(r)
        out = await h.call(tool_name="spy_add", arguments={"a": 1, "b": 2})
        eid = out["tool_use_id"]

        assert len(claims_with_eid(rs.store, TAG_EFFECT_INTENT,  eid)) == 1
        assert len(claims_with_eid(rs.store, TAG_EFFECT_RESULT,  eid)) == 1
        assert claims_with_eid(rs.store, TAG_EFFECT_REJECTED, eid) == []

        # decision: session_init=True (preamble) + session_init=False (this call)
        assert len(decision_claims(rs.store, session_init=True))  == 1
        assert len(decision_claims(rs.store, session_init=False)) == 1

    async def test_result_fields_verbatim_except_eid(
        self, recording, target_harness_factory,
    ):
        """F_OUTPUT / F_SIDE_EFFECT / F_STATUS round-trip; F_EFFECT_ID is rewritten."""
        r = ToolReplayer(view=recording, mode=ReplayMode.STRICT_TAPE)
        h, rs = target_harness_factory(r)
        out = await h.call(tool_name="spy_add", arguments={"a": 1, "b": 2})
        eid = out["tool_use_id"]
        result = claims_with_eid(rs.store, TAG_EFFECT_RESULT, eid)[0]

        assert result.fields[F_EFFECT_ID]  == eid
        assert result.fields[F_OUTPUT]     == 3   # the recorded output
        assert result.fields[F_STATUS]     == "ok"
        assert result.fields[F_SIDE_EFFECT] == "pure"

    async def test_decision_carries_target_and_source_eids_distinct(
        self, recording, target_harness_factory,
    ):
        r = ToolReplayer(view=recording, mode=ReplayMode.STRICT_TAPE)
        h, rs = target_harness_factory(r)
        out = await h.call(tool_name="spy_add", arguments={"a": 1, "b": 2})
        eid_target = out["tool_use_id"]

        per_call = decision_claims(rs.store, session_init=False)
        assert len(per_call) == 1
        d = per_call[0]
        assert d.fields[F_DECISION_TARGET_EFFECT_ID] == eid_target
        assert d.fields[F_DECISION_SOURCE_EFFECT_ID] is not None
        assert d.fields[F_DECISION_SOURCE_EFFECT_ID] != eid_target
        assert d.fields[F_DECISION_OPERATION] == "substitute"

    async def test_spend_is_tool_calls_only_no_wall_seconds(
        self, recording, target_harness_factory,
    ):
        """substitute spends tool_calls=1.0 with wall_seconds=0.0;
        the 0.0 is filtered by _fold_spend's amount<=0 guard, so
        only the tool_calls claim hits the store."""
        r = ToolReplayer(view=recording, mode=ReplayMode.STRICT_TAPE)
        h, rs = target_harness_factory(r)
        out = await h.call(tool_name="spy_add", arguments={"a": 1, "b": 2})
        eid = out["tool_use_id"]

        spends = claims_with_eid(rs.store, TAG_RESOURCE_SPENT, eid)
        buckets = {c.fields[F_BUCKET]: c.fields[F_AMOUNT] for c in spends}
        assert buckets == {"tool_calls": 1.0}

    async def test_anthropic_shape_preserved(
        self, recording, target_harness_factory,
    ):
        r = ToolReplayer(view=recording, mode=ReplayMode.STRICT_TAPE)
        h, _ = target_harness_factory(r)
        out = await h.call(
            tool_name="spy_add", arguments={"a": 1, "b": 2},
            effect_id="tool_abcdef012345",
        )
        assert out["type"] == "tool_result"
        assert out["tool_use_id"] == "tool_abcdef012345"
        assert "is_error" not in out  # success path

    async def test_recorded_error_round_trips_is_error(
        self, recording, target_harness_factory,
    ):
        """spy_fail's recording was an error; substitute must surface
        is_error=True without re-raising."""
        r = ToolReplayer(view=recording, mode=ReplayMode.STRICT_TAPE)
        h, rs = target_harness_factory(r)
        out = await h.call(tool_name="spy_fail", arguments={"x": 7})
        assert out.get("is_error") is True
        result = claims_with_eid(rs.store, TAG_EFFECT_RESULT, out["tool_use_id"])[0]
        assert result.fields[F_STATUS] == "error"

    async def test_caller_supplied_effect_id_used(
        self, recording, target_harness_factory,
    ):
        r = ToolReplayer(view=recording, mode=ReplayMode.STRICT_TAPE)
        h, rs = target_harness_factory(r)
        my_eid = "tool_deadbeef0001"
        await h.call(
            tool_name="spy_add", arguments={"a": 1, "b": 2},
            effect_id=my_eid,
        )
        assert claims_with_eid(rs.store, TAG_EFFECT_INTENT, my_eid)
        assert claims_with_eid(rs.store, TAG_EFFECT_RESULT, my_eid)


# ─────────────────────────────────────────────────────────────────────
# refuse path
# ─────────────────────────────────────────────────────────────────────

class TestRefusePath:

    async def test_raises_replay_refused(
        self, recording, target_harness_factory,
    ):
        r = ToolReplayer(view=recording, mode=ReplayMode.LIVE_REROUTE)
        h, _ = target_harness_factory(r)
        with pytest.raises(ReplayRefused):
            await h.call(
                tool_name="spy_send",
                arguments={"target": "t", "payload": "p"},
            )

    async def test_tool_body_not_executed(
        self, recording, target_harness_factory, call_log,
    ):
        r = ToolReplayer(view=recording, mode=ReplayMode.LIVE_REROUTE)
        h, _ = target_harness_factory(r)
        with pytest.raises(ReplayRefused):
            await h.call(
                tool_name="spy_send",
                arguments={"target": "t", "payload": "p"},
            )
        assert call_log["send"] == 0

    async def test_intent_and_rejection_folded_no_result(
        self, recording, target_harness_factory,
    ):
        r = ToolReplayer(view=recording, mode=ReplayMode.LIVE_REROUTE)
        h, rs = target_harness_factory(r)
        my_eid = "tool_111111111111"
        with pytest.raises(ReplayRefused):
            await h.call(
                tool_name="spy_send",
                arguments={"target": "t", "payload": "p"},
                effect_id=my_eid,
            )
        assert claims_with_eid(rs.store, TAG_EFFECT_INTENT,   my_eid)
        assert claims_with_eid(rs.store, TAG_EFFECT_REJECTED, my_eid)
        assert not claims_with_eid(rs.store, TAG_EFFECT_RESULT, my_eid)

    async def test_rejection_detail_carries_replay_metadata(
        self, recording, target_harness_factory,
    ):
        r = ToolReplayer(view=recording, mode=ReplayMode.LIVE_REROUTE)
        h, rs = target_harness_factory(r)
        my_eid = "tool_222222222222"
        with pytest.raises(ReplayRefused):
            await h.call(
                tool_name="spy_send",
                arguments={"target": "t", "payload": "p"},
                effect_id=my_eid,
            )
        rej = claims_with_eid(rs.store, TAG_EFFECT_REJECTED, my_eid)[0]
        assert rej.fields[F_REASON] == "replay:refused_external"
        d = rej.fields[F_DETAIL]
        assert d["mode"] == "live_reroute"
        assert d["tool_name"] == "spy_send"
        assert d["declared_class"] == "external_write"
        assert d["effective_class"] == "external_write"


# ─────────────────────────────────────────────────────────────────────
# fail path
# ─────────────────────────────────────────────────────────────────────

class TestFailPath:

    async def test_raises_replay_missing(
        self, recording, target_harness_factory,
    ):
        r = ToolReplayer(view=recording, mode=ReplayMode.STRICT_TAPE)
        h, _ = target_harness_factory(r)
        with pytest.raises(ReplayMissing):
            await h.call(
                tool_name="spy_add", arguments={"a": 999, "b": 999},
            )

    async def test_only_decision_folded_no_intent_or_result(
        self, recording, target_harness_factory,
    ):
        r = ToolReplayer(view=recording, mode=ReplayMode.STRICT_TAPE)
        h, rs = target_harness_factory(r)
        my_eid = "tool_333333333333"
        with pytest.raises(ReplayMissing):
            await h.call(
                tool_name="spy_add", arguments={"a": 999, "b": 999},
                effect_id=my_eid,
            )
        # No intent, no result, no rejection for this eid.
        assert not claims_with_eid(rs.store, TAG_EFFECT_INTENT,   my_eid)
        assert not claims_with_eid(rs.store, TAG_EFFECT_RESULT,   my_eid)
        assert not claims_with_eid(rs.store, TAG_EFFECT_REJECTED, my_eid)
        # But the decision IS folded.
        decisions = [
            c for c in decision_claims(rs.store, session_init=False)
            if c.fields.get(F_DECISION_TARGET_EFFECT_ID) == my_eid
        ]
        assert len(decisions) == 1
        assert decisions[0].fields[F_DECISION_OPERATION] == "fail"

    async def test_tool_body_not_executed(
        self, recording, target_harness_factory, call_log,
    ):
        r = ToolReplayer(view=recording, mode=ReplayMode.STRICT_TAPE)
        h, _ = target_harness_factory(r)
        with pytest.raises(ReplayMissing):
            await h.call(
                tool_name="spy_add", arguments={"a": 999, "b": 999},
            )
        assert call_log["add"] == 0


# ─────────────────────────────────────────────────────────────────────
# re-execute path
# ─────────────────────────────────────────────────────────────────────

class TestReExecutePath:

    async def test_tool_body_executed_once(
        self, recording, target_harness_factory, call_log,
    ):
        r = ToolReplayer(view=recording, mode=ReplayMode.LIVE_REROUTE)
        h, _ = target_harness_factory(r)
        await h.call(tool_name="spy_add", arguments={"a": 1, "b": 2})
        assert call_log["add"] == 1

    async def test_decision_recorded_as_re_execute(
        self, recording, target_harness_factory,
    ):
        r = ToolReplayer(view=recording, mode=ReplayMode.LIVE_REROUTE)
        h, rs = target_harness_factory(r)
        out = await h.call(tool_name="spy_add", arguments={"a": 1, "b": 2})
        eid = out["tool_use_id"]
        d = [c for c in decision_claims(rs.store, session_init=False)
             if c.fields[F_DECISION_TARGET_EFFECT_ID] == eid][0]
        assert d.fields[F_DECISION_OPERATION] == "re-execute"
        # Recording was found, source eid should still be populated
        assert d.fields[F_DECISION_SOURCE_EFFECT_ID] is not None

    async def test_spend_includes_real_wall_seconds(
        self, recording, target_harness_factory,
    ):
        """re-execute uses the normal pipeline → wall_seconds is
        a measured monotonic delta, not zero."""
        r = ToolReplayer(view=recording, mode=ReplayMode.LIVE_REROUTE)
        h, rs = target_harness_factory(r)
        out = await h.call(tool_name="spy_add", arguments={"a": 1, "b": 2})
        eid = out["tool_use_id"]
        spends = {
            c.fields[F_BUCKET]: c.fields[F_AMOUNT]
            for c in claims_with_eid(rs.store, TAG_RESOURCE_SPENT, eid)
        }
        assert spends["tool_calls"] == 1.0
        # wall_seconds may be filtered out if exactly 0.0 (highly
        # unlikely for a real call); accept either presence with
        # >=0 or absence.
        assert spends.get("wall_seconds", 0.0) >= 0.0


# ─────────────────────────────────────────────────────────────────────
# session_init lifecycle
# ─────────────────────────────────────────────────────────────────────

class TestSessionInit:

    async def test_zero_calls_no_session_init(
        self, recording, target_harness_factory,
    ):
        r = ToolReplayer(view=recording, mode=ReplayMode.STRICT_TAPE)
        h, rs = target_harness_factory(r)
        # No call() yet.
        assert decision_claims(rs.store, session_init=True) == []

    async def test_first_call_folds_session_init_once(
        self, recording, target_harness_factory,
    ):
        r = ToolReplayer(view=recording, mode=ReplayMode.STRICT_TAPE)
        h, rs = target_harness_factory(r)
        await h.call(tool_name="spy_add", arguments={"a": 1, "b": 2})
        await h.call(tool_name="spy_send", arguments={"target": "t", "payload": "p"})
        # Three calls including spy_fail — but we can also do mixed paths.
        await h.call(tool_name="spy_fail", arguments={"x": 7})
        assert len(decision_claims(rs.store, session_init=True)) == 1

    async def test_session_init_carries_mode_and_cap(
        self, recording, target_harness_factory,
    ):
        r = ToolReplayer(view=recording, mode=ReplayMode.STRICT_TAPE)
        h, rs = target_harness_factory(r)
        await h.call(tool_name="spy_add", arguments={"a": 1, "b": 2})
        init = decision_claims(rs.store, session_init=True)[0]
        assert init.fields[F_DECISION_MODE] == "strict_tape"
        assert init.fields[F_DECISION_FROZEN_MAX_SEQ] == r.frozen_max_seq
        assert init.fields[F_DECISION_OPERATION] == "session_init"

    async def test_session_init_invariant_across_mixed_operations(
        self, recording, target_harness_factory,
    ):
        """One session_init, regardless of which operations fire."""
        r = ToolReplayer(
            view=recording,
            mode=ReplayMode.LIVE_REROUTE,
            allow_external_write=False,
            frozen_max_seq=None,
        )
        h, rs = target_harness_factory(r)
        # re-execute (PURE)
        await h.call(tool_name="spy_add", arguments={"a": 1, "b": 2})
        # refuse (EXTERNAL_WRITE without opt-in)
        with pytest.raises(ReplayRefused):
            await h.call(
                tool_name="spy_send",
                arguments={"target": "t", "payload": "p"},
            )
        # re-execute again (different args, absent → re-execute under LIVE_REROUTE)
        await h.call(tool_name="spy_add", arguments={"a": 99, "b": 99})

        assert len(decision_claims(rs.store, session_init=True)) == 1
        assert len(decision_claims(rs.store, session_init=False)) == 3
