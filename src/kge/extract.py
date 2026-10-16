"""Extractors turn a source into *candidates*. Nothing here writes knowledge."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from kge.models import Candidate, EntityRef, EpistemicType, Evidence, LiteralValue, Source
from kge.ontology import Ontology


@dataclass
class ExtractionResult:
    candidates: list[Candidate]
    invalid: list[dict[str, Any]] = field(default_factory=list)
    raw_output: str | None = None


class Extractor(Protocol):
    id: str

    def extract(self, source: Source) -> ExtractionResult: ...


# ---------------------------------------------------------------- rule-based


_DATE = r"\d{4}-\d{2}-\d{2}"
_RULES: list[tuple[str, re.Pattern[str]]] = [
    (
        "works_for",
        re.compile(
            rf"^(?P<s>.+?)は(?P<o>.+?)に勤務している"
            rf"(?:（(?P<from>{_DATE})〜(?P<to>{_DATE})?）)?。$"
        ),
    ),
    ("located_in", re.compile(r"^(?P<s>.+?)は(?P<o>.+?)に所在する。$")),
    ("part_of", re.compile(r"^(?P<s>.+?)は(?P<o>.+?)に属する。$")),
    ("founded_in", re.compile(r"^(?P<s>.+?)は(?P<o>\d{4})年に設立された。$")),
]


class RuleExtractor:
    """Deterministic pattern extractor for simple Japanese registry sentences."""

    id = "rule:ja-registry-v1"
    source_prior = 0.95

    def __init__(self, ontology: Ontology) -> None:
        self.ontology = ontology

    def extract(self, source: Source) -> ExtractionResult:
        out: list[Candidate] = []
        invalid: list[dict[str, Any]] = []
        for n, line in enumerate(source.content.splitlines(), start=1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            for pred, rx in _RULES:
                m = rx.match(line)
                if not m:
                    continue
                pdef = self.ontology.predicates[pred]
                obj: EntityRef | LiteralValue
                if pdef.literal_range:
                    obj = LiteralValue(value=m["o"], datatype=pdef.range)
                else:
                    obj = EntityRef(label=m["o"], type=pdef.range)
                gd = m.groupdict()
                out.append(
                    Candidate(
                        subject=EntityRef(label=m["s"], type=pdef.domain),
                        predicate=pred,
                        object=obj,
                        epistemic_type=EpistemicType.FACT,
                        confidence=self.source_prior,
                        confidence_method="source_prior",
                        valid_from=dt.date.fromisoformat(gd["from"]) if gd.get("from") else None,
                        valid_to=dt.date.fromisoformat(gd["to"]) if gd.get("to") else None,
                        evidence=[
                            Evidence(
                                source_id=source.id,
                                source_version=source.version,
                                quote=line,
                                locator=f"line:{n}",
                            )
                        ],
                        extracted_by=self.id,
                    )
                )
                break
            else:
                invalid.append({"line": n, "text": line, "error": "no pattern matched"})
        return ExtractionResult(out, invalid)


# ---------------------------------------------------------------- LLM-based


class LLMClient(Protocol):
    model: str

    def complete(self, prompt: str) -> str: ...


class LLMReplayMissing(Exception):
    pass


def prompt_key(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]


class ReplayLLMClient:
    """Returns recorded responses keyed by prompt hash. Keeps runs offline and reproducible."""

    model = "replay"

    def __init__(self, fixtures_dir: str | Path) -> None:
        self.dir = Path(fixtures_dir)

    def complete(self, prompt: str) -> str:
        key = prompt_key(prompt)
        path = self.dir / f"{key}.json"
        if not path.exists():
            raise LLMReplayMissing(f"no recorded response for prompt {key} in {self.dir}")
        data = json.loads(path.read_text(encoding="utf-8"))
        return str(data["response"])


PROMPT_TEMPLATE = """\
あなたは知識抽出器です。次の文書から、以下の述語に当てはまる記述だけをJSONで抽出してください。
述語: {predicates}
各要素は subject, subject_type, predicate, object, object_type または object_datatype,
epistemic_type(fact|hypothesis), confidence(0-1), quote(文書からの逐語引用), valid_from, valid_to
を持ちます。文書に書かれていない推測は hypothesis とし、quote は空にしてください。
出力形式: {{"assertions": [...]}}

文書:
{document}
"""


class _LLMItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    subject: str = Field(min_length=1)
    subject_type: str | None = None
    predicate: str = Field(min_length=1)
    object: str = Field(min_length=1)
    object_type: str | None = None
    object_datatype: str | None = None
    epistemic_type: EpistemicType = EpistemicType.HYPOTHESIS
    confidence: float = Field(ge=0.0, le=1.0)
    quote: str = ""
    valid_from: dt.date | None = None
    valid_to: dt.date | None = None


class LLMExtractor:
    def __init__(self, client: LLMClient, ontology: Ontology) -> None:
        self.client = client
        self.ontology = ontology
        template_hash = hashlib.sha256(PROMPT_TEMPLATE.encode()).hexdigest()[:8]
        self.id = f"llm:{client.model}@{template_hash}"

    def prompt(self, source: Source) -> str:
        preds = ", ".join(
            f"{k}({v.domain}→{v.range})" for k, v in sorted(self.ontology.predicates.items())
        )
        return PROMPT_TEMPLATE.format(predicates=preds, document=source.content)

    def extract(self, source: Source) -> ExtractionResult:
        raw = self.client.complete(self.prompt(source))
        try:
            items = json.loads(raw)["assertions"]
            if not isinstance(items, list):
                raise TypeError("assertions is not a list")
        except (ValueError, KeyError, TypeError) as e:
            return ExtractionResult([], [{"error": f"unparseable LLM output: {e}"}], raw)
        out: list[Candidate] = []
        invalid: list[dict[str, Any]] = []
        for i, item in enumerate(items):
            try:
                it = _LLMItem.model_validate(item)
                obj: EntityRef | LiteralValue
                if it.object_datatype:
                    obj = LiteralValue(value=it.object, datatype=it.object_datatype)
                else:
                    obj = EntityRef(label=it.object, type=it.object_type)
                evidence = (
                    [Evidence(source_id=source.id, source_version=source.version, quote=it.quote)]
                    if it.quote
                    else []
                )
                out.append(
                    Candidate(
                        subject=EntityRef(label=it.subject, type=it.subject_type),
                        predicate=it.predicate,
                        object=obj,
                        epistemic_type=it.epistemic_type,
                        confidence=it.confidence,
                        confidence_method="llm_self_report",
                        valid_from=it.valid_from,
                        valid_to=it.valid_to,
                        evidence=evidence,
                        extracted_by=self.id,
                    )
                )
            except ValidationError as e:
                invalid.append({"index": i, "item": item, "error": e.errors(include_url=False)})
        return ExtractionResult(out, invalid, raw)
