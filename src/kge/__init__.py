"""Knowledge Graph Engineering PoC.

The public, embeddable API is :class:`KnowledgeGraph` plus the interchange format in
:mod:`kge.interchange`; see the README section "他のシステムへの組み込み".
"""

from kge.api import KnowledgeGraph
from kge.extract import ExtractionFailed, LLMReplayMissing
from kge.interchange import (
    SCHEMA,
    VERSION,
    InterchangeError,
    Submission,
    SubmittedSource,
    SubmittedStatement,
)
from kge.models import Assertion, CandidateState, EpistemicType, StagedCandidate, Status
from kge.pipeline import IngestResult, ReviewError
from kge.store import OntologyMismatch, UnauthorizedCommit

__version__ = "0.2.0"

__all__ = [
    "SCHEMA",
    "VERSION",
    "Assertion",
    "CandidateState",
    "EpistemicType",
    "ExtractionFailed",
    "IngestResult",
    "InterchangeError",
    "KnowledgeGraph",
    "LLMReplayMissing",
    "OntologyMismatch",
    "ReviewError",
    "StagedCandidate",
    "Status",
    "Submission",
    "SubmittedSource",
    "SubmittedStatement",
    "UnauthorizedCommit",
]
