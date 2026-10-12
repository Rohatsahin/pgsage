from importlib.metadata import PackageNotFoundError, version

from pgsage.ai import Action, Explanation, explain_plan
from pgsage.plan import ExplainResult, PlanNode, parse_explain, plan_digest
from pgsage.tune import TuneReport, tune
from pgsage.verify import IndexSuggestion, VerifyResult, verify_index

try:
    __version__ = version("pgsage")
except PackageNotFoundError:
    __version__ = "0+unknown"

__all__ = [
    "Action",
    "ExplainResult",
    "Explanation",
    "IndexSuggestion",
    "PlanNode",
    "TuneReport",
    "VerifyResult",
    "__version__",
    "explain_plan",
    "parse_explain",
    "plan_digest",
    "tune",
    "verify_index",
]
