"""The research layer — what the agent is given, and what it may hand back.

Nothing in this package talks to a model or to a network. It is the vocabulary
the agent works in: a requirement it is asked to satisfy, and (later) the
proposals it returns. Keeping that vocabulary here, testable without an API
key, is what stops "did the model get it wrong?" and "did klm state it wrong?"
from being the same question.
"""

from __future__ import annotations
