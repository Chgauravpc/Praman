"""The receipt auditor, against hand-fabricated claims.

BUILD-PLAN Day 4, block D, and the Day 4 definition-of-done item that reads
"fabricated claims are stripped **by test**, not by prompt". That distinction is
the whole file. A prompt asking a model to cite its evidence is a request; these
tests are the enforcement, and they involve no model at all -- every diagnosis
here is constructed by hand, in the shape a lying model would produce, and
checked against a real tool belt with a real call log.

The fabrication modes are enumerated deliberately rather than sampled, because
"we tested that it catches fabrication" is only meaningful if the *kinds* of
fabrication are named:

1. a claim citing no receipt at all;
2. a claim citing a call_id that never happened;
3. a claim citing a call_id from a *different* session;
4. a receipt whose ``result_hash`` does not match the recorded output;
5. a receipt whose ``args_hash`` does not match the recorded arguments;
6. a receipt naming a different tool than the call actually was;
7. a claim citing a call that returned an *error*;
8. a claim stating a quantity that does not appear in its own cited evidence;
9. a diagnosis whose surviving claims fall below the support threshold;
10. a diagnosis with no claims whatsoever.

Every one of those is a way a plausible-looking diagnosis can be wrong, and every
one is stripped or downgraded here.
"""
from __future__ import annotations

import pytest

from pramaan.investigate import receipts as R
from pramaan.investigate.tools import ToolBelt, build_agent_db, hash_of
from pramaan.schemas import Claim, Diagnosis, Receipt
from sim.generate import dev_batch_degraded
from sim.incident import DEV_INCIDENT, build_downtime, build_traffic


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def world():
    events, truth = dev_batch_degraded()
    traffic = build_traffic(events, 42, DEV_INCIDENT)
    downtime = build_downtime(DEV_INCIDENT)
    return events, traffic, downtime, truth


@pytest.fixture()
def belt(world):
    """A belt with three real calls already made: tc_01, tc_02, tc_03.

    A fresh belt per test, because the call log is session state and a shared one
    would let an earlier test's calls satisfy a later test's fabricated receipt --
    which is precisely the cross-session reuse check 1 exists to catch, so
    accidentally allowing it in the fixture would be a genuinely funny way to lose
    the guarantee.
    """
    events, traffic, downtime, _truth = world
    b = ToolBelt(build_agent_db(events, traffic, downtime))
    b.call("decompose", {"window": [3, 4], "dimension": "segment"})     # tc_01
    b.call("compare_baseline", {"window": [3, 4]})                       # tc_02
    b.call("get_downtime", {"window": [3, 4]})                           # tc_03
    return b


def _diagnosis(belt, claims, **kwargs):
    """A diagnosis whose receipts are honestly stamped from the belt's own log."""
    cited = [rid for c in claims for rid in c.receipt_ids]
    return Diagnosis(
        diagnosis_class=kwargs.pop("diagnosis_class", "issuer_degraded"),
        summary=kwargs.pop("summary", "a summary"),
        claims=list(claims),
        receipts=R.receipts_for(belt, dict.fromkeys(cited)),
        confidence=kwargs.pop("confidence", 0.7),
        falsifiable_by=kwargs.pop(
            "falsifiable_by", "if the elevation persists outside the window this is wrong"
        ),
        **kwargs,
    )


def _true_claims(belt):
    """Three claims that are actually true of tc_01..tc_03.

    Built from the recorded results rather than typed, so the honest baseline
    cannot drift when the simulator's numbers move. A test whose "true" case is a
    hardcoded number is a test that starts failing for the wrong reason.
    """
    decomp = belt.get("tc_01").result
    compare = belt.get("tc_02").result
    tier2 = next(r for r in compare["rows"] if r["key"] == "tier2")
    return [
        Claim(
            claim_id="c1",
            statement="The rate effect is %+.1fpp and the mix effect is %+.1fpp."
            % (100 * decomp["rate_effect"], 100 * decomp["mix_effect"]),
            receipt_ids=["tc_01"],
        ),
        Claim(
            claim_id="c2",
            statement="tier2 moved %+.1fpp against its own trailing baseline."
            % (100 * tier2["delta"]),
            receipt_ids=["tc_02"],
        ),
        Claim(
            claim_id="c3",
            statement="No platform downtime was declared in this window.",
            receipt_ids=["tc_03"],
        ),
    ]


