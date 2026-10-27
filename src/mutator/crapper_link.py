"""Import crapper from the environment or the sibling checkout.

Function names and namespaces come from crapper so `.metrics/mutate` joins
the same operations uml-viewer joins to `.metrics/crap.edn`.
"""

from __future__ import annotations

import sys
from pathlib import Path


def _sibling_src() -> Path:
    project = Path(__file__).resolve().parents[2]
    return project.parent / "crapper" / "src"


def ensure_crapper():
    try:
        import crapper
    except ImportError:
        sibling = _sibling_src()
        if not (sibling / "crapper").is_dir():
            raise ImportError(
                "crapper is not installed and was not found at "
                f"{sibling}. Clone https://github.com/ericeil/crapper next to "
                "mutator. Kotlin mutation sites use that checkout's function names."
            ) from None
        sys.path.insert(0, str(sibling))
        import crapper
    import crapper.coverage
    import crapper.discover
    import crapper.languages
    import crapper.languages.treesitter
    import crapper.runners

    return crapper
