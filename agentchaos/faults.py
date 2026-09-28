"""Fault injection for the AgentChaos proxy.

Five fault types, each decided by its own probability:
1. error:      return a fake upstream error immediately (upstream never called).
2. timeout:    delay, then return a fake timeout error (upstream never called).
   (latency, below, is the non-failing sibling of this: same delay, no failure.)
3. malformed:  call upstream for real, then corrupt the successful result
               while keeping isError=False - it must still look like a success.
5. ratelimit:  return a 429 error. Stateful: once it fires, the next two
               calls to that same tool are also 429'd, so a chaos run can
               show whether the agent backs off or keeps hammering.
(latency has no number of its own - it's the "nothing failed" case of #2.)

Reproducibility: every random decision is derived from
    (seed, tool name, this tool's call number, fault type)
rather than drawn from one shared generator advanced call after call. See
FaultInjector._rng for why that matters.
"""
import asyncio
import logging
import random
from collections import defaultdict
from dataclasses import dataclass

from mcp import types

log = logging.getLogger("faults")

RATELIMIT_RETRY_AFTER_SECONDS = 5
RATELIMIT_COOLDOWN_CALLS = 2  # once triggered, this many *more* calls also get 429


@dataclass
class FaultConfig:
    error_rate: float = 0.0        # probability a call fails with a fake error response
    timeout_rate: float = 0.0      # probability a call times out instead
    timeout_seconds: float = 30.0  # how long a simulated timeout claims to have waited
    latency_ms: int = 0            # max random delay (ms) added to a normal call
    malformed_rate: float = 0.0    # probability a genuine result is corrupted afterwards
    ratelimit_rate: float = 0.0    # probability a call triggers a 429 cooldown
    seed: int = 0                  # seed for reproducible fault sequences