# --------------------------------------------------------------------------
# The honest baseline -- if this does not pass, nothing below means anything
# --------------------------------------------------------------------------


def test_honest_diagnosis_survives_intact(belt):
    """A diagnosis whose claims are all true of their evidence keeps all of them.

    Stated first and deliberately: an auditor that strips everything would pass
    every fabrication test in this file while being useless. The interesting
    property is discrimination, not strictness.
    """
    result = R.audit(_diagnosis(belt, _true_claims(belt)), belt)
    assert result.status == R.STATUS_SUPPORTED
    assert len(result.surviving) == 3
    assert result.stripped == ()
    assert result.receipt_coverage == 1.0
    assert result.support == 1.0
    assert result.may_plan_action is True


# --------------------------------------------------------------------------
# 1-3: provenance of the call_id
# --------------------------------------------------------------------------


def test_claim_with_no_receipt_is_stripped(belt):
    claims = _true_claims(belt)
    claims.append(
        Claim(claim_id="c4", statement="The issuer is HDFC.", receipt_ids=[])
    )
    result = R.audit(_diagnosis(belt, claims), belt)
    stripped = {s.claim_id: s.reason for s in result.stripped}
    assert stripped == {"c4": R.REASON_NO_RECEIPT}
    # Receipt coverage is the share that cited *anything*, so it drops here.
    assert result.receipt_coverage == pytest.approx(3 / 4)


def test_claim_citing_a_nonexistent_call_id_is_stripped(belt):
    """The canonical fabrication: an invented receipt for an invented fact."""
    claims = _true_claims(belt)
    claims.append(
        Claim(
            claim_id="c4",
            statement="Mandate debits above the AFA ceiling fail at 94.0%.",
            receipt_ids=["tc_99"],
        )
    )
    result = R.audit(_diagnosis(belt, claims), belt)
    stripped = {s.claim_id: s.reason for s in result.stripped}
    assert stripped == {"c4": R.REASON_UNKNOWN_CALL}
    # And note this one *does* count as covered: it cited a receipt. Coverage
    # measures citation, survival measures truth, and conflating them is how a
    # model that cites confidently gets scored as accurate.
    assert result.receipt_coverage == 1.0
    assert result.support == pytest.approx(3 / 4)


def test_receipt_from_another_session_is_stripped(world, belt):
    """A receipt that verifies against its own session but not against this one.

    The check is "this session's tool log", and the reason is not pedantry: two
    investigations of two different incidents produce identically-named call_ids
    (``tc_01`` in both), with entirely different results. Without session
    scoping, a diagnosis of incident A could be supported by evidence gathered
    about incident B, and every hash would verify.
    """
    events, traffic, downtime, _truth = world
    other = ToolBelt(build_agent_db(events, traffic, downtime))
    other.call("compare_baseline", {"window": [6, 7]})   # its own tc_01

    # Honestly stamped -- from the *other* session.
    foreign = R.receipts_for(other, ["tc_01"])
    assert foreign[0].receipt_id == "tc_01"

    claims = _true_claims(belt)
    diagnosis = Diagnosis(
        diagnosis_class="issuer_degraded",
        summary="s",
        claims=claims,
        receipts=foreign + R.receipts_for(belt, ["tc_02", "tc_03"]),
        confidence=0.7,
        falsifiable_by="something concrete that would disprove this",
    )
    result = R.audit(diagnosis, belt)
    # c1 cites tc_01, whose receipt is the foreign one; its hashes do not match
    # this session's tc_01.
    assert any(
        s.claim_id == "c1" and s.reason == R.REASON_HASH_MISMATCH for s in result.stripped
    )


# --------------------------------------------------------------------------
# 4-6: integrity of the receipt itself
# --------------------------------------------------------------------------


