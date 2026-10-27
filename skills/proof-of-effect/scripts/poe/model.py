"""Verdicts and findings shared by every checker."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict


class Verdict(str, Enum):
    EFFECTIVE = "EFFECTIVE"        # the tool's resolved config or behaviour changed
    NO_OP = "NO-OP"                # the tool resolves the same thing before and after
    BREAKS = "BREAKS"              # the tool worked before the change and fails after it
    UNKNOWN_KEY = "UNKNOWN-KEY"    # the tool (or the action's metadata) does not know the name
    NOT_READ = "NOT-READ"          # nothing loads this file or reads this variable
    PHANTOM_FLAG = "PHANTOM-FLAG"  # the installed CLI's --help does not list the flag
    EXISTS = "EXISTS"              # a static check passed; the effect itself was not measured
    UNCLEAR = "UNCLEAR"            # the config changed, but not visibly through this key
    UNVERIFIABLE = "UNVERIFIABLE"  # no checker or no tool available here: run a probe


PROBLEMS = frozenset({Verdict.NO_OP, Verdict.BREAKS, Verdict.UNKNOWN_KEY,
                      Verdict.NOT_READ, Verdict.PHANTOM_FLAG})
NEEDS_PROBE = frozenset({Verdict.UNCLEAR, Verdict.UNVERIFIABLE})

ORDER = [Verdict.BREAKS, Verdict.UNKNOWN_KEY, Verdict.NO_OP, Verdict.NOT_READ,
         Verdict.PHANTOM_FLAG, Verdict.UNCLEAR, Verdict.UNVERIFIABLE,
         Verdict.EFFECTIVE, Verdict.EXISTS]


@dataclass
class Finding:
    verdict: Verdict
    checker: str
    file: str
    subject: str
    evidence: str
    change: str = "changed"  # added | removed | changed | file
    detail: str = ""

    @property
    def is_problem(self) -> bool:
        # Removing a key that did nothing is a harmless clean-up, not a failed fix.
        if self.verdict is Verdict.NO_OP and self.change == "removed":
            return False
        return self.verdict in PROBLEMS

    @property
    def needs_probe(self) -> bool:
        return self.verdict in NEEDS_PROBE

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "checker": self.checker,
            "file": self.file,
            "subject": self.subject,
            "change": self.change,
            "evidence": self.evidence,
            "detail": self.detail,
            "problem": self.is_problem,
            "needs_probe": self.needs_probe,
        }
