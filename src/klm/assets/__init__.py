"""Acquiring and checking the files klm manages on a part's behalf.

See docs/08-asset-pipeline.md. The order things are tried in — catalog, then
KiCad's own libraries, then a template — is in `klm.services.assets`; this
package holds the pieces each of those steps needs.
"""

from klm.assets.packages import Package, find_package, normalize_package
from klm.assets.qa import QaReport, QaResult, QaStatus, check_footprint, check_model3d, check_symbol

__all__ = [
    "Package",
    "QaReport",
    "QaResult",
    "QaStatus",
    "check_footprint",
    "check_model3d",
    "check_symbol",
    "find_package",
    "normalize_package",
]
