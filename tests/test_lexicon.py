"""Tests for homunculus.lexicon. Synthetic labels and paper keys only; no network."""

from __future__ import annotations

import random
import shutil
import tempfile
import unittest
from pathlib import Path

from homunculus import lexicon as L
from homunculus.store import Ledger

AT = "2026-11-20T03:00:00Z"
LATER = "2026-11-27T03:00:00Z"
LIGHT = L.node_id("intervention", "Evening light dimming")
MEL = L.node_id("mechanism", "Melatonin onset")
SLEEP = L.node_id("outcome", "Sleep onset")


def nodes(at: str = AT) -> list[dict]:
    return [L.make_node("intervention", "Evening light dimming", at), L.make_node("mechanism", "Melatonin onset", at),
            L.make_node("outcome", "Sleep onset", at)]


def support(paper: str, certainty: float, human: bool, src: str = LIGHT, dst: str = MEL, at: str = AT,
            rel: str = "supports") -> dict:
    return L.make_edge(rel, src, dst, paper, at, certainty=certainty, human=human)


class IdentifierTest(unittest.TestCase):
    def test_node_id(self):
        self.assertEqual(LIGHT, "intervention:evening-light-dimming")
        self.assertEqual(L.node_id("mechanism", "  Ä-Wert / AMPK  "), "mechanism:a-wert-ampk")
        for args in (("gene", "x"), ("concept", "!!!")):
            with self.subTest(args=args), self.assertRaises(ValueError):
                L.node_id(*args)

    def test_paper_node(self):
        self.assertEqual(L.paper_node(" doi:10.1/x "), "paper:doi:10.1/x")
        with self.assertRaises(ValueError):
            L.paper_node(" ")


class EventTest(unittest.TestCase):
    def test_edges_need_matching_ends(self):
        with self.assertRaises(ValueError):
            L.make_edge("supports", "paper:doi:a", MEL, "doi:a", AT)
        with self.assertRaises(ValueError):
            L.make_edge("replicates", LIGHT, MEL, "doi:a", AT)
        with self.assertRaises(ValueError):
            L.make_edge("causes", LIGHT, MEL, "doi:a", AT)
        with self.assertRaises(ValueError):
            L.make_edge("supports", LIGHT, MEL, "doi:a", AT, certainty=4.5)
        with self.assertRaises(ValueError):
            L.make_edge("supports", LIGHT, MEL, "", AT)
        ok = L.make_edge("replicates", L.paper_node("doi:b"), L.paper_node("doi:a"), "doi:b", AT)
        self.assertEqual(ok["kind"], "edge")

    def test_node_id_must_match_label(self):
        bad = {**L.make_node("mechanism", "Melatonin onset", AT), "id": "mechanism:other"}
        with self.assertRaises(ValueError):
            L.check_event(bad)
        with self.assertRaises(ValueError):
            L.check_event({"schema": 1, "kind": "gossip", "at": AT})

    def test_question_keywords_are_normalized(self):
        q = L.make_question(MEL, SLEEP, "Gilt das bei Menschen?", ["Melatonin", "melatonin ", "", "Sleep-Onset"], AT)
        self.assertEqual(q["keywords"], ["melatonin", "sleep onset"])


class FoldTest(unittest.TestCase):
    def events(self) -> list[dict]:
        return [
            *nodes(),
            support("doi:h1", 2.0, True),
            support("doi:a1", 0.5, False),
            support("doi:c1", 1.0, True, MEL, SLEEP),
            support("doi:c2", 1.0, True, MEL, SLEEP, rel="contradicts"),
            L.make_edge("mechanism_of", MEL, SLEEP, "doi:h1", AT),
            L.make_question(MEL, SLEEP, "Wirkt das beim Menschen?", ["melatonin", "sleep"], AT),
            L.make_question(LIGHT, MEL, "Dosis beim Menschen?", ["light"], AT),
        ]

    def test_verdicts(self):
        view = L.fold(self.events())
        light = view["links"][L.link_key(LIGHT, MEL)]
        self.assertEqual((light["support"], light["human"], light["verdict"]), (2.0, True, "supported"))
        self.assertEqual(len(light["supports"]), 2)
        sleep = view["links"][L.link_key(MEL, SLEEP)]
        self.assertEqual(sleep["verdict"], "contested")
        self.assertEqual(sleep["mechanism_of"], [{"paper": "doi:h1", "certainty": None, "human": False}])
        self.assertEqual(view["nodes"][MEL], {"type": "mechanism", "label": "Melatonin onset"})

    def test_open_link_has_only_mechanism_edges(self):
        view = L.fold([L.make_edge("mechanism_of", LIGHT, MEL, "doi:m", AT)])
        self.assertEqual(view["links"][L.link_key(LIGHT, MEL)]["verdict"], "open")

    def test_fold_is_order_independent(self):
        events = self.events()
        expected = L.fold(events)
        rng = random.Random(5)
        for _ in range(10):
            rng.shuffle(events)
            self.assertEqual(L.fold(events), expected)

    def test_last_edge_per_paper_wins(self):
        view = L.fold([support("doi:h1", 1.0, True), support("doi:h1", 3.0, True, at=LATER)])
        link = view["links"][L.link_key(LIGHT, MEL)]
        self.assertEqual((len(link["supports"]), link["support"]), (1, 3.0))

    def test_retraction_drops_every_edge_of_the_paper(self):
        retract = L.make_edge("retracts", L.paper_node("crossref:notice"), L.paper_node("doi:h1"), "crossref:notice",
                              AT)
        replicated = L.make_edge("replicates", L.paper_node("doi:h2"), L.paper_node("doi:h1"), "doi:h2", LATER)
        view = L.fold([support("doi:h1", 2.0, True, at=LATER), support("doi:a1", 0.5, False), retract, replicated])
        link = view["links"][L.link_key(LIGHT, MEL)]
        self.assertEqual([e["paper"] for e in link["supports"]], ["doi:a1"])
        self.assertEqual((link["support"], link["human"]), (0.5, False))
        self.assertEqual(view["retracted"], ["doi:h1"])
        self.assertEqual(view["replications"], {})

    def test_replications(self):
        events = [L.make_edge("replicates", L.paper_node(f"doi:r{i}"), L.paper_node("doi:h1"), f"doi:r{i}", AT)
                  for i in range(2)]
        view = L.fold(events)
        self.assertEqual(view["replications"], {"doi:h1": ["doi:r0", "doi:r1"]})
        self.assertEqual(L.replications(view, "doi:h1"), 2)
        self.assertEqual(L.replications(view, "doi:none"), 0)
        self.assertEqual(L.replications(None, "doi:h1"), 0)

    def test_questions_close_with_human_support(self):
        view = L.fold(self.events())
        self.assertEqual([q["text"] for q in view["questions"]], ["Wirkt das beim Menschen?"])
        dup = L.make_question(MEL, SLEEP, "wirkt das beim menschen", ["x"], LATER)
        self.assertEqual(len(L.fold([*self.events(), dup])["questions"]), 1)

    def test_invalid_event_raises(self):
        with self.assertRaises(ValueError):
            L.fold([{"schema": 1, "kind": "edge", "at": AT, "rel": "supports"}])


