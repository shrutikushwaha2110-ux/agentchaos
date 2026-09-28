"""Fault injection for the AgentChaos proxy.

Two fault types, each decided by its own probability roll:
- error:   return a fake upstream error immediately (upstream is never called).
- timeout: delay, then return a fake timeout error (upstream is never called).
If neither fires, a small random delay simulates ordinary network latency and
the proxy goes on to call the real upstream.

Rolls use a seeded random.Random instance, not the global `random` module, so
that a run with the same --seed injects exactly the same faults in exactly
the same order every time - this is what makes a "chaos run" reproducible.
"""
import asyncio
import logging
import random
from dataclasses import dataclass

from mcp import types

log = logging.getLogger("faults")

TIMEOUT_SECONDS = 30  # how long a simulated timeout claims to have waited


@dataclass
class FaultConfig:
    error_rate: float = 0.0    # probability a call fails with a fake error response
    timeout_rate: float = 0.0  # probability a call times out instead
    latency_ms: int = 0        # max random delay (ms) added to a normal call
    seed: int = 0               # seed for reproducible fault sequences


class FaultInjector:
    """Decides, per tool call, whether to short-circuit with a fault."""

    def __init__(self, config: FaultConfig):
        self.config = config
        # Our own Random instance: two injectors with the same seed always
        # produce the same sequence of rolls, regardless of what else in the
        # process has called the shared `random` module.
        self.rng = random.Random(config.seed)

    async def maybe_inject(self, tool_name: str) -> types.CallToolResult | None:
        """Return a fault result to use instead of calling upstream, or None
        to proceed normally. Error is checked first, then timeout, so at most
        one fault fires per call.
        """
        if self.rng.random() < self.config.error_rate:
            log.info("FAULT [error] %s -> 503 Service Unavailable", tool_name)
            return types.CallToolResult(
                content=[types.TextContent(type="text", text="503 Service Unavailable")],
                isError=True,
            )

        if self.rng.random() < self.config.timeout_rate:
            log.info("FAULT [timeout] %s -> waiting %ss then failing", tool_name, TIMEOUT_SECONDS)
            await asyncio.sleep(TIMEOUT_SECONDS)
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"Tool call timed out after {TIMEOUT_SECONDS}s")],
                isError=True,
            )

        if self.config.latency_ms > 0:
            delay = self.rng.uniform(0, self.config.latency_ms / 1000)
            log.info("FAULT [latency] %s -> delaying %.2fs", tool_name, delay)
            await asyncio.sleep(delay)

        return None
