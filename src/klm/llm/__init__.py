"""The model layer — one seam between klm and the Anthropic SDK.

Everything above this package speaks in klm's own small types, so the research
agent can be tested without an API key and without the SDK installed. That is
the same rule the supplier layer follows for HTTP, and for the same reason: a
component you can only exercise by paying for it is a component nobody
exercises.
"""

from __future__ import annotations