class QueryTest(unittest.TestCase):
    def setUp(self):
        self.view = L.fold([
            *nodes(), support("doi:h1", 2.0, True),
            L.make_question(MEL, SLEEP, "Gilt die Kette beim Menschen?", ["melatonin onset", "sleep latency"], AT),
            L.make_question(LIGHT, SLEEP, "Welche Dosis?", ["evening light"], AT),
        ])

    def test_lookup(self):
        verdict = L.lookup(self.view, LIGHT, MEL)
        self.assertEqual(verdict, L.LinkVerdict(2.0, 0.0, True, "supported", ("doi:h1",)))
        self.assertIsNone(L.lookup(self.view, MEL, LIGHT))
        self.assertIsNone(L.lookup(None, LIGHT, MEL))

    def test_related_links_match_labels_in_text(self):
        hits = L.related_links(self.view, "Synthetic trial: melatonin onset after dimming")
        self.assertEqual([(h["src"], h["dst"]) for h in hits], [(LIGHT, MEL)])
        self.assertEqual(L.related_links(self.view, "unrelated title"), [])

    def test_export_keywords(self):
        self.assertEqual(L.open_keywords(self.view), ["evening light", "melatonin onset", "sleep latency"])
        export = L.export_keywords(self.view)
        self.assertEqual(set(export), set(L.SCOUTS))
        self.assertEqual(sorted(export["retina"]["queries"]), ['"evening light"', '"melatonin onset" AND "sleep latency"'])
        self.assertEqual(export["papilla"]["terms"], export["retina"]["terms"])
        self.assertEqual(L.export_keywords(None, ("retina",)), {"retina": {"terms": [], "queries": []}})

    def test_merge_keywords_keeps_existing(self):
        config = {"retina": {"queries": ["sleep AND exercise"], "terms": {"evening light": 3.0}},
                  "spindle": {"terms": ["hrv"]}}
        merged = L.merge_keywords(config, L.export_keywords(self.view, ("retina", "spindle")))
        self.assertEqual(merged["retina"]["terms"]["evening light"], 3.0)
        self.assertEqual(merged["retina"]["terms"]["sleep latency"], 1.0)
        self.assertEqual(merged["retina"]["queries"][0], "sleep AND exercise")
        self.assertEqual(merged["spindle"]["terms"][:2], ["hrv", "evening light"])
        self.assertEqual(config["spindle"]["terms"], ["hrv"])


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.ledger = Ledger(self.tmp / "ledger", {})

    def test_append_fold_and_view(self):
        self.assertEqual(L.append(self.ledger, [*nodes(), support("doi:h1", 2.0, True)]), 4)
        self.assertEqual(len(self.ledger.read_jsonl(L.EVENTS_PATH)), 4)
        rel = L.rebuild_view(self.ledger)
        self.assertEqual(rel, L.VIEW_PATH)
        self.assertEqual(self.ledger.read_json(rel), L.load_view(self.ledger))
        self.assertEqual(L.lookup(self.ledger.read_json(rel), LIGHT, MEL).support, 2.0)

    def test_invalid_batch_writes_nothing(self):
        with self.assertRaises(ValueError):
            L.append(self.ledger, [nodes()[0], {"schema": 1, "kind": "node", "at": AT}])
        self.assertEqual(self.ledger.read_jsonl(L.EVENTS_PATH), [])

    def test_empty_ledger_gives_empty_view(self):
        view = L.load_view(self.ledger)
        self.assertEqual((view["links"], view["questions"], view["retracted"]), ({}, [], []))


if __name__ == "__main__":
    unittest.main()
