"""PCM bursts keep every sample, bounded memory and cancellable ownership."""

from __future__ import annotations

import asyncio
from contextlib import aclosing
from typing import TYPE_CHECKING
from typing import cast

import pytest

from nagents.web.live_output import BUFFER_BYTES
from nagents.web.live_output import FRAME_BYTES
from nagents.web.live_output import BrowserOutput
from tests.support.hang_guard import HANG_GUARD
from tests.test_web_live_input_clock import pcm
from tests.test_web_live_input_clock import run

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from nagents.web.live_output import OutputPacket


@pytest.mark.parametrize("quantum", [0, 1 / 64])
def test_four_second_api_burst_is_paced_without_truncation_or_unbounded_buffer(quantum: float) -> None:
    async def scenario() -> None:
        output = BrowserOutput()
        expected = b"".join(pcm(index + 1) for index in range(200)) + pcm(300, 240)
        writer = asyncio.create_task(output.write(expected))
        received: list[bytes] = []
        times: list[float] = []
        try:
            async with aclosing(cast("AsyncGenerator[OutputPacket, None]", output.packets())) as packets:
                async with asyncio.timeout(HANG_GUARD):
                    while sum(map(len, received)) < len(expected):
                        packet = await anext(packets)
                        assert not packet.interrupted and 0 < len(packet.data) <= FRAME_BYTES
                        received.append(packet.data)
                        times.append(asyncio.get_running_loop().time())
                        assert output.buffered_bytes <= BUFFER_BYTES
                await writer
            assert b"".join(received) == expected
            assert times[-1] == pytest.approx(4, abs=quantum + 1e-8)
            assert output.peak_buffered_bytes == BUFFER_BYTES
            assert output.blocked_writes > 0
            assert output.sent_samples == len(expected) // 2
        finally:
            await output.close()
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)

    run(scenario(), quantum=quantum)


def test_already_paced_rtp_does_not_get_a_second_delay_or_synthetic_silence() -> None:
    async def scenario() -> None:
        output = BrowserOutput()
        arrivals: list[float] = []
        deliveries: list[float] = []

        async def produce() -> None:
            for index in range(50):
                await asyncio.sleep(0.02)
                arrivals.append(asyncio.get_running_loop().time())
                await output.write(pcm(index))

        writer = asyncio.create_task(produce())
        try:
            async with aclosing(cast("AsyncGenerator[OutputPacket, None]", output.packets())) as packets:
                for index in range(50):
                    packet = await anext(packets)
                    assert packet.data == pcm(index)
                    deliveries.append(asyncio.get_running_loop().time())
            await writer
            assert deliveries == pytest.approx(arrivals)
            assert output.sent_samples == 50 * 480
        finally:
            await output.close()

    run(scenario())


@pytest.mark.parametrize("closing", [False, True])
def test_interrupt_and_close_release_all_blocked_writers_without_refilling_old_pcm(closing: bool) -> None:
    async def scenario() -> None:
        output = BrowserOutput()
        first = asyncio.create_task(output.write(pcm(1, 24000 * 4)))
        second = asyncio.create_task(output.write(pcm(2)))
        await asyncio.sleep(0)
        assert output.buffered_bytes == BUFFER_BYTES and not first.done()
        await (output.close() if closing else output.interrupt())
        async with asyncio.timeout(HANG_GUARD):
            await asyncio.gather(first, second)
        assert output.buffered_bytes == 0
        async with aclosing(cast("AsyncGenerator[OutputPacket, None]", output.packets())) as packets:
            assert (await anext(packets)).interrupted
            if closing:
                await output.write(pcm(3))
                with pytest.raises(StopAsyncIteration):
                    await anext(packets)
            else:
                await output.write(pcm(3))
                assert (await anext(packets)).data == pcm(3)
        await output.close()

    run(scenario())


def test_interrupt_invalidates_a_leased_frame_waiting_for_its_playout_time() -> None:
    async def scenario() -> None:
        output = BrowserOutput()
        await output.write(pcm(1) + pcm(2))
        async with aclosing(cast("AsyncGenerator[OutputPacket, None]", output.packets())) as packets:
            assert (await anext(packets)).data == pcm(1)
            pending = asyncio.create_task(anext(packets))
            await asyncio.sleep(0)
            await output.interrupt()
            assert (await pending).interrupted
            await output.write(pcm(3))
            assert (await anext(packets)).data == pcm(3)
        await output.close()

    run(scenario())


def test_close_commits_even_when_the_cleanup_caller_is_cancelled() -> None:
    async def scenario() -> None:
        output = BrowserOutput()
        writer = asyncio.create_task(output.write(pcm(1, 24000 * 4)))
        await asyncio.sleep(0)
        await output._condition.acquire()
        closer = asyncio.create_task(output.close())
        await asyncio.sleep(0)
        closer.cancel()
        output._condition.release()
        with pytest.raises(asyncio.CancelledError):
            await closer
        await writer
        assert output.buffered_bytes == 0
        await output.close()

    run(scenario())