class FaultInjector:
    """Decides, per tool call, whether to inject a fault.

    Two hooks, because malformed data needs the real result to corrupt:
    - before_call(): may short-circuit the call (error, timeout, rate limit),
      or apply plain latency and let the proxy call upstream as normal.
    - after_call(): given upstream's real result, may corrupt it (malformed).
    """

    def __init__(self, config: FaultConfig):
        self.config = config
        # How many times each tool has been called, and how many more calls
        # to a tool are still under an active rate-limit cooldown.
        self.call_counts: dict[str, int] = defaultdict(int)
        self.ratelimit_remaining: dict[str, int] = defaultdict(int)

    def _rng(self, tool_name: str, call_index: int, kind: str) -> random.Random:
        """A fresh, independent RNG for one (call, fault type) pair.

        Deriving the seed from a string, rather than advancing one shared
        Random forward call after call, buys two things:
        1. Reproducible regardless of call order: tool X's 3rd call always
           rolls the same numbers, whether it was the process's 3rd call
           overall or its 30th - an agent that calls tools in a different
           order still sees the same faults per tool.
        2. Independent fault types: adding, removing, or reordering the
           fault checks inside before_call/after_call never shifts the
           rolls of the other fault types, because each has its own stream.
        Random(str) is used deliberately instead of Random(hash(...)):
        Python's built-in hash() of a string is randomized per-process
        (PYTHONHASHSEED) for security, so it would silently break
        cross-run reproducibility. Random() hashes the string itself with a
        fixed algorithm, so the same string always seeds the same sequence.
        """
        return random.Random(f"{self.config.seed}:{tool_name}:{call_index}:{kind}")

    async def before_call(self, tool_name: str) -> tuple[types.CallToolResult | None, int]:
        """Return (fault_result, call_index).

        If fault_result is not None, use it instead of calling upstream.
        Otherwise, call upstream as normal, then pass call_index and the
        real result to after_call().
        """
        call_index = self.call_counts[tool_name]
        self.call_counts[tool_name] += 1

        # Rate-limit cooldown takes priority: if we're still in one, every
        # call gets 429 no matter what its own rolls would say.
        if self.ratelimit_remaining[tool_name] > 0:
            self.ratelimit_remaining[tool_name] -= 1
            log.info(
                "FAULT [ratelimit] %s call #%d -> 429 (cooldown, %d more after this)",
                tool_name, call_index, self.ratelimit_remaining[tool_name],
            )
            return self._ratelimit_result(), call_index

        if self._rng(tool_name, call_index, "ratelimit").random() < self.config.ratelimit_rate:
            self.ratelimit_remaining[tool_name] = RATELIMIT_COOLDOWN_CALLS
            log.info(
                "FAULT [ratelimit] %s call #%d -> 429 (triggered, next %d calls also limited)",
                tool_name, call_index, RATELIMIT_COOLDOWN_CALLS,
            )
            return self._ratelimit_result(), call_index

        if self._rng(tool_name, call_index, "error").random() < self.config.error_rate:
            log.info("FAULT [error] %s call #%d -> 503 Service Unavailable", tool_name, call_index)
            return types.CallToolResult(
                content=[types.TextContent(type="text", text="503 Service Unavailable")],
                isError=True,
            ), call_index

        if self._rng(tool_name, call_index, "timeout").random() < self.config.timeout_rate:
            log.info(
                "FAULT [timeout] %s call #%d -> waiting %gs then failing",
                tool_name, call_index, self.config.timeout_seconds,
            )
            await asyncio.sleep(self.config.timeout_seconds)
            return types.CallToolResult(
                content=[types.TextContent(
                    type="text", text=f"Tool call timed out after {self.config.timeout_seconds:g}s"
                )],
                isError=True,
            ), call_index

        if self.config.latency_ms > 0:
            delay = self._rng(tool_name, call_index, "latency").uniform(0, self.config.latency_ms / 1000)
            log.info("FAULT [latency] %s call #%d -> delaying %.2fs", tool_name, call_index, delay)
            await asyncio.sleep(delay)

        return None, call_index

    def after_call(self, tool_name: str, call_index: int, result: types.CallToolResult) -> types.CallToolResult:
        """Possibly corrupt a genuine, successful result (malformed data fault)."""
        if result.isError:
            return result  # don't pile a fake fault onto a real upstream error

        rng = self._rng(tool_name, call_index, "malformed")
        if rng.random() < self.config.malformed_rate:
            corrupted, style = self._corrupt(rng, result)
            log.info("FAULT [malformed] %s call #%d -> %s", tool_name, call_index, style)
            return corrupted
        return result

    def _ratelimit_result(self) -> types.CallToolResult:
        return types.CallToolResult(
            content=[types.TextContent(
                type="text",
                text=f"429 Too Many Requests. Retry after {RATELIMIT_RETRY_AFTER_SECONDS} seconds.",
            )],
            isError=True,
        )

    def _corrupt(self, rng: random.Random, original: types.CallToolResult) -> tuple[types.CallToolResult, str]:
        """Build a corrupted-but-successful result. `rng` continues the same
        stream that decided *whether* to corrupt, so which style is picked is
        just as reproducible as that decision.

        Gotcha found while building this: the MCP *client* library checks a
        non-error result's structuredContent against the tool's declared
        output schema, and raises if it's missing - so a truly empty or
        None structuredContent would crash the client before the corrupted
        data ever reached an agent, instead of testing anything. So the
        corrupted text still goes into structuredContent's "result" field,
        kept schema-valid (it's still declared as a string) while the string
        itself is garbage. The corruption is in the *content*, not in
        breaking the protocol envelope around it.
        """
        style = rng.choice(["empty", "truncated", "garbage"])
        if style == "empty":
            text = ""
            content: list[types.TextContent] = []
        elif style == "truncated":
            source = original.content[0].text if original.content and hasattr(original.content[0], "text") else ""
            text = source[: len(source) // 2]
            content = [types.TextContent(type="text", text=text)]
        else:  # garbage: a real server that emitted broken JSON instead of a proper reply
            text = rng.choice(["null", '{"error": undefined}'])
            content = [types.TextContent(type="text", text=text)]

        structured = original.structuredContent
        if isinstance(structured, dict) and "result" in structured:
            structured = {**structured, "result": text}
        return types.CallToolResult(content=content, structuredContent=structured, isError=False), style
