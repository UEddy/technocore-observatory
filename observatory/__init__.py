"""Technocore Observatory.

Build steps 1 to 3: a sampler that fetches raw responses and archives them
verbatim as NDJSON, a parser that reads that archive, and a loader that builds
a SQLite database out of it.

The two halves stay separate on purpose. The fetcher knows nothing about the
response format, so a format change can never cost a snapshot, and the parser
never touches the network.
"""

__all__ = ["archive", "backoff", "budget", "transport", "fetcher", "parser", "store"]