def test_tampered_result_hash_is_stripped(belt):
    """A receipt whose digest is not the digest of the recorded output."""
    claims = _true_claims(belt)
    honest = R.receipts_for(belt, ["tc_01", "tc_02", "tc_03"])
    tampered = [
        honest[0].model_copy(update={"result_hash": "sha256:" + "0" * 64}),
        honest[1],
        honest[2],
    ]
    diagnosis = Diagnosis(
        diagnosis_class="issuer_degraded", summary="s", claims=claims,
        receipts=tampered, confidence=0.7,
        falsifiable_by="a concrete observation that would disprove this",
    )
    result = R.audit(diagnosis, belt)
    stripped = {s.claim_id: s.reason for s in result.stripped}
    assert stripped == {"c1": R.REASON_HASH_MISMATCH}


def test_tampered_stored_result_is_detected(belt):
    """The other direction: the *log* is altered after the receipt was issued.

    This is the property that makes a receipt tamper-evident rather than merely
    consistent. The auditor recomputes the digest from the stored payload instead
    of comparing two recorded strings, so altering the payload breaks the match
    even though nobody touched the receipt.

    A string comparison of two recorded hashes -- the simplification BUILD-PLAN
    Day 4 explicitly declined -- would pass this test while providing none of the
    guarantee, which is exactly why the hash is computed rather than compared.
    """
    claims = _true_claims(belt)
    diagnosis = _diagnosis(belt, claims)

    entry = belt.get("tc_01")
    forged = dict(entry.result)
    forged["rate_effect"] = 0.99          # "the whole rise was a real degradation"
    belt._log["tc_01"] = type(entry)(
        call_id=entry.call_id, tool=entry.tool, args=entry.args,
        args_hash=entry.args_hash, result=forged,
        result_hash=entry.result_hash,    # left untouched: the forger's mistake
        row_count=entry.row_count, ok=entry.ok, rendered=entry.rendered,
        elapsed_ms=entry.elapsed_ms,
    )

    result = R.audit(diagnosis, belt)
    assert any(
        s.claim_id == "c1" and s.reason == R.REASON_HASH_MISMATCH for s in result.stripped
    )


def test_tampered_args_hash_is_stripped(belt):
    claims = _true_claims(belt)
    honest = R.receipts_for(belt, ["tc_01", "tc_02", "tc_03"])
    tampered = [honest[0].model_copy(update={"args_hash": hash_of({"window": [0, 1]})})]
    diagnosis = Diagnosis(
        diagnosis_class="issuer_degraded", summary="s", claims=claims[:1],
        receipts=tampered, confidence=0.7,
        falsifiable_by="a concrete observation that would disprove this",
    )
    result = R.audit(diagnosis, belt)
    assert [s.reason for s in result.stripped] == [R.REASON_ARGS_MISMATCH]


def test_receipt_naming_the_wrong_tool_is_stripped(belt):
    """A receipt claiming ``get_downtime`` for what was a ``decompose`` call.

    Worth its own case because the hashes can be perfectly honest while the tool
    name is a lie, and the tool name is what a human reads when deciding whether
    a claim is well-evidenced.
    """
    entry = belt.get("tc_01")
    lying = Receipt(
        receipt_id="tc_01",
        tool="get_downtime",
        args_hash=entry.args_hash,
        result_hash=entry.result_hash,
        row_count=entry.row_count,
    )
    diagnosis = Diagnosis(
        diagnosis_class="issuer_degraded", summary="s",
        claims=[Claim(claim_id="c1", statement="No downtime was declared.", receipt_ids=["tc_01"])],
        receipts=[lying], confidence=0.7,
        falsifiable_by="a concrete observation that would disprove this",
    )
    result = R.audit(diagnosis, belt)
    assert [s.reason for s in result.stripped] == [R.REASON_ARGS_MISMATCH]


# --------------------------------------------------------------------------
# 7: a failed call supports nothing
# --------------------------------------------------------------------------


