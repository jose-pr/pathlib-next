"""Stopping a server that runs on a loop of its own, thread and sockets
included."""

from __future__ import annotations

import asyncio


async def _drain(server, connections):
    if server is not None:
        server.close()
    for conn in list(connections):
        conn.abort()
    current = asyncio.current_task()
    # Tasks that are left (a handler of an aborted connection) are cancelled
    # and awaited, and the transports they were closing get their turn.
    for _ in range(20):
        pending = [task for task in asyncio.all_tasks() if task is not current]
        if not pending:
            break
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await asyncio.sleep(0)
    if server is not None:
        try:
            await asyncio.wait_for(server.wait_closed(), 5)
        except asyncio.TimeoutError:
            pass
    await asyncio.sleep(0)
    await asyncio.get_running_loop().shutdown_asyncgens()


def stop_server(loop, thread, server=None, connections=()):
    """Close `server`, abort `connections`, cancel what is still pending on
    `loop`, stop and join its thread, and close the loop."""
    try:
        asyncio.run_coroutine_threadsafe(_drain(server, connections), loop).result(15)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        if not thread.is_alive():
            loop.close()
