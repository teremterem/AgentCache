# pylint: disable=protected-access
"""Tests for agentcache.promises.MessageSequence"""
import pytest

from agentcache.forum import Forum
from agentcache.promises import MessageSequence


@pytest.mark.asyncio
async def test_nested_message_sequences(forum: Forum) -> None:
    """Verify that message ordering in nested message sequences is preserved."""
    outer_sequence = MessageSequence(forum)
    inner_sequence = MessageSequence(forum)

    # TODO Oleksandr: put inner_sequence send operators AFTER the outer_sequence send operators in this test
    #  after you get rid of _asend_msg()
    with inner_sequence:
        inner_sequence._send_msg("inner message 2")
        inner_sequence._send_msg("inner message 3")

    with outer_sequence:
        outer_sequence._send_msg("outer message 1")
        await outer_sequence._asend_msg(inner_sequence)
        outer_sequence._send_msg("outer message 4")

    actual_sequence = [msg.content for msg in await outer_sequence.amaterialize_all()]
    assert actual_sequence == [
        "outer message 1",
        "inner message 2",
        "inner message 3",
        "outer message 4",
    ]