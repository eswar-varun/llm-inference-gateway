# Design FAQ

Why the gateway behaves the way it does. Each answer points to the decision in
[DESIGN.md](DESIGN.md) and, where one exists, the test that pins it down.

## Streaming

**What happens if the upstream fails on a streaming request?**
It depends on whether the client has received anything yet. Before the first upstream chunk
the gateway hasn't sent a byte, so the client gets a normal HTTP error (for example `502` with
an OpenAI-style error body). After the first chunk the `200` is already on the wire, so the
gateway sends an in-band error event and ends the stream **without** `[DONE]`.
See D3 and D4, `test_upstream_failure_before_first_token_gets_a_real_status`, and
`test_mid_stream_failure_becomes_an_error_event`.

**Why not send `200` as soon as the upstream call starts?**
Then every upstream failure would arrive as a `200` with a broken body, and clients, load
balancers and dashboards that look at status codes would count it as a success. Waiting costs
nothing: the client's first token can't arrive before the upstream's.

**What happens when a client hangs up mid-answer?**
Uvicorn delivers `http.disconnect`, and Starlette cancels the response task. `CancelledError`
is raised where the code is waiting (reading the next upstream chunk) and unwinds through
`aclosing(upstream)` in the relay and `response.aclose()` in the provider, which closes the
upstream connection and stops generation (measured: ~230 ms). Servers that detect a hang-up
only when a write fails close the body generator at its `yield` instead. Nothing is raised
inside the relay in that case, so `_sse_body` closes it explicitly in `finally`. Both paths
are tested (D5).

**Why `aclosing(...)` rather than letting Python clean up generators?**
An async generator abandoned mid-iteration is closed only when it's garbage-collected, by a
finalizer the event loop schedules. That timing is an implementation detail. Closing
explicitly makes releasing the upstream connection deterministic.

**How is token usage captured for streams?**
OpenAI-style APIs only send usage on a stream when `stream_options.include_usage` is set.
The gateway always sets it upstream, records the final usage chunk, and drops that chunk
unless the client asked for it (D8).

## Failures and timeouts

**Why four timeouts?**
They detect different failures (D6):
- **connect:** a dead host should fail fast.
- **read:** a stalled stream.
- **pool:** the gateway itself is saturated, so it reports `503 overloaded`, not an
  upstream timeout.
- **total:** an upstream that trickles bytes slowly enough never trips the read timeout.

**Why does an upstream `401` become a `502`, while a `400` is passed through?**
A `401` from the upstream means it rejected *the gateway's* credentials or deployment name.
The client did nothing wrong, and a `401` would send them to debug credentials they don't
control. A `400` such as "maximum context length exceeded" is caused by the client's input,
so they need the upstream's status and message (D7).

**Which failures are safe to retry?**
Each failure carries `retryable`. Connection errors, timeouts, `429` and most `5xx` are
retryable; client errors aren't. For streams, a retry is only safe before the first byte
reaches the client, because after that it would duplicate output. Retries and provider
failover arrive in milestone 2.

## Testing

**How is disconnect behaviour tested if in-process test clients buffer responses?**
`httpx.ASGITransport` collects the whole response before returning it, so it can't observe a
mid-stream hang-up. `tests/test_integration.py` runs the mock upstream and the gateway under
real uvicorn servers on ephemeral ports, reads 5 tokens of a 1,000-token stream, disconnects,
and asserts the mock recorded an aborted stream with no active streams left.

**Why build a mock LLM server?**
Real providers cost money, rate-limit you, and fail at random rather than on command. Load
tests and fault-injection experiments need deterministic latency, generation speed and
failure rates that can be changed at runtime (`PUT /admin/config`).

## Scaling and extension

**How does this run across many instances?**
Milestone 1 keeps no shared state, so it scales horizontally behind a load balancer. Rate
limits and idempotency keys (milestone 3) need shared state, which is why Redis comes in
there.

**Is Python's GIL a problem for a gateway?**
Not for the proxying itself. The work is I/O-bound, and asyncio multiplexes thousands of
waiting connections on one thread. CPU-heavy work, such as embeddings for the semantic cache,
is a different matter and is measured in milestone 4.

**How would Bedrock be added?**
As a new `Provider` subclass: SigV4 signing, the Converse API, and translation of its event
stream into OpenAI-style chunks. Nothing above the provider layer changes.

**What's missing today?**
Failover across providers, retries, client authentication and quotas, and caching. See the
[roadmap](../README.md#roadmap).

## Break it yourself

These are the fastest way to see why each piece exists:

1. In `service.py::open_stream`, remove the first-chunk peek and run `pytest -k first_token`.
2. In `api.py::_sse_body`, delete `await stream.chunks.aclose()` and run the full suite. The
   real-socket test still passes and `test_closing_the_response_body_closes_the_upstream`
   fails. Work out why both are right.
3. Start `mockllm --ttft-ms 5000` with a provider `read_s: 1`. Which `ErrorKind` does the
   client see?
4. Send `X-Request-ID: hello world` and explain why it's replaced.
