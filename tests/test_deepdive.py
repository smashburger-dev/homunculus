"""Tests for homunculus.deepdive. Synthetic claims, labels and quotes only; no network."""

from __future__ import annotations

import copy
import shutil
import tempfile
import unittest
from pathlib import Path

from homunculus import agents
from homunculus import deepdive as D
from homunculus import evidence as E
from homunculus import lexicon as L
from homunculus.guardrails import Rules
from homunculus.store import Ledger

AT = "2026-11-20T03:00:00Z"
SOURCE = (
    "Synthetic source A: dimming evening light raised ipRGC-mediated signaling in adults. "
    "Synthetic source B: in rats, ipRGC signaling advanced melatonin onset. "
    "Synthetic source C: an earlier melatonin onset shortened sleep onset in adults. "
    "Synthetic source D: the pathway is conserved across mammals."
)
RULES = Rules(supplements={"creatine": {"unit": "g", "max_dose": 5}})


def ev(paper: str, design: str, quote: str, relation: str = "supports") -> dict:
    return {"paper": paper, "design": design, "relation": relation, "quote": quote}


def link(lid: str, src: tuple[str, str], dst: tuple[str, str], kind: str, evidence, question=None,
         keywords=()) -> dict:
    return {"id": lid, "src": src[1], "src_type": src[0], "dst": dst[1], "dst_type": dst[0], "kind": kind,
            "evidence": list(evidence), "open_question": question, "keywords": list(keywords)}


LIGHT = ("intervention", "Evening light dimming")
IPRGC = ("mechanism", "ipRGC signaling")
MEL = ("mechanism", "Melatonin onset")
SLEEP = ("outcome", "Sleep onset")


def dive(**over) -> dict:
    """Three links; the middle one is shown in rats only."""
    out = {
        "claim": {"intervention": "Evening light dimming", "category": "behavioral", "outcome": "Sleep onset",
                  "tracker_key": "sleep_start_min", "supplement": None},
        "links": [
            link("l1", LIGHT, IPRGC, "mechanism",
                 [ev("doi:10.0/a", "mechanistic_human", "dimming evening light raised ipRGC-mediated signaling")]),
            link("l2", IPRGC, MEL, "biomarker",
                 [ev("doi:10.0/b", "animal", "in rats, ipRGC signaling advanced melatonin onset")],
                 "Advances ipRGC signaling melatonin onset in humans?", ["ipRGC", "melatonin onset"]),
            link("l3", MEL, SLEEP, "outcome",
                 [ev("doi:10.0/c", "rct_parallel", "an earlier melatonin onset shortened sleep onset")]),
        ],
        "conserved_pathway": {"value": True, "quote": "the pathway is conserved across mammals"},
    }
    out.update(over)
    return out


def decide(d, **kw):
    kw.setdefault("source_text", SOURCE)
    kw.setdefault("rules", RULES)
    return D.decide(d, paper_key="doi:10.0/frontier", paper_ref="evidence/papers/10-0-frontier.json", at=AT, **kw)


class SchemaTest(unittest.TestCase):
    def test_fixture_and_minimal_instance(self):
        self.assertEqual(D.validate(dive()), [])
        self.assertEqual(agents.validate_json(agents.minimal_instance(D.DEEPDIVE_SCHEMA), D.DEEPDIVE_SCHEMA), [])

    def test_schema_errors_come_first(self):
        bad = dive()
        bad["claim"]["category"] = "magic"
        self.assertTrue(D.validate(bad))
        self.assertTrue(D.validate(dive(links=[])))
        self.assertEqual(D.validate([]), ["deep dive must be an object"])

    def test_chain_must_be_contiguous_and_anchored(self):
        broken = dive()
        broken["links"][1]["src"] = "Something else"
        self.assertEqual(D.validate(broken), ["links/1: src does not continue links/0"])
        anchored = dive()
        anchored["claim"]["outcome"] = "Resting heart rate"
        self.assertEqual(D.validate(anchored), ["links/2: dst must be the claim's outcome"])
        dup = dive()
        dup["links"][2]["id"] = "l1"
        self.assertIn("link ids must be unique", D.validate(dup))
        symbols = dive()
        symbols["links"][0]["dst"] = "!!!"
        self.assertTrue(D.validate(symbols)[0].startswith("node label"))

    def test_typed_joints(self):
        retyped = dive()
        retyped["links"][1]["src_type"] = "concept"
        self.assertEqual(D.validate(retyped), ["links/1: src does not continue links/0"])


class QuoteTest(unittest.TestCase):
    def test_unquoted_evidence_is_dropped(self):
        d = dive()
        d["links"][0]["evidence"].append(ev("doi:10.0/x", "meta_rct", "a meta-analysis confirmed it"))
        d["conserved_pathway"]["quote"] = "conserved in all species"
        clean, dropped = D.void_unquoted(d, SOURCE)
        self.assertEqual(dropped, ["links/0/evidence/1", "conserved_pathway"])
        self.assertEqual(len(clean["links"][0]["evidence"]), 1)
        self.assertIsNone(clean["conserved_pathway"]["value"])
        self.assertEqual(len(d["links"][0]["evidence"]), 2)  # input untouched


