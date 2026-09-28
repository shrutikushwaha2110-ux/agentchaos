"""Check fault injection through the real proxy:
- with error-rate 0.5, roughly half of 20 calls should fail.
- two runs with the same seed must inject faults in exactly the same order.
  That reproducibility is the whole point of seeding: a chaos run can be
  replayed later to debug exactly what the agent saw.
- the same seed gives the same faults *per tool*, even if the agent happens
  to call tools in a different order (see faults.FaultInjector._rng).
- timeout, malformed and rate-limit faults each fire and look right.

Run from the project root:  python -m agentchaos.check_faults
"""
import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agentchaos.faults import FaultConfig, FaultInjector

ROOT = Path(__file__).parent.parent
SERVER = str(ROOT / "test_server" / "server.py")

NUM_CALLS = 20
SEED = 42


def proxy_args(*flags: str) -> list[str]:
    return ["-m", "agentchaos.proxy", *flags, "--", sys.executable, SERVER]


@asynccontextmanager
async def connect(*flags: str):
    """Yield a session to a fresh proxy subprocess with the given flags."""
    params = StdioServerParameters(command=sys.executable, args=proxy_args(*flags), cwd=str(ROOT))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def call_many(session: ClientSession, tool: str, args: dict, n: int) -> list:
    """Call the same tool n times; return the raw CallToolResult objects."""
    return [await session.call_tool(tool, args) for _ in range(n)]


async def check_error_rate_and_reproducibility() -> bool:
    """error-rate 0.5, same seed twice: roughly half fail, and identically."""
    passed = True

    async with connect("--error-rate", "0.5", "--seed", str(SEED)) as session:
        results = await call_many(session, "get_weather", {"city": "Delhi"}, NUM_CALLS)
    first = [r.isError for r in results]
    fail_count = sum(first)
    print(f"error-rate 0.5, seed {SEED}: {fail_count}/{NUM_CALLS} calls failed -> {first}")
    roughly_half = 5 <= fail_count <= 15  # loose bound; this is randomness, not an exact split
    print(f"[{'PASS' if roughly_half else 'FAIL'}] roughly half of the calls failed")
    passed &= roughly_half

    async with connect("--error-rate", "0.5", "--seed", str(SEED)) as session:
        results = await call_many(session, "get_weather", {"city": "Delhi"}, NUM_CALLS)
    second = [r.isError for r in results]
    same_sequence = first == second
    print(f"[{'PASS' if same_sequence else 'FAIL'}] same seed -> identical fault sequence")
    if not same_sequence:
        print("  run 1:", first)
        print("  run 2:", second)
    passed &= same_sequence

    async with connect("--error-rate", "0.5", "--seed", str(SEED + 1)) as session:
        results = await call_many(session, "get_weather", {"city": "Delhi"}, NUM_CALLS)
    other_seed = [r.isError for r in results]
    differs = other_seed != first
    print(
        f"(seed {SEED + 1}, for comparison: {sum(other_seed)}/{NUM_CALLS} failed, "
        f"sequence {'differs as expected' if differs else 'happens to match - unlucky but not a bug'})"
    )
    return passed


async def check_order_independence() -> bool:
    """Same seed, but call get_weather and search_notes in a different
    interleaving each time. Each tool's Nth call should still get the same
    fault both times, because rolls are keyed on (tool, that tool's call
    count) - not on how many calls happened overall."""
    flags = ("--error-rate", "0.5", "--seed", str(SEED))

    async with connect(*flags) as session:
        # Order A: 5 weather calls, then 5 notes calls.
        weather_a = await call_many(session, "get_weather", {"city": "Delhi"}, 5)
        notes_a = await call_many(session, "search_notes", {"query": "meeting"}, 5)

    async with connect(*flags) as session:
        # Order B: interleaved, so the *overall* call number for each tool differs.
        weather_b, notes_b = [], []
        for _ in range(5):
            weather_b.append((await call_many(session, "get_weather", {"city": "Delhi"}, 1))[0])
            notes_b.append((await call_many(session, "search_notes", {"query": "meeting"}, 1))[0])

    weather_match = [r.isError for r in weather_a] == [r.isError for r in weather_b]
    notes_match = [r.isError for r in notes_a] == [r.isError for r in notes_b]
    print(f"get_weather faults, blocked vs interleaved:  {[r.isError for r in weather_a]}")
    print(f"                                              {[r.isError for r in weather_b]}")
    print(f"[{'PASS' if weather_match else 'FAIL'}] get_weather faults match across call orders")
    print(f"[{'PASS' if notes_match else 'FAIL'}] search_notes faults match across call orders")
    return weather_match and notes_match


