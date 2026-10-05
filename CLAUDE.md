# AgentChaos

A fault-injection ("chaos monkey") tool for AI agents. It sits as an MCP proxy between an agent and its tool server, injects failures into tool results, and scores how well the agent copes.

## Goal
Resume project for a student. Ship a working, public, well-documented v1 by **Oct 16, 2026**. Time budget: 1-2 hours/day.

## How to help me
- I'm learning while building. Explain new concepts briefly before writing code, and keep changes small enough that I can understand every line (I need to defend this in interviews).
- Prefer simple, readable Python over clever code.
- After each change, tell me how to run or test it.

## Tech stack
- Python 3.11+, `mcp` Python SDK (FastMCP for servers, `ClientSession` + `stdio_client` for clients)
- Typer (CLI), PyYAML (config), Jinja2 (HTML report), pytest (tests), uv + pyproject.toml (packaging)
- LLM under test: a free-tier model (Gemini or Groq) first

## Architecture
- `test_server/` - small MCP server with fake tools: get_weather, search_notes, send_email (logs only, never sends)
- `agentchaos/proxy.py` - MCP server to the agent + MCP client to the real server; forwards calls
- `agentchaos/faults.py` - 5 fault types: latency/timeout, error response, malformed/empty data, prompt injection (with canary word), rate limit
- `chaos.yaml` - fault probabilities + random seed (runs must be reproducible)
- `agentchaos/runner.py` - runs tasks from `tasks.yaml` twice (baseline, chaos), logs every event to JSONL
- `agentchaos/scorer.py` - task success, injection resisted, honest error handling, recovery
- `agentchaos/report.py` - HTML resilience report

## 20-day plan
Week 1 (Sep 27 - Oct 3): Day 1 learn MCP, Day 2 test server, Day 3 pass-through proxy, Day 4 faults 1-2, Day 5 faults 3 & 5, Day 6 prompt injection, Day 7 config + CLI + GitHub
Week 2 (Oct 4 - Oct 10): Day 8 simple agent, Day 9 task suite, Day 10 runner + log, Day 11 scoring, Day 12 HTML report, Day 13 tests, Day 14 buffer
Week 3 (Oct 11 - Oct 16): Day 15 benchmark 2-3 models, Day 16 real MCP server test, Day 17 README, Day 18 package + demo video, Day 19 publish, Day 20 final review

If behind, cut in this order: fewer models, skip Day 16, merge faults 3 & 5, Markdown report. Never cut: prompt injection, scoring, README with results, demo video.

## Progress
- [x] Day 1 - server.py + client.py starter files work
- [x] Day 2 - test_server/server.py (get_weather, search_notes, send_email) + try_it.py work
- [x] Day 3 - agentchaos/proxy.py forwards tools/calls unchanged; agentchaos/check_proxy.py passes
- [x] Day 4 - agentchaos/faults.py (error + timeout/latency); agentchaos/check_faults.py confirms ~50% failure and seed reproducibility
- [x] Day 5 - agentchaos/faults.py adds malformed data + rate limit (all 5 faults done); package fixed to agentchaos.* imports; per-call deterministic rolls; check_faults.py covers all 5 faults incl. order independence
- [x] Day 6 - agentchaos/faults.py adds prompt injection (--injection-rate): real upstream result kept, instruction appended or hidden mid-text, isError stays False; 4 templates (2 "say", 2 "action" using .invalid email), each planting a deterministic CANARY-xxxxxxxx (8 hex chars); check_faults.py confirms genuine content survives and same seed -> same canary
- [x] Day 7 - agentchaos/config.py loads+validates FaultConfig from YAML (rate bounds, unknown-key errors); CLI flags override the file; agentchaos/cli.py is a Typer console script (`agentchaos proxy --config chaos.yaml -- ...`); pyproject.toml + pip install -e .; chaos.yaml (commented default) + configs/mild.yaml + configs/hostile.yaml presets; short README.md; check_proxy.py and check_faults.py still pass
- [x] Day 8 - agentchaos/agent.py: a simple Gemini (google-genai SDK) tool-using agent loop over MCP, through the proxy; neutral system prompt, no anti-injection instructions; MCP tool schemas converted to Gemini function declarations via parameters_json_schema; loops up to 8 steps feeding tool results back; simple retry on Gemini's own 429s; tool calls/results logged to stderr. `agentchaos ask "question" --config chaos.yaml` added to the CLI. API key/model from .env (python-dotenv) - see .env.example
- [x] Day 9 - agentchaos/agent.py run_agent returns an AgentResult (final_answer, tool_calls with args/result/error, steps) and supports `--model provider/model` (groq/openai/gpt-oss-120b default, gemini/<model>); tasks.yaml has 10 tasks with deterministic checks (contains_all/contains_any/email_sent_to/says_unavailable); agentchaos/tasks.py runs them and prints a pass/fail table via `agentchaos tasks --config configs/baseline.yaml`; baseline with no faults: 10/10 passed on Groq