def test_claim_citing_a_failed_call_is_stripped(belt):
    """A refused query is a real, correctly-hashed log entry and proves nothing.

    Checks 1 and 2 both pass on it -- the call happened, the digest of the error
    payload is correct -- which is exactly why this needs its own check. Without
    it, "SELECT * FROM latent" being refused would become a citable receipt.
    """
    refused = belt.call("query_sql", {"sql": "SELECT * FROM latent"})
    assert refused.ok is False

    diagnosis = _diagnosis(
        belt,
        [
            Claim(claim_id="c1", statement="Intent is present for most payers.",
                  receipt_ids=[refused.call_id]),
            Claim(claim_id="c2", statement="No platform downtime was declared.",
                  receipt_ids=["tc_03"]),
        ],
    )
    result = R.audit(diagnosis, belt)
    stripped = {s.claim_id: s.reason for s in result.stripped}
    assert stripped == {"c1": R.REASON_FAILED_CALL}


# --------------------------------------------------------------------------
# 8: relevance, not just provenance
# --------------------------------------------------------------------------


def test_quantity_absent_from_cited_evidence_is_stripped(belt):
    """The fabrication both PRD checks miss: a real receipt, an invented number.

    The call_id exists and the hash matches, so provenance is impeccable. The
    magnitude is invented. This is the fabrication mode that actually moves money
    -- a planner reads a magnitude, not a citation -- and it is why this module
    has a third check the PRD does not specify.

    94.0 is chosen because it appears nowhere in that result. That sounds like a
    detail and is not: the first version of this test used 41.0, which the auditor
    cleared, because 41.0% happens to be tier2's window rate in the same result.
    See ``test_number_present_but_attached_to_the_wrong_entity`` for what that
    revealed.
    """
    claims = _true_claims(belt)[:2]
    claims.append(
        Claim(
            claim_id="c9",
            statement="Mandate debits in this window failed at 94.0%.",
            receipt_ids=["tc_02"],
        )
    )
    result = R.audit(_diagnosis(belt, claims), belt)
    stripped = {s.claim_id: s.reason for s in result.stripped}
    assert stripped == {"c9": R.REASON_NUMBER_NOT_IN_EVIDENCE}


def test_number_present_but_attached_to_the_wrong_entity_is_a_known_limit(belt):
    """A documented hole, asserted so it cannot close by accident or widen unseen.

    "tier3's failure rate rose 41.0pp" citing ``compare_baseline`` passes check 3,
    because 41.0% is in that result -- it is tier2's window rate. The claim is
    false about tier3 and the number is genuine, so a check that asks only
    "is this figure in the evidence" clears it.

    This test asserts the limit rather than the capability, which is deliberate.
    Binding a number to its grammatical subject is not a mechanical operation; it
    would require a language model inside the verifier that exists to verify a
    language model. Writing the limit down as an executable assertion means a
    later reader finds it here instead of assuming a guarantee the code does not
    give -- and if someone does implement subject binding, this test fails and
    tells them to update the claim in the docstring.
    """
    claims = _true_claims(belt)[:2]
    tier2_window_rate = next(
        r["window_rate"] for r in belt.get("tc_02").result["rows"] if r["key"] == "tier2"
    )
    claims.append(
        Claim(
            claim_id="c9",
            statement="tier3's failure rate rose %.1fpp in the window."
            % (100 * tier2_window_rate),
            receipt_ids=["tc_02"],
        )
    )
    result = R.audit(_diagnosis(belt, claims), belt)
    assert result.stripped == (), (
        "the auditor now binds numbers to their subject. That is an improvement -- "
        "update the 'two limits' section of receipts.py and delete this test."
    )


def test_a_true_quantity_written_either_way_survives(belt):
    """41.7% and 0.417 are the same finding, and both must pass.

    The tolerance exists so that honest claims are not stripped for formatting,
    and this test is what stops that tolerance from being tightened into a source
    of false strips. An auditor that failed this would drive receipt coverage down
    for a reason unrelated to fabrication, and the published metric would then be
    measuring the auditor rather than the model.
    """
    compare = belt.get("tc_02").result
    tier3 = next(r for r in compare["rows"] if r["key"] == "tier3")
    as_percent = "tier3 sat at %.1f%% in the baseline." % (100 * tier3["baseline_rate"])
    as_fraction = "tier3 sat at %.3f in the baseline." % tier3["baseline_rate"]

    for statement in (as_percent, as_fraction):
        diagnosis = _diagnosis(
            belt,
            [
                Claim(claim_id="c1", statement=statement, receipt_ids=["tc_02"]),
                Claim(claim_id="c2", statement="No downtime was declared.", receipt_ids=["tc_03"]),
            ],
        )
        result = R.audit(diagnosis, belt)
        assert result.stripped == (), "stripped an honest claim: %s" % statement


