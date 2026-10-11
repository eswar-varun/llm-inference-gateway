"""Server-Sent Events encoding and parsing (https://html.spec.whatwg.org/#server-sent-events)."""

from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator


def encode_event(data: str) -> bytes:
    # Our payloads are single-line JSON, so one data field per event is enough.
    return f"data: {data}\n\n".encode()


async def iter_event_data(lines: AsyncIterable[str]) -> AsyncIterator[str]:
    """Yield the `data` payload of each event from an SSE line stream.

    Handles what real providers send: multi-line data fields, `:` comment lines used as
    keep-alives, and other fields (event/id/retry) that we ignore. A blank line ends an event.
    """
    data_lines: list[str] = []
    async for line in lines:
        if line == "":
            if data_lines:
                yield "\n".join(data_lines)
                data_lines = []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "data":
            data_lines.append(value)
    if data_lines:
        yield "\n".join(data_lines)
