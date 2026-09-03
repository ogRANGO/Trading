"""Fetchers. Each module exposes ``fetch(...) -> SourceResult`` and does GET-only
network with short timeouts. A failure degrades exactly one lane."""