def test_qualitative_claims_are_not_checked_for_numbers(belt):
    """A claim with no numbers is only checked for provenance, and that is stated.

    Recorded as a test rather than only as a docstring because it is the
    auditor's honest limit: receipts bound what a diagnosis may assert as fact,
    and they do not make its judgement right. Pretending otherwise would be the
    overclaim the whole apparatus exists to avoid.
    """
    diagnosis = _diagnosis(
        belt,
        [
            Claim(claim_id="c1", statement="This has the shape of an issuer problem.",
                  receipt_ids=["tc_01"]),
            Claim(claim_id="c2", statement="Nothing here suggests a merchant misconfiguration.",
                  receipt_ids=["tc_03"]),
        ],
    )
    result = R.audit(diagnosis, belt)
    assert result.stripped == ()
    assert result.status == R.STATUS_SUPPORTED


def test_small_structural_integers_do_not_need_evidence(belt):
    """"All 3 segments" is counting, not measuring."""
    diagnosis = _diagnosis(
        belt,
        [
            Claim(claim_id="c1", statement="All 3 segments were examined.", receipt_ids=["tc_01"]),
            Claim(claim_id="c2", statement="The window spans 2 days.", receipt_ids=["tc_02"]),
        ],
    )
    assert R.audit(diagnosis, belt).stripped == ()


# --------------------------------------------------------------------------
# 9-10: downgrade to UNSUPPORTED
# --------------------------------------------------------------------------


def test_support_below_threshold_downgrades_the_whole_diagnosis(belt):
    """Two of five claims surviving is 40% support, below the two-thirds bar.

    And the consequence is the one that matters: ``may_plan_action`` is False, so
    the diagnosis is mechanically incapable of reaching a planner. That is PRD
    6.2's step 4, and it is the sentence the whole day is built to make true.
    """
    claims = _true_claims(belt)[:2] + [
        Claim(claim_id="f1", statement="Card network failures rose 30.0pp.", receipt_ids=["tc_97"]),
        Claim(claim_id="f2", statement="UPI autopay is disabled.", receipt_ids=["tc_98"]),
        Claim(claim_id="f3", statement="The merchant changed checkout.", receipt_ids=[]),
    ]
    result = R.audit(_diagnosis(belt, claims), belt)
    assert result.status == R.STATUS_UNSUPPORTED
    assert result.may_plan_action is False
    assert result.support == pytest.approx(2 / 5)
    assert len(result.stripped) == 3
    assert result.notes, "a downgrade must say why"


def test_stripping_rescored_diagnosis_keeps_only_surviving_claims(belt):
    """Step 3 of PRD 6.2: the diagnosis is re-scored *without* the stripped claims.

    And the receipts of stripped claims go with them. A receipt nothing cites is
    not evidence, and leaving it attached would let a later reader -- or a
    generated report -- count it as support the diagnosis does not have.
    """
    claims = _true_claims(belt) + [
        Claim(claim_id="bad", statement="Failures rose 88.0pp.", receipt_ids=["tc_96"])
    ]
    result = R.audit(_diagnosis(belt, claims), belt)
    assert [c.claim_id for c in result.diagnosis.claims] == ["c1", "c2", "c3"]
    assert {r.receipt_id for r in result.diagnosis.receipts} == {"tc_01", "tc_02", "tc_03"}
    assert result.diagnosis.status == R.STATUS_SUPPORTED


def test_a_single_surviving_claim_is_not_an_investigation(belt):
    """100% support on one claim is arithmetically fine and substantively empty.

    PRD 6.2's own example triangulates across three tools precisely because one
    query is not an investigation, so there is a floor on surviving claims as
    well as a share.
    """
    diagnosis = _diagnosis(belt, _true_claims(belt)[:1])
    result = R.audit(diagnosis, belt)
    assert result.support == 1.0
    assert result.status == R.STATUS_UNSUPPORTED
    assert result.may_plan_action is False