class LinkTest(unittest.TestCase):
    def test_unscored_papers_are_capped_and_known_scores_win(self):
        l3 = dive()["links"][2]
        self.assertEqual(D.assess_link(l3)["certainty"], D.UNSCORED_MAX)
        self.assertEqual(D.assess_link(l3, {"doi:10.0/c": 3.5})["certainty"], 3.5)
        animal = D.assess_link(dive()["links"][1])
        self.assertEqual((animal["certainty"], animal["human"]), (0.5, False))

    def test_contradiction_contests(self):
        l3 = dive()["links"][2]
        l3["evidence"].append(ev("doi:10.0/d", "rct_parallel", "x", "contradicts"))
        a = D.assess_link(l3)
        self.assertEqual((a["contested"], a["certainty"]), (True, 0.0))
        self.assertFalse(D.assess_link(l3, {"doi:10.0/c": 3.0})["contested"])

    def test_lexicon_prior_and_retractions(self):
        l2 = dive()["links"][1]
        prior = L.LinkVerdict(1.0, 0.0, True, "supported", ("doi:10.0/h",))
        a = D.assess_link(l2, prior=prior)
        self.assertEqual((a["certainty"], a["human"], a["prior"]), (1.0, True, "supported"))
        gone = D.assess_link(l2, retracted={"doi:10.0/b"})
        self.assertEqual((gone["certainty"], gone["evidence"]), (0.0, []))

    def test_chain_support(self):
        assessed = [D.assess_link(x) for x in dive()["links"]]
        chain = D.chain_support(assessed)
        self.assertEqual((chain["weakest"], chain["coverage"], chain["holds"]), (0.5, round(2 / 3, 10), True))
        self.assertAlmostEqual(chain["support"], 0.5 * 2 / 3)
        self.assertFalse(D.chain_support([])["holds"])


class RiskTest(unittest.TestCase):
    def claim(self, **over) -> dict:
        return {**dive()["claim"], **over}

    def test_only_allowlisted_categories(self):
        self.assertEqual(D.risk_problems(self.claim(), RULES), [])
        self.assertEqual(D.risk_problems(self.claim(category="drug"), RULES), ["category:drug"])

    def test_forbidden_terms(self):
        problems = D.risk_problems(self.claim(intervention="Sleeping in an altitude tent"), RULES)
        self.assertEqual(problems, ["forbidden:altitude_protocol"])

    def test_supplements_go_through_guardrails(self):
        ok = {"name": "Creatine", "dose": 3, "unit": "g", "tested_by": "Informed Sport"}
        self.assertEqual(D.risk_problems(self.claim(category="supplement", supplement=ok), RULES), [])
        high = {**ok, "dose": 20}
        self.assertEqual(D.risk_problems(self.claim(category="supplement", supplement=high), RULES),
                         ["supplement:dose_above_max"])
        unknown = {"name": "Mystery blend", "dose": 1, "unit": "g", "tested_by": None}
        self.assertEqual(D.risk_problems(self.claim(category="supplement", supplement=unknown), RULES),
                         ["supplement:not_allowlisted", "supplement:untested"])
        self.assertEqual(D.risk_problems(self.claim(category="supplement"), RULES), ["supplement:missing"])

    def test_measurable(self):
        self.assertTrue(D.measurable(self.claim()))
        self.assertFalse(D.measurable(self.claim(tracker_key=None)))
        self.assertFalse(D.measurable(self.claim(tracker_key="mood_ring")))


