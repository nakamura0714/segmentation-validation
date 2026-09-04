"""採否判断と開発用データセットの生成。

Issue（問題の候補）と SelectionDecision（採否）を分けるのがこのパッケージの要点。
"""

from .decisions import (
    AutomaticDecision,
    Decision,
    DecisionSource,
    HumanDecision,
    ReviewStatus,
    SelectionDecision,
    assert_invariants,
    build_decisions,
    summarize,
)

__all__ = [
    "AutomaticDecision",
    "Decision",
    "DecisionSource",
    "HumanDecision",
    "ReviewStatus",
    "SelectionDecision",
    "assert_invariants",
    "build_decisions",
    "summarize",
]
