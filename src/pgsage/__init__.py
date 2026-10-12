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
    "__version__",
    "Action",
    "ExplainResult",
    "Explanation",
    "IndexSuggestion",
    "PlanNode",
    "TuneReport",
    "VerifyResult",
    "explain_plan",
    "parse_explain",
    "plan_digest",
    "tune",
    "verify_index",
]
