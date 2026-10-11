from __future__ import annotations

from llmgateway.sse import encode_event, iter_event_data


async def _parse(*lines: str) -> list[str]:
    async def source():
        for line in lines:
            yield line
    return [data async for data in iter_event_data(source())]


async def test_one_event_per_blank_line():
    assert await _parse("data: a", "", "data: b", "") == ["a", "b"]


async def test_multiline_data_is_joined_with_newlines():
    assert await _parse("data: line one", "data: line two", "") == ["line one\nline two"]


async def test_comments_and_other_fields_are_ignored():
    lines = (": keep-alive", "event: message", "id: 7", "retry: 1000", "data: x", "")
    assert await _parse(*lines) == ["x"]


async def test_space_after_colon_is_optional():
    assert await _parse("data:x", "") == ["x"]


async def test_trailing_event_without_blank_line_is_flushed():
    assert await _parse("data: last") == ["last"]


async def test_consecutive_blank_lines_do_not_emit_empty_events():
    assert await _parse("", "", "data: a", "", "") == ["a"]


def test_encode_event_round_trips():
    assert encode_event('{"a":1}') == b'data: {"a":1}\n\n'
