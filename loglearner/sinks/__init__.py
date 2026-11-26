"""Output sinks for LogLearner.

Sinks consume verified rules from the pipeline and write them to external
systems (Hindsight today; planned: HTTP, S3, JSONL streams later).

All sinks are optional dependencies — importing this module never fails
even when no sink backend is installed. Each sink module gates its own
backend import.
"""

from .hindsight_sink import retain_to_hindsight, hindsight_available

__all__ = ["retain_to_hindsight", "hindsight_available"]
