"""Tree-sitter grammar registry: load once, warn (not crash) on missing grammars (FR-27)."""

import logging
from functools import lru_cache

from tree_sitter import Language

logger = logging.getLogger(__name__)

# CodeContext.language values that map to a real grammar (excludes "unsupported").
_GRAMMAR_MODULES = {
    "python": "tree_sitter_python",
    "javascript": "tree_sitter_javascript",
    "go": "tree_sitter_go",
    "csharp": "tree_sitter_c_sharp",
    "rust": "tree_sitter_rust",
}


@lru_cache(maxsize=1)
def load_grammars() -> dict[str, Language | None]:
    """Import every supported grammar; a missing/broken one logs a warning and maps to None.

    Cached: import + Language() construction is startup-check work, not per-request work.
    """
    registry: dict[str, Language | None] = {}
    for lang, module_name in _GRAMMAR_MODULES.items():
        try:
            module = __import__(module_name)
            registry[lang] = Language(module.language())
        except Exception:
            logger.warning(
                "tree-sitter grammar unavailable for %s (%s)", lang, module_name, exc_info=True
            )
            registry[lang] = None
    return registry