class DecideTest(unittest.TestCase):
    def test_holding_low_risk_measurable_chain_is_a_candidate(self):
        rec = decide(dive())
        self.assertEqual((rec["decision"], rec["reasons"]), ("n_of_1_candidate", []))
        self.assertEqual(rec["belief_id"], "fr-10-0-frontier")
        self.assertEqual([q["link"] for q in rec["open_questions"]], ["l2"])
        self.assertEqual(rec["keywords"], ["iprgc", "melatonin onset"])
        self.assertTrue(rec["conserved_pathway"])
        self.assertEqual(rec, decide(copy.deepcopy(dive())))

    def test_rats_only_chain_stays_watch(self):
        d = dive()
        for x in d["links"]:
            x["evidence"] = [ev("doi:10.0/b", "animal", "in rats, ipRGC signaling advanced melatonin onset")]
        rec = decide(d)
        self.assertEqual(rec["decision"], "watch")
        self.assertEqual(rec["reasons"], ["coverage_low"])
        self.assertEqual(len(rec["open_questions"]), 3)
        self.assertEqual(rec["open_questions"][0]["text"], "Is Evening light dimming -> ipRGC signaling shown in humans?")
        self.assertEqual(rec["open_questions"][0]["keywords"], ["evening light dimming", "iprgc signaling"])

    def test_fabricated_quote_breaks_the_link(self):
        d = dive()
        d["links"][2]["evidence"][0]["quote"] = "a large trial proved it"
        rec = decide(d)
        self.assertEqual(rec["decision"], "watch")
        self.assertIn("weak_link:l3", rec["reasons"])
        self.assertEqual(rec["dropped_evidence"], ["links/2/evidence/0"])

    def test_high_risk_or_unmeasurable_stays_watch(self):
        d = dive()
        d["claim"]["category"] = "drug"
        self.assertEqual(decide(d)["reasons"], ["category:drug"])
        d = dive()
        d["claim"]["tracker_key"] = None
        self.assertEqual(decide(d)["reasons"], ["not_measurable"])

    def test_contested_link(self):
        d = dive()
        d["links"][2]["evidence"].append(ev("doi:10.0/d", "rct_parallel", "Synthetic source C", "contradicts"))
        rec = decide(d)
        self.assertIn("contested:l3", rec["reasons"])
        self.assertNotIn("weak_link:l3", rec["reasons"])

    def test_invalid_dive_raises(self):
        with self.assertRaises(D.DiveError) as ctx:
            decide(dive(links=[]))
        self.assertTrue(ctx.exception.problems)

    def test_promise_inputs_feed_the_appraisal(self):
        rec = decide(dive())
        inputs = D.promise_inputs(rec, E.PromiseInput(dose_mg_per_kg=20.0, species="rat", human_max_mg_per_kg=5.0))
        self.assertEqual((inputs.plausible, inputs.coherent, inputs.conserved_pathway), (True, True, True))
        self.assertAlmostEqual(inputs.human_link_share, 2 / 3)
        paper = {"design": {"value": "animal", "quote": "q"}, "n_total": {"value": 24, "quote": "q"},
                 "randomized": {"value": True, "quote": "q"}, "blinded_outcome": {"value": True, "quote": "q"},
                 "registration_id": {"value": "PCT-1", "quote": "q"}, "dose_response": {"value": True, "quote": "q"},
                 "peer_reviewed": {"value": True, "quote": None},
                 "effect": {"type": {"value": "SMD", "quote": "q"}, "est": {"value": 1.2, "quote": "q"},
                            "ci_lo": {"value": 0.6, "quote": "q"}, "ci_hi": {"value": 1.8, "quote": "q"}}}
        owner = E.OwnerProfile("female", 30, "trained", (), {})
        s = E.score(paper, owner, update_to=[], swc_paper_units=None)
        self.assertEqual(E.appraise(paper, s, inputs).status, "frontier")


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.ledger = Ledger(self.tmp / "ledger", {})

    def test_store_writes_record_and_lexicon(self):
        rec = decide(dive())
        rel = D.store(self.ledger, rec)
        self.assertEqual(rel, "evidence/deepdives/10-0-frontier.json")
        self.assertEqual(self.ledger.read_json(rel), rec)
        view = self.ledger.read_json(L.VIEW_PATH)
        ipr, mel = L.node_id(*IPRGC), L.node_id(*MEL)
        self.assertEqual(view["nodes"][ipr]["label"], "ipRGC signaling")
        self.assertEqual(view["links"][L.link_key(ipr, mel)]["verdict"], "supported")
        self.assertEqual(len(view["links"][L.link_key(ipr, mel)]["mechanism_of"]), 0)
        light = L.node_id(*LIGHT)
        self.assertEqual(view["links"][L.link_key(light, ipr)]["mechanism_of"][0]["paper"], "doi:10.0/frontier")
        self.assertEqual(L.open_keywords(view), ["iprgc", "melatonin onset"])

    def test_next_dive_starts_from_prior_verdicts(self):
        D.store(self.ledger, decide(dive()))
        view = L.load_view(self.ledger)
        later = dive()
        later["links"][2]["evidence"] = []  # the new paper cites nothing for l3 itself
        rec = decide(later, view=view)
        l3 = rec["links"][2]
        self.assertEqual((l3["certainty"], l3["human"], l3["prior"]), (D.UNSCORED_MAX, True, "supported"))
        self.assertEqual(rec["decision"], "n_of_1_candidate")
        self.assertEqual(decide(later)["decision"], "watch")

    def test_request_meta_carries_known_links(self):
        D.store(self.ledger, decide(dive()))
        meta = D.request_meta({"id": "c1", "title": "Synthetic: melatonin onset after evening light dimming"},
                              L.load_view(self.ledger))
        self.assertEqual(meta["task"], "deep_dive")
        self.assertEqual(meta["paper"]["id"], "c1")
        self.assertTrue(meta["known_links"])


if __name__ == "__main__":
    unittest.main()
