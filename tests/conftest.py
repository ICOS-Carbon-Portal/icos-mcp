"""Every test runs over RECORDED proxy answers (tests/cassette), so a
result is the same tomorrow as today and needs no network.

  uv run pytest                                  # replay (the default)
  ICOS_CASSETTE_MODE=record uv run pytest        # re-record from the live proxy
  ICOS_LIVE=1 uv run pytest                      # live, no recording

The recordings are also the input the M9 port is replayed over.
"""

import os

HERE = os.path.dirname(__file__)
if os.environ.get("ICOS_LIVE") != "1":
    os.environ.setdefault("ICOS_CASSETTE", os.path.join(HERE, "cassette"))