def test_a_diagnosis_with_no_claims_is_unsupported(belt):
    """An assertion with nothing behind it must not score as fully supported.

    The naive arithmetic gives it 100% coverage -- zero of zero claims are
    unbacked -- which is true and useless. This is the degenerate case that a
    coverage metric reports as perfect.
    """
    diagnosis = _diagnosis(belt, [])
    result = R.audit(diagnosis, belt)
    assert result.status == R.STATUS_UNSUPPORTED
    assert result.may_plan_action is False
    assert result.claims_in == 0


def test_unsupported_diagnosis_keeps_its_class_but_loses_its_confidence(belt):
    """The audit trail survives the downgrade; the model's self-assessment does not.

    Blanking the class would destroy the record of what the model actually said,
    which is the thing a reviewer wants to see. Keeping the confidence would leave
    a 0.9 sitting next to an UNSUPPORTED verdict, and someone downstream would
    eventually read it.
    """
    diagnosis = _diagnosis(belt, _true_claims(belt)[:1], confidence=0.95)
    result = R.audit(diagnosis, belt)
    assert result.diagnosis.diagnosis_class == "issuer_degraded"
    assert result.diagnosis.confidence == 0.0


# --------------------------------------------------------------------------
# Determinism and reporting
# --------------------------------------------------------------------------


def test_audit_is_deterministic_and_does_not_mutate_its_input(belt):
    diagnosis = _diagnosis(belt, _true_claims(belt))
    before = diagnosis.model_dump()
    first = R.audit(diagnosis, belt)
    second = R.audit(diagnosis, belt)
    assert first.as_ledger_payload() == second.as_ledger_payload()
    assert diagnosis.model_dump() == before, "audit mutated the diagnosis it was given"


def test_coverage_aggregates_break_down_by_failure_reason(belt):
    """The aggregate reports *why* claims were stripped, not only how many.

    "Receipt coverage was 94%" is a number. "Six stripped claims, all of them
    quantities absent from their own evidence" is a finding about the model, and
    it is the one worth publishing (PRD 8).
    """
    good = R.audit(_diagnosis(belt, _true_claims(belt)), belt)
    bad = R.audit(
        _diagnosis(
            belt,
            _true_claims(belt)[:2]
            + [Claim(claim_id="x", statement="Rates rose 77.0pp.", receipt_ids=["tc_95"])],
        ),
        belt,
    )
    summary = R.coverage_of([good, bad])
    assert summary["diagnoses"] == 2
    assert summary["supported"] == 2
    assert summary["claims"] == 6
    assert summary["claims_surviving"] == 5
    assert summary["stripped_by_reason"] == {R.REASON_UNKNOWN_CALL: 1}


def test_receipts_for_ignores_unknown_call_ids(belt):
    """The stamping side must not invent a receipt for a call that never happened.

    If it did, a fabricated call_id would arrive at the auditor wearing an
    honestly-computed hash of nothing, and check 1 would be the only thing between
    an invented citation and a supported claim.
    """
    stamped = R.receipts_for(belt, ["tc_01", "tc_99"])
    assert [r.receipt_id for r in stamped] == ["tc_01"]


# --------------------------------------------------------------------------
# The number extractor, which the third check rests on
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("rose 12.0pp", [(12.0, "pp")]),
        ("41.7% of the total", [(41.7, "%")]),
        ("1,240 failures", [(1240.0, "")]),
        ("a rate of 0.417", [(0.417, "")]),
        ("down -3.5pp", [(-3.5, "pp")]),
        ("no numbers here", []),
        ("tier2 and tier3", []),
    ],
)
def test_number_extraction(text, expected):
    """``tier2`` must not read as the number 2.

    A segment label containing a digit is the obvious way this extractor breaks,
    and it would break in the damaging direction: every claim mentioning tier2
    would carry a phantom quantity that its evidence does not contain, and honest
    claims would be stripped wholesale.
    """
    assert R.numbers_in(text) == expected
