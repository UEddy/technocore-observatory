"""Technocore Observatory sampler.

Step 1 of the build order only: fetch raw responses and archive them verbatim
as NDJSON. No parsing lives in this package yet, by design.
"""

__all__ = ["archive", "backoff", "budget", "transport", "fetcher"]
