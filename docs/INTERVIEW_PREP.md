# Interview prep: milestone 1

Interviewers will pick one project and dig for 20–30 minutes. This file lists the questions
this design invites. **Answer each one out loud in your own words before reading the answer.**
If you can't, read the code it points to, then try again.

## The 60-second pitch

> "Every team calling LLMs re-implements retries, timeouts, failover and cost tracking, so I
> built an OpenAI-compatible gateway that does it once. Clients just change their base URL.
> The interesting part is streaming: HTTP commits to a status code before the body, so the
> gateway waits for the upstream's first token before replying. Failures before that point
> get real status codes and can be retried on another provider; failures after it are
> reported in-band without `[DONE]` so a truncated answer never looks complete. And when a
> client hangs up, the cancellation propagates upstream so we stop paying for tokens nobody
> reads. I proved that with an integration test against real sockets."

## Core questions

**1. Why OpenAI-compatible instead of your own API?**
Adoption cost. Every SDK and framework already speaks it, so switching to the gateway is a
one-line `base_url` change. → D1

**2. What happens if the upstream returns 503 on a streaming request?**
Before the first chunk: the gateway hasn't sent anything yet, so the client gets a `502` with
an OpenAI-style error body. After the first chunk: the `200` is already sent, so the gateway
emits an in-band error event and closes without `[DONE]`. → D3, D4,
`test_upstream_failure_before_first_token_gets_a_real_status`,
`test_mid_stream_failure_becomes_an_error_event`

**3. Why not just send `200` immediately and stream whatever comes?**
Because then *every* upstream failure becomes a 200 with a broken body. Clients, load
balancers and dashboards that look at status codes would all count it as success. Waiting
costs nothing: the client's first token can't arrive before the upstream's.

**4. A user closes the browser tab mid-answer. What happens, step by step?**
The TCP connection closes → uvicorn delivers an `http.disconnect` message → Starlette cancels
the response task → `CancelledError` is raised where the code is waiting, i.e. reading the
next upstream chunk → it unwinds through `aclosing(upstream)` in the relay → the provider's
`finally` runs `response.aclose()` → the upstream connection closes and generation stops.
Measured: upstream aborted within ~230 ms.
Follow-up you should expect: *"what if the server doesn't cancel?"* Then the body generator
is closed at its `yield`, nothing is raised inside the relay, and the explicit `aclose()` in
`_sse_body`'s `finally` is what saves you. → D5

**4b. Why `aclosing(...)` instead of letting Python clean up generators?**
An async generator abandoned mid-iteration isn't closed when you stop iterating. It's closed
when it's garbage-collected, by a finalizer the event loop schedules. That timing is an
implementation detail. Explicit close makes resource release deterministic.

**5. Why does that matter?**
LLM output is billed per token. Without propagation, an abandoned request keeps generating to
`max_tokens`. At scale that's real money, and it also holds GPU capacity other users need.

**6. Why four timeouts? Isn't one enough?**
They detect different failures. Connect = dead host (fail fast). Read = stalled stream.
Pool = *we* are saturated (that's a 503 about us, not a 504 about them). Total = an upstream
that trickles bytes slowly enough never to trip the read timeout. → D6

**7. Why does an upstream 401 become a 502 instead of a 401?**
The client authenticated to the gateway fine. The upstream rejected *the gateway's*
credentials. A 401 would send the client to debug credentials they don't own. 502 says
"the thing behind me failed". → D7

**8. But you pass 400s through. Why the difference?**
A 400 like "context length exceeded" is caused by the client's input, and only they can fix
it, so they need the upstream's message and status.

**9. How do you know a retry is safe?**
Each failure carries `retryable`. Connect errors, timeouts, 429 and most 5xx are retryable;
4xx client errors aren't. For streams, retrying is only safe before the first byte is sent
to the client. After that you'd duplicate output. (Implemented in milestone 2.)

**10. How do you get token usage for streamed responses?**
OpenAI-style APIs only send it if `stream_options.include_usage` is set. The gateway always
sets it upstream, records the final usage chunk, and strips that chunk if the client didn't
ask for it. → D8

**11. How did you test the disconnect behavior?**
In-process ASGI transports buffer the whole response, so they can't observe a mid-stream
hang-up. The integration test runs the mock upstream and the gateway under real uvicorn
servers on ephemeral ports, reads 5 tokens of a 1,000-token stream, disconnects, and asserts
the mock recorded an aborted stream with zero active streams. → `tests/test_integration.py`

**12. Why build a mock LLM server?**
Real providers cost money, rate-limit you, and fail randomly rather than on command.
Benchmarks and chaos tests need deterministic latency, speed and failure rates, adjustable
at runtime (`PUT /admin/config`).

**13. What's the weakest part of milestone 1?**
Good answer: there's no failover yet. One provider outage takes the route down. That's
milestone 2. Also, the gateway has no client auth or quotas, so any caller can spend any
amount.

## Likely follow-ups (think these through)

- How would you run this across 10 instances? What state is shared? *(Today: none, so it
  scales horizontally behind a load balancer. Rate limits and idempotency in milestone 3
  need shared state, hence Redis.)*
- Python has a GIL. Is that a problem for a gateway? *(The work is I/O-bound: asyncio
  multiplexes thousands of waiting connections on one thread. CPU-heavy work like
  embeddings for the cache is a different story, so measure it in milestone 4.)*
- What if the client sends `n=3`? *(Passed through; chunks carry `index`; nothing in the
  relay assumes one choice.)*
- How would you add Bedrock? *(New `Provider` subclass: SigV4 signing, Converse API, and a
  translation of its event stream into OpenAI chunks. Nothing above the provider changes.)*

## Amazon leadership-principle angles

- **Customer Obsession:** zero client changes to adopt (D1); errors that tell clients
  whether to retry (D7).
- **Frugality:** cancelling abandoned generations (D5).
- **Dive Deep:** discovering that in-process tests can't observe streaming timing, and
  writing real-socket tests to prove it.
- **Insist on the Highest Standards:** ambiguous truncation was unacceptable, hence no
  `[DONE]` after an error (D4).

## Exercises: break it to understand it

1. In `service.py::open_stream`, remove the `first = await anext(...)` peek (start relaying
   immediately). Run `pytest -k first_token`. Explain exactly why it fails.
2. In `api.py::_sse_body`, delete only the line `await stream.chunks.aclose()` and run the
   full suite. The real-socket integration test **still passes**, but
   `test_closing_the_response_body_closes_the_upstream` **fails**. Before reading D5, explain
   why both results are correct. (Hint: where is the code suspended when each kind of
   disconnect arrives?)
3. Start `mockllm --ttft-ms 5000` and set the provider's `read_s: 1`. What does the client
   see, and which `ErrorKind` produced it?
4. Run the gateway and send `X-Request-ID: hello world`. Why is it replaced?
