"""A fake OpenAI-compatible streaming server, for testing the harness without a GPU (#28).

The point of this file is one detail, and it is worth stating before the code:

**Token chunks carry `"usage": null`.**

That is what vLLM emits under `stream_options={"include_usage": True}`, and it is what broke
this project on 2026-08-04: the driver classified chunks with `'"usage"' in data`, which
matched *every* chunk, so it `continue`d past all of them and recorded **zero TTFT across 500
requests** — while the final chunk still populated the token counts, so every CSV looked
plausible. A stub that omitted the key would let that bug pass, which would make this fixture
worse than nothing: a test that reassures without testing.

So the contract below is not "close enough to vLLM". It is specifically the shape that
produced the failure, plus the delays needed to make TTFT and ITL *known values* rather than
merely non-zero — because "non-zero" would also have passed for a driver that timestamped the
wrong chunk.

Response format per request:

    data: {"choices":[{"text":" tok","index":0}],"usage":null}     x max_tokens
    data: {"choices":[],"usage":{"prompt_tokens":P,"completion_tokens":N}}
    data: [DONE]

Stdlib only: a test fixture should not add a dependency to `requirements.txt`, and the
reproducibility claim is stronger if the test path needs nothing beyond the runtime one.

Usage (also runnable by hand for debugging):
    python3 fake_vllm.py --port 8099 --ttft-ms 40 --itl-ms 5
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional, Tuple

# Defaults are deliberately far apart so a swapped TTFT/ITL reading is obvious in an
# assertion failure rather than plausible.
DEFAULT_TTFT_MS = 40.0
DEFAULT_ITL_MS = 5.0


class _Handler(BaseHTTPRequestHandler):
    # Injected by make_server()
    ttft_ms: float = DEFAULT_TTFT_MS
    itl_ms: float = DEFAULT_ITL_MS
    fail_every: int = 0  # if > 0, every Nth request returns HTTP 500
    _seen = 0
    _lock = threading.Lock()

    def log_message(self, *args):  # noqa: D102 - silence per-request stderr noise
        pass

    def _sse(self, obj: dict) -> None:
        self.wfile.write(f"data: {json.dumps(obj)}\n\n".encode())
        self.wfile.flush()

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's API
        if not self.path.endswith("/v1/completions"):
            self.send_error(404)
            return

        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        max_tokens = int(body.get("max_tokens", 16))
        prompt_tokens = max(1, len(str(body.get("prompt", "")).split()))

        with _Handler._lock:
            _Handler._seen += 1
            n = _Handler._seen
        if self.fail_every and n % self.fail_every == 0:
            # Body-carrying 5xx: the driver reads the body for the error column, and #21's
            # router traceback arrived exactly this way.
            self.send_response(500)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"fake_vllm: injected failure")
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        time.sleep(self.ttft_ms / 1000.0)
        for i in range(max_tokens):
            if i:
                time.sleep(self.itl_ms / 1000.0)
            # "usage": null on EVERY token chunk - see the module docstring.
            self._sse({"choices": [{"text": f" t{i}", "index": 0}], "usage": None})

        # The usage-only chunk: empty choices, so it is neither a TTFT nor an ITL.
        self._sse({
            "choices": [],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": max_tokens},
        })
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def make_server(port: int = 0, ttft_ms: float = DEFAULT_TTFT_MS,
                itl_ms: float = DEFAULT_ITL_MS,
                fail_every: int = 0) -> Tuple[ThreadingHTTPServer, int]:
    """Return an unstarted server and the port it bound. `port=0` picks a free one."""
    handler = type("_Bound", (_Handler,), {
        "ttft_ms": ttft_ms, "itl_ms": itl_ms, "fail_every": fail_every,
    })
    handler._seen = 0
    srv = ThreadingHTTPServer(("127.0.0.1", port), handler)
    return srv, srv.server_address[1]


class running:
    """Context manager: serve in a background thread, shut down on exit.

    ```python
    with running(ttft_ms=40, itl_ms=5) as url:
        ...  # url is http://127.0.0.1:<port>
    ```
    """

    def __init__(self, **kw):
        self._kw = kw
        self._srv: Optional[ThreadingHTTPServer] = None

    def __enter__(self) -> str:
        self._srv, port = make_server(**self._kw)
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{port}"

    def __exit__(self, *exc):
        assert self._srv is not None
        self._srv.shutdown()
        self._srv.server_close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--port", type=int, default=8099)
    p.add_argument("--ttft-ms", type=float, default=DEFAULT_TTFT_MS)
    p.add_argument("--itl-ms", type=float, default=DEFAULT_ITL_MS)
    p.add_argument("--fail-every", type=int, default=0)
    a = p.parse_args()
    srv, port = make_server(a.port, a.ttft_ms, a.itl_ms, a.fail_every)
    print(f"fake vLLM on http://127.0.0.1:{port} "
          f"(ttft {a.ttft_ms} ms, itl {a.itl_ms} ms, fail_every {a.fail_every})")
    srv.serve_forever()
