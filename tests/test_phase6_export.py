"""Phase 6: RDF/PROV-O export is valid Turtle and keeps provenance queryable."""

import rdflib
from rdflib.namespace import PROV, RDF

from kge import demo
from kge.export import DEFAULT_BASE, to_turtle

KGE = rdflib.Namespace(DEFAULT_BASE)
KGO = rdflib.Namespace(DEFAULT_BASE + "ontology/")
KGS = rdflib.Namespace(DEFAULT_BASE + "schema#")


def _graph(**kw):  # type: ignore[no-untyped-def]
    e = demo.run(emit=lambda _: None)
    g = rdflib.Graph()
    g.parse(data=to_turtle(e.store, **kw), format="turtle")
    return e, g


def test_export_is_valid_turtle_and_deterministic() -> None:
    e1 = demo.run(emit=lambda _: None)
    e2 = demo.run(emit=lambda _: None)
    assert to_turtle(e1.store) == to_turtle(e2.store)
    _, g = _graph()
    assert len(g) > 100


def test_only_confirmed_knowledge_is_asserted_directly() -> None:
    e, g = _graph()
    asserted = set(g.triples((None, KGO.located_in, None)))
    acme = e.store.find_entity_by_label("Acme社")
    germany = e.store.find_entity_by_label("ドイツ")
    japan = e.store.find_entity_by_label("日本")
    assert (KGE[acme.id], KGO.located_in, KGE[germany.id]) in asserted
    assert (KGE[acme.id], KGO.located_in, KGE[japan.id]) not in asserted
    # the retracted statement is still there, reified, with its invalidation time
    old = next(
        s for s in g.subjects(RDF.object, KGE[japan.id])
        if (s, RDF.predicate, KGO.located_in) in g
    )  # fmt: skip
    assert (old, KGS.status, rdflib.Literal("retracted")) in g
    assert g.value(old, PROV.invalidatedAtTime) is not None
    assert g.value(old, PROV.wasInvalidatedBy) is not None


def test_provenance_queries() -> None:
    _, g = _graph()
    facts_without_source = g.query(
        """SELECT ?s WHERE { ?s a kgs:Fact . FILTER NOT EXISTS { ?s prov:wasDerivedFrom ?src } }""",
        initNs={"kgs": KGS, "prov": PROV},
    )
    assert list(facts_without_source) == []
    inferences = g.query(
        """SELECT ?s ?rule (COUNT(?p) AS ?n) WHERE {
             ?s a kgs:Inference ; kgs:rule ?rule ; prov:wasDerivedFrom ?p .
             ?p a rdf:Statement } GROUP BY ?s ?rule""",
        initNs={"kgs": KGS, "prov": PROV, "rdf": RDF},
    )
    rows = list(inferences)
    assert rows and all(int(r[2]) == 2 for r in rows)  # both rules have two premises
    llm = g.query(
        """SELECT ?s ?who WHERE { ?s prov:wasAttributedTo ?a ; kgs:approvedBy ?h .
             ?a rdfs:label ?who . ?h a prov:Person . FILTER STRSTARTS(STR(?who), "llm:") }""",
        initNs={"kgs": KGS, "prov": PROV},
    )
    assert len(list(llm)) == 1  # the one LLM fact, approved by a person


def test_active_only_and_escaping() -> None:
    _, g = _graph(include_history=False)
    statuses = {str(o) for o in g.objects(None, KGS.status)}
    assert statuses == {"accepted"}
    from kge.export import _lit

    assert _lit('a"b\\c\nd') == '"a\\"b\\\\c\\nd"'


def test_local_names_are_injective_and_decimals_have_no_exponent() -> None:
    from kge.export import _decimal, _local

    names = ["human:田中", "human:佐藤", "human:alice.smith", "human:alice_smith"]
    assert len({_local(n) for n in names}) == len(names)
    ttl = "@prefix kge: <urn:kge:> .\n" + "".join(
        f"kge:agent_{_local(n)} a kge:A .\n" for n in names
    )
    assert len(set(rdflib.Graph().parse(data=ttl, format="turtle").subjects())) == len(names)
    assert _decimal(1e-07) == "0.0000001" and _decimal(0.7) == "0.7" and _decimal(1.0) == "1.0"


def test_each_quote_stays_with_its_source() -> None:
    e, g = _graph()
    rows = list(
        g.query(
            """SELECT ?a ?src ?q WHERE { ?a kgs:evidence ?ev .
                 ?ev prov:wasDerivedFrom ?src ; kgs:quote ?q }""",
            initNs={"kgs": KGS, "prov": PROV},
        )
    )
    expected = {
        (str(KGE[a.id]), str(KGE[f"{ev.source_id}_v{ev.source_version}"]), ev.quote)
        for a in e.store.active(include_hypotheses=True)
        for ev in a.evidence
    }
    assert {(str(a), str(s), str(q)) for a, s, q in rows} >= expected and expected
