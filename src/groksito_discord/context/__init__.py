"""
Context package for Groksito.

Public API is re-exported here from .core (implementation) and .context_summarizer.

Public API re-exported from core.py (state, persistence, quotas).

Import-time side effects (context load) still occur on first import of the
package (via core), but are now contained. Callers using `from . import context`
or `from .context import ...` continue to work unchanged.
"""

from __future__ import annotations

# Re-export the core implementation (state, updates, classification, persistence, quotas, etc.)
# Using * for full compatibility with all previous direct imports from the flat context module.
# Private names (starting with _) are still accessible as they were before.
from .core import *

# Re-export the summarizer (heavy OpenAI usage kept lazy inside its functions;
# callers should prefer explicit import from .context.context_summarizer when possible).
from .context_summarizer import (
    summarize_recent_conversation,
    format_recent_context_block,
)

# Note: The bulk of the original logic (state, load/save on import, classify, etc.)
# now lives in core.py. This __init__.py is intentionally thin for a proper package.
# Import-time side effects are still present (via `import .core` triggering load).
# See core.py docstring for details.
