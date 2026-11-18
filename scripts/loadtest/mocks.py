"""Local stand-ins for the load test. Nothing here makes an outside call.

    python mocks.py openai  <port>   # embeddings + Responses API (whole and streamed)
    python mocks.py storage <port>   # the Supabase Storage REST calls the API makes
    python mocks.py static  <port> <dir>

OpenAI stand-in: POST /control {"embed_delay": s, "chunk_delay": s, "first_delay": s} sets
latencies; GET /stats returns call counts. Defaults model a fast provider (first token after
0.3 s, 30 pieces 20 ms apart) so the test measures this system, not a provider.
"""

import hashlib
import json
import math
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler, ThreadingHTTPServer
from functools import partial

DIMENSIONS = 1536
state = {
    "embed_delay": 0.05,
    "chunk_delay": 0.02,
    "first_delay": 0.3,
    "embeddings": 0,
    "embedded_texts": 0,
    "responses": 0,
    "streams": 0,
}
lock = threading.Lock()


def vector(text: str) -> list[float]:
    """Deterministic unit vector from the text's words, so similar texts are similar."""
    values = [0.0] * DIMENSIONS
    for word in re.findall(r"\w+", text.lower()):
        digest = hashlib.sha256(word.encode()).digest()
        values[int.from_bytes(digest[:4], "big") % DIMENSIONS] += 1.0
    norm = math.sqrt(sum(v * v for v in values)) or 1.0
    return [v / norm for v in values]


def answer(body: dict) -> str:
    ids = re.findall(r'id=\\?"(S\d+)\\?"', json.dumps(body.get("input", [])))
    source = ids[0] if ids else "S1"
    return json.dumps(
        {
            "found_answer": True,
            "answer": "The documents describe the policy in detail. " * 5 + f"[{source}]",
            "citations": [{"source_id": source, "quote": ""}],
        }
    )


def response(text: str, model: str) -> dict:
    return {
        "id": "resp_load",
        "object": "response",
        "created_at": 1,
        "model": model,
        "status": "completed",
        "output": [
            {
                "type": "message",
                "id": "msg_1",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        ],
        "usage": {
            "input_tokens": 900,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 80,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 980,
        },
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
    }


class Base(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args: object) -> None:
        pass

    def reply(self, status: int, body: object, content_type: str = "application/json") -> None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length", "0") or 0))


class OpenAI(Base):
    def do_GET(self) -> None:  # noqa: N802
        self.reply(200, state)

    def do_POST(self) -> None:  # noqa: N802
        body = json.loads(self.body() or b"{}")
        if self.path == "/control":
            state.update(body)
            return self.reply(200, {"ok": True})
        if self.path.endswith("/embeddings"):
            inputs = body["input"] if isinstance(body["input"], list) else [body["input"]]
            with lock:
                state["embeddings"] += 1
                state["embedded_texts"] += len(inputs)
            time.sleep(state["embed_delay"])
            tokens = sum(len(t.split()) for t in inputs)
            return self.reply(
                200,
                {
                    "object": "list",
                    "model": body["model"],
                    "data": [
                        {"object": "embedding", "index": i, "embedding": vector(t)}
                        for i, t in enumerate(inputs)
                    ],
                    "usage": {"prompt_tokens": tokens, "total_tokens": tokens},
                },
            )
        if self.path.endswith("/responses"):
            text, model = answer(body), body.get("model", "mock")
            with lock:
                state["responses"] += 1
                state["streams"] += bool(body.get("stream"))
            time.sleep(state["first_delay"])
            if not body.get("stream"):
                return self.reply(200, response(text, model))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            step = max(1, len(text) // 30)
            try:
                for i in range(0, len(text), step):
                    event = {
                        "type": "response.output_text.delta",
                        "item_id": "msg_1",
                        "output_index": 0,
                        "content_index": 0,
                        "delta": text[i : i + step],
                        "sequence_number": i,
                        "logprobs": [],
                    }
                    self.wfile.write(
                        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()
                    )
                    self.wfile.flush()
                    time.sleep(state["chunk_delay"])
                done = {
                    "type": "response.completed",
                    "sequence_number": 99999,
                    "response": response(text, model),
                }
                self.wfile.write(
                    f"event: response.completed\ndata: {json.dumps(done)}\n\n".encode()
                )
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        self.reply(404, {"error": {"message": "unknown path"}})


objects: dict[str, bytes] = {}


class Storage(Base):
    """/storage/v1/bucket/<b>, /object/<b>/<path> (POST, GET), DELETE /object/<b>, list."""

    def _key(self) -> str:
        return self.path.split("/storage/v1/object/", 1)[1].split("?", 1)[0]

    def do_GET(self) -> None:  # noqa: N802
        if "/storage/v1/bucket/" in self.path:
            return self.reply(200, {"id": "b", "name": "b", "public": False})
        data = objects.get(self._key())
        if data is None:
            return self.reply(400, {"statusCode": "404", "error": "not_found"})
        return self.reply(200, data, "application/octet-stream")

    def do_POST(self) -> None:  # noqa: N802
        if "/object/list/" in self.path:
            self.body()
            return self.reply(200, [])
        if "/object/sign/" in self.path:
            self.body()
            return self.reply(200, {"signedURL": "/object/sign/x?token=t"})
        key, data = self._key(), self.body()
        with lock:
            if key in objects:
                return self.reply(400, {"statusCode": "409", "error": "Duplicate"})
            objects[key] = data
        return self.reply(200, {"Key": key})

    def do_DELETE(self) -> None:  # noqa: N802
        prefixes = json.loads(self.body() or b"{}").get("prefixes", [])
        bucket = self.path.rstrip("/").rsplit("/", 1)[1]
        with lock:
            for prefix in prefixes:
                objects.pop(f"{bucket}/{prefix}", None)
        return self.reply(200, [])


if __name__ == "__main__":
    kind, port = sys.argv[1], int(sys.argv[2])
    handler = {"openai": OpenAI, "storage": Storage}.get(kind) or partial(
        SimpleHTTPRequestHandler, directory=sys.argv[3]
    )
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    server.serve_forever()
