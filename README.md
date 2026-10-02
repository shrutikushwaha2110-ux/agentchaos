# AgentChaos

A fault-injection ("chaos monkey") proxy for AI agents that talk to MCP tool
servers: it sits between the agent and the real server, injects failures
into tool results, and (soon) scores how well the agent copes.

## The 5 faults

- **error** - an instant fake upstream error; the real server is never called.
- **timeout / latency** - a slow call, or an outright timeout.
- **malformed data** - a "successful" result whose content is corrupted.
- **prompt injection** - a "successful" result that also carries a planted
  instruction and a unique `CANARY-xxxxxxxx` code, so you can check whether
  the agent repeated it or acted on it.
- **rate limit** - a 429 response, followed by a short cooldown on that tool.

## Install

```bash
pip install -e .
```

## Quickstart

```bash
agentchaos proxy --config chaos.yaml -- python test_server/server.py
```

This starts the proxy in front of the bundled test server, using the fault
rates in `chaos.yaml`. Point an agent's MCP client at the proxy instead of
the real server to see how it copes. `configs/mild.yaml` and
`configs/hostile.yaml` are lighter/heavier presets.

A full README with setup details, results and a demo lands on Day 17.