async def check_timeout() -> bool:
    """timeout-rate 1.0 with a tiny timeout-seconds: every call times out fast."""
    async with connect("--timeout-rate", "1.0", "--timeout-seconds", "0.2", "--seed", str(SEED)) as session:
        results = await call_many(session, "get_weather", {"city": "Delhi"}, 3)
    all_timed_out = all(r.isError for r in results)
    all_say_timeout = all("timed out" in r.content[0].text for r in results)
    print(f"timeout-rate 1.0: {[r.content[0].text for r in results]}")
    print(f"[{'PASS' if all_timed_out and all_say_timeout else 'FAIL'}] every call times out with the right message")
    return all_timed_out and all_say_timeout


async def check_malformed() -> bool:
    """malformed-rate 1.0: every call succeeds (isError=False) but the content is broken."""
    async with connect("--malformed-rate", "1.0", "--seed", str(SEED)) as session:
        results = await call_many(session, "get_weather", {"city": "Delhi"}, 6)
    all_look_like_success = all(not r.isError for r in results)
    texts = [r.content[0].text if r.content else "<empty>" for r in results]
    styles_seen = {t for t in texts}
    print(f"malformed-rate 1.0, isError=False every time, content: {texts}")
    print(f"[{'PASS' if all_look_like_success else 'FAIL'}] isError stays False (looks like success)")
    varied = len(styles_seen) > 1
    print(f"[{'PASS' if varied else 'FAIL'}] more than one corruption style appeared across 6 calls")
    return all_look_like_success and varied


async def check_ratelimit() -> bool:
    """ratelimit-rate 1.0 means every fresh roll triggers (a roll is always
    < 1.0), so calling the real proxy wouldn't show the cooldown *ending* -
    it would just keep re-triggering forever. To see the boundary cleanly,
    this test drives FaultInjector directly: trigger once, confirm the next
    2 calls are forced 429 by the cooldown alone, then turn the rate down
    to 0 and confirm the call after that is finally clean."""
    config = FaultConfig(ratelimit_rate=1.0, seed=SEED)
    injector = FaultInjector(config)

    codes = []
    for _ in range(3):  # triggering call + the 2 forced by cooldown
        fault_result, _ = await injector.before_call("get_weather")
        codes.append("429" if fault_result is not None else "ok")
    config.ratelimit_rate = 0.0  # cooldown should now be the only thing left to expire
    fault_result, _ = await injector.before_call("get_weather")
    codes.append("429" if fault_result is not None else "ok")

    first_three_429 = codes[:3] == ["429", "429", "429"]
    fourth_recovered = codes[3] == "ok"
    print(f"ratelimit-rate 1.0: {codes}")
    print(f"[{'PASS' if first_three_429 else 'FAIL'}] triggering call + next 2 calls are all 429")
    print(f"[{'PASS' if fourth_recovered else 'FAIL'}] cooldown ends after that: 4th call succeeds")
    return first_three_429 and fourth_recovered


async def main() -> int:
    results = {
        "error rate + reproducibility": await check_error_rate_and_reproducibility(),
        "order independence": await check_order_independence(),
        "timeout": await check_timeout(),
        "malformed data": await check_malformed(),
        "rate limit": await check_ratelimit(),
    }
    print()
    for name, ok in results.items():
        print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    all_passed = all(results.values())
    print("\nAll fault checks passed." if all_passed else "\nSome checks failed.")
    return 0 if all_passed else 1


sys.exit(asyncio.run(main()))
