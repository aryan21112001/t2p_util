"""Power BI constraint rules — the register of what Desktop's editor enforces."""

from .registry import (  # noqa: F401
    RULES,
    Rule,
    by_id,
    for_check,
    implemented,
    gaps,
    coverage,
)
