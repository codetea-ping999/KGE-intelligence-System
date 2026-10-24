"""Forward-chaining reasoner. Every inference records the rule and premises it came from."""

from __future__ import annotations

from collections.abc import Iterator

from kge.models import Assertion, EpistemicType, Justification, LiteralValue, interval_intersection
from kge.ontology import Ontology, RuleDef, parse_pattern
from kge.store import AddAssertion, CommitResult, EntityIdRef, Store

REASONER = "reasoner"
APPROVER = "policy:reasoner"


def _is_var(term: str) -> bool:
    return term.startswith("?")


class Reasoner:
    def __init__(self, store: Store, ontology: Ontology) -> None:
        self.store = store
        self.ontology = ontology

    def _matches(
        self, rule: RuleDef, facts: dict[str, list[Assertion]]
    ) -> Iterator[tuple[dict[str, str], list[Assertion]]]:
        patterns = [parse_pattern(p) for p in rule.if_]

        def walk(
            i: int, binding: dict[str, str], used: list[Assertion]
        ) -> Iterator[tuple[dict[str, str], list[Assertion]]]:
            if i == len(patterns):
                yield binding, used
                return
            s, p, o = patterns[i]
            for a in facts.get(p, []):
                b = dict(binding)
                ok = True
                for term, value in ((s, a.subject), (o, a.object_value)):
                    if _is_var(term):
                        if b.setdefault(term, value) != value:
                            ok = False
                    elif term != value:
                        ok = False
                if ok:
                    yield from walk(i + 1, b, [*used, a])

        yield from walk(0, {}, [])

    def derive(self) -> list[AddAssertion]:
        """One round of rule application over current non-hypothetical knowledge."""
        facts: dict[str, list[Assertion]] = {}
        for a in self.store.active(include_hypotheses=False):
            facts.setdefault(a.predicate, []).append(a)
        ops: list[AddAssertion] = []
        planned: set[tuple[str, str, str, str, str]] = set()
        for rule in self.ontology.all_rules():
            ts, tp, to = parse_pattern(rule.then)
            pdef = self.ontology.predicates[tp]
            for binding, premises in self._matches(rule, facts):
                window = interval_intersection([(a.valid_from, a.valid_to) for a in premises])
                if window is None:
                    continue
                subj, obj = binding.get(ts, ts), binding.get(to, to)
                if not self._fits(subj, pdef.domain) or (
                    not pdef.literal_range and not self._fits(obj, pdef.range)
                ):
                    continue  # the ontology does not allow this conclusion for these entities
                vf, vt = window
                key = (subj, tp, obj, str(vf), str(vt))
                if key in planned or self._known(subj, tp, obj, vf, vt):
                    continue
                planned.add(key)
                confidence = round(min(a.confidence for a in premises) * rule.confidence_factor, 4)
                ops.append(
                    AddAssertion(
                        subject=EntityIdRef(id=subj),
                        predicate=tp,
                        object=(
                            LiteralValue(value=obj, datatype=pdef.range)
                            if pdef.literal_range
                            else EntityIdRef(id=obj)
                        ),
                        epistemic_type=EpistemicType.INFERENCE,
                        confidence=confidence,
                        confidence_method=f"rule_min*{rule.confidence_factor}",
                        valid_from=vf,
                        valid_to=vt,
                        extracted_by=f"{REASONER}:{rule.id}",
                        approved_by=APPROVER,
                        justification=Justification(
                            rule_id=rule.id, premise_ids=[a.id for a in premises]
                        ),
                    )
                )
        return ops

    def _fits(self, entity_id: str, expected: str) -> bool:
        e = self.store.get_entity(entity_id)
        return e is not None and self.ontology.conforms(e.types, expected)

    def _known(self, s: str, p: str, o: str, vf: object, vt: object) -> bool:
        return any(
            a.object_value == o and a.valid_from == vf and a.valid_to == vt
            for a in self.store.active(subject=s, predicate=p, include_hypotheses=True)
        )

    def run(self, max_rounds: int = 10) -> list[CommitResult]:
        """Apply rules until fixpoint; one changeset per round."""
        results = []
        for n in range(1, max_rounds + 1):
            ops = self.derive()
            if not ops:
                break
            results.append(self.store.commit(REASONER, f"inference round {n}", ops))
        return results
