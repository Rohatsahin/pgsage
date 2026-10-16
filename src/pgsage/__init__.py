from importlib.metadata import PackageNotFoundError, version

from pgsage.context import inspect_query
from pgsage.plan import ExplainResult, PlanNode, parse_explain, plan_digest
from pgsage.verify import (
    IndexSuggestion,
    StatsSuggestion,
    VerifyResult,
    verify_index,
    verify_rewrite,
    verify_statistics,
)

try:
    __version__ = version("pgsage")
except PackageNotFoundError:
    __version__ = "0+unknown"

__all__ = [
    "ExplainResult",
    "IndexSuggestion",
    "PlanNode",
    "StatsSuggestion",
    "VerifyResult",
    "__version__",
    "inspect_query",
    "parse_explain",
    "plan_digest",
    "verify_index",
    "verify_rewrite",
    "verify_statistics",
]
