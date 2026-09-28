"""Check fault injection through the real proxy:
- with error-rate 0.5, roughly half of 20 calls should fail.
- two runs with the same seed must inject faults in exactly the same order.
  That reproducibility is the whole point of seeding: a chaos run can be
  replayed later to debug exactly what the agent saw.

Run from the project root:  python agentchaos/check_faults.py
"""
import asyncio
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).parent.parent
SERVER = str(ROOT / "test_server" / "server.py")
PROXY = str(ROOT / "agentchaos" / "proxy.py")

NUM_CALLS = 20
SEED = 42


async def run_through_proxy(seed: int) -> list[bool]:
    """Make NUM_CALLS identical calls through a fresh proxy process; return
    the isError flag for each one, in order."""
    proxy_argv = [
        PROXY,
        "--error-rate", "0.5",
        "--timeout-rate", "0",
        "--latency-ms", "0",
        "--seed", str(seed),
        "--",
        sys.executable, SERVER,
    ]
    params = StdioServerParameters(command=sys.executable, args=proxy_argv)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            errors = []
            for _ in range(NUM_CALLS):
                result = await session.call_tool("get_weather", {"city": "Delhi"})
                errors.append(result.isError)
            return errors


async def main() -> int:
    checks_passed = True

    first = await run_through_proxy(SEED)
    fail_count = sum(first)
    print(f"error-rate 0.5, seed {SEED}: {fail_count}/{NUM_CALLS} calls failed -> {first}")
    roughly_half = 5 <= fail_count <= 15  # loose bound; this is randomness, not an exact split
    print(f"[{'PASS' if roughly_half else 'FAIL'}] roughly half of the calls failed")
    checks_passed &= roughly_half

    second = await run_through_proxy(SEED)
    same_sequence = first == second
    print(f"[{'PASS' if same_sequence else 'FAIL'}] same seed -> identical fault sequence")
    if not same_sequence:
        print("  run 1:", first)
        print("  run 2:", second)
    checks_passed &= same_sequence

    other_seed = await run_through_proxy(SEED + 1)
    differs = other_seed != first
    print(
        f"(seed {SEED + 1}, for comparison: {sum(other_seed)}/{NUM_CALLS} failed, "
        f"sequence {'differs as expected' if differs else 'happens to match - unlucky but not a bug'})"
    )

    print("\nFault injection is reproducible." if checks_passed else "\nSome checks failed.")
    return 0 if checks_passed else 1


sys.exit(asyncio.run(main()))
