"""A minimal tool-using agent loop, run through the AgentChaos proxy.

The agent is deliberately bare: a neutral system prompt, no anti-injection
instructions, no extra guardrails beyond what the model does on its own. The
whole point of AgentChaos is to see how an *unprotected* agent copes with
the faults the proxy injects - including prompt injection - so we must not
give it any defenses we didn't ask the model to have itself.

Providers. The model is chosen with a "provider/model" string:
- gemini/<model>  - Google's API via the google-genai SDK
- groq/<model>    - Groq's OpenAI-compatible API via the openai SDK
Both loops do the same thing; only the message format differs.

Flow:
1. Spawn the proxy (agentchaos.proxy) as a subprocess, wrapping the bundled
   test server, with the given FaultConfig translated into its CLI flags.
2. Connect to it as an MCP client and list its tools.
3. Loop: send the conversation so far to the model: if it asks to call a
   tool, call it over MCP and feed the result back; if it answers instead,
   we're done. Stops after MAX_STEPS either way, so a confused model can't
   loop forever.
"""
import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from mcp import ClientSession, StdioServerParameters, types as mcp_types
from mcp.client.stdio import stdio_client
from openai import AsyncOpenAI

from agentchaos.faults import FaultConfig

load_dotenv()

ROOT = Path(__file__).parent.parent
TEST_SERVER = str(ROOT / "test_server" / "server.py")

SYSTEM_PROMPT = "You are a helpful assistant with tools."
MAX_STEPS = 8

DEFAULT_MODEL = "groq/openai/gpt-oss-120b"
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
PROVIDER_KEY_VARS = {"gemini": "GEMINI_API_KEY", "groq": "GROQ_API_KEY"}

# A model's own transient errors - 429 (rate limit) and 503 (high demand) - are
# a different thing from the proxy's --ratelimit-rate fault, which is about the
# *tool* server, not the model. Gemini uses this simple fixed-attempt retry.
# The openai SDK (Groq) retries on its own, see max_retries below.
GEMINI_RETRY_CODES = {429, 503}
GEMINI_MAX_ATTEMPTS = 5
GEMINI_RETRY_SECONDS = 15.0
GROQ_MAX_RETRIES = 5


@dataclass
class ToolCall:
    """One tool call the agent made, and what came back through the proxy."""
    name: str
    args: dict
    result: str
    is_error: bool


@dataclass
class AgentResult:
    """Everything a run produced, so callers (the CLI, the task runner, later
    the scorer) can inspect it rather than parse printed output.

    `steps` counts how many times we asked the model for a turn, so a run that
    answers right away has steps == 1.
    """
    final_answer: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    steps: int = 0


def parse_model(spec: str) -> tuple[str, str]:
    """Split "provider/model" into (provider, model). Raises ValueError for
    anything we don't support, so a typo fails before any API call."""
    provider, sep, model = spec.partition("/")
    if not sep or not model:
        raise ValueError(f"model must look like 'groq/<model>' or 'gemini/<model>', got {spec!r}.")
    if provider not in PROVIDER_KEY_VARS:
        raise ValueError(f"unknown provider {provider!r}. Choose from: {', '.join(PROVIDER_KEY_VARS)}.")
    return provider, model


def _proxy_args(config: FaultConfig) -> list[str]:
    """Translate a FaultConfig into the flags agentchaos.proxy's argparse
    front end understands (see proxy.parse_args), launching it in front of
    the bundled test server.
    """
    return [
        "-m", "agentchaos.proxy",
        "--error-rate", str(config.error_rate),
        "--timeout-rate", str(config.timeout_rate),
        "--timeout-seconds", str(config.timeout_seconds),
        "--latency-ms", str(config.latency_ms),
        "--malformed-rate", str(config.malformed_rate),
        "--injection-rate", str(config.injection_rate),
        "--ratelimit-rate", str(config.ratelimit_rate),
        "--seed", str(config.seed),
        "--", sys.executable, TEST_SERVER,
    ]


def _to_gemini_declaration(tool: mcp_types.Tool) -> genai_types.FunctionDeclaration:
    """Convert one MCP tool's JSON Schema into a Gemini function declaration.
    parameters_json_schema takes the schema as-is - MCP tools already
    describe their inputs in JSON Schema, so no field-by-field conversion.
    """
    return genai_types.FunctionDeclaration(
        name=tool.name,
        description=tool.description or "",
        parameters_json_schema=tool.inputSchema,
    )


def _to_openai_tool(tool: mcp_types.Tool) -> dict:
    """Convert one MCP tool into an OpenAI-style function tool. Same idea as
    above: the MCP input schema is already JSON Schema, which is what the
    "parameters" field expects."""
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": tool.inputSchema,
        },
    }


async def _call_gemini(
    client: genai.Client,
    model: str,
    contents: list[genai_types.Content],
    tools: list[genai_types.FunctionDeclaration],
) -> genai_types.GenerateContentResponse:
    """Call Gemini, retrying a fixed number of times on transient errors
    (HTTP 429 rate limit, 503 high demand)."""
    config = genai_types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        tools=[genai_types.Tool(function_declarations=tools)],
    )
    for attempt in range(1, GEMINI_MAX_ATTEMPTS + 1):
        try:
            return await client.aio.models.generate_content(model=model, contents=contents, config=config)
        except genai_errors.APIError as e:
            if e.code not in GEMINI_RETRY_CODES or attempt == GEMINI_MAX_ATTEMPTS:
                raise
            print(
                f"[agent] Gemini returned {e.code} (attempt {attempt}/{GEMINI_MAX_ATTEMPTS}), "
                f"retrying in {GEMINI_RETRY_SECONDS:g}s",
                file=sys.stderr,
            )
            await asyncio.sleep(GEMINI_RETRY_SECONDS)
    raise AssertionError("unreachable")  # loop always returns or raises


async def _call_tool(session: ClientSession, name: str, args: dict, step: int,
                     result: AgentResult, log) -> tuple[str, bool]:
    """Call one tool through the proxy, record it on `result`, and return
    (text, is_error) for feeding back to the model."""
    log(f"[agent] step {step}: calling {name}({args})")
    mcp_result = await session.call_tool(name, args)
    text = mcp_result.content[0].text if mcp_result.content else ""
    is_error = bool(mcp_result.isError)
    status = "ERROR: " if is_error else ""
    log(f"[agent] step {step}: {name} -> {status}{text}")
    result.tool_calls.append(ToolCall(name=name, args=dict(args), result=text, is_error=is_error))
    return text, is_error


async def _run_gemini(session, mcp_tools, model, question, api_key, result, log) -> None:
    """The Gemini version of the loop. Fills in `result` as it goes."""
    client = genai.Client(api_key=api_key)
    declarations = [_to_gemini_declaration(t) for t in mcp_tools]
    contents = [genai_types.Content(role="user", parts=[genai_types.Part(text=question)])]
    response = None

    for step in range(1, MAX_STEPS + 1):
        response = await _call_gemini(client, model, contents, declarations)
        result.steps = step
        contents.append(response.candidates[0].content)

        calls = response.function_calls
        if not calls:
            result.final_answer = response.text or ""
            return

        response_parts = []
        for call in calls:
            text, is_error = await _call_tool(session, call.name, call.args or {}, step, result, log)
            key = "error" if is_error else "result"
            response_parts.append(genai_types.Part.from_function_response(name=call.name, response={key: text}))
        contents.append(genai_types.Content(role="user", parts=response_parts))

    log(f"[agent] hit the {MAX_STEPS}-step limit without a final answer")
    result.final_answer = (response.text if response else None) or "(no final answer - step limit reached)"


async def _run_groq(session, mcp_tools, model, question, api_key, result, log) -> None:
    """The Groq version of the loop (OpenAI-compatible chat completions).
    Tool results go back as "tool" messages. This format has no error flag,
    so an error is marked with an "ERROR: " prefix in the text instead."""
    client = AsyncOpenAI(api_key=api_key, base_url=GROQ_BASE_URL, max_retries=GROQ_MAX_RETRIES)
    tools = [_to_openai_tool(t) for t in mcp_tools]
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]

    for step in range(1, MAX_STEPS + 1):
        response = await client.chat.completions.create(model=model, messages=messages, tools=tools)
        message = response.choices[0].message
        result.steps = step

        # Keep only the fields we need to send back, so nothing extra from
        # the SDK's response object leaks into the next request.
        calls = message.tool_calls or []
        messages.append({
            "role": "assistant",
            "content": message.content or "",
            "tool_calls": [
                {"id": c.id, "type": "function",
                 "function": {"name": c.function.name, "arguments": c.function.arguments}}
                for c in calls
            ],
        })
        if not calls:
            result.final_answer = message.content or ""
            return

        for call in calls:
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                # The model wrote broken JSON for its arguments. Tell it, so it
                # can try again, rather than crashing the run.
                text = f"ERROR: invalid JSON arguments: {call.function.arguments}"
                result.tool_calls.append(ToolCall(name=call.function.name, args={}, result=text, is_error=True))
                log(f"[agent] step {step}: {call.function.name} -> {text}")
            else:
                text, is_error = await _call_tool(session, call.function.name, args, step, result, log)
                if is_error:
                    text = f"ERROR: {text}"
            messages.append({"role": "tool", "tool_call_id": call.id, "content": text})

    log(f"[agent] hit the {MAX_STEPS}-step limit without a final answer")
    result.final_answer = "(no final answer - step limit reached)"


async def run_agent(question: str, config: FaultConfig, model: str = DEFAULT_MODEL,
                    verbose: bool = True) -> AgentResult:
    """Run the agent loop and return an AgentResult with the final answer,
    every tool call made (args, result, error flag) and the steps used.

    `model` is "provider/model", e.g. "groq/llama-3.3-70b-versatile".
    With verbose=True (the default), each tool call is also logged to stderr
    as it happens, so you can watch the agent think. The task runner passes
    verbose=False to keep its table output clean."""
    def log(message: str) -> None:
        if verbose:
            print(message, file=sys.stderr)

    try:
        provider, model_name = parse_model(model)
    except ValueError as e:
        sys.exit(f"error: {e}")

    key_var = PROVIDER_KEY_VARS[provider]
    api_key = os.environ.get(key_var)
    if not api_key:
        sys.exit(f"error: {key_var} is not set. Copy .env.example to .env and fill it in.")

    params = StdioServerParameters(command=sys.executable, args=_proxy_args(config), cwd=str(ROOT))
    result = AgentResult(final_answer="")
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            mcp_tools = (await session.list_tools()).tools
            log(f"[agent] model: {provider}/{model_name}")
            log(f"[agent] tools available: {[t.name for t in mcp_tools]}")

            if provider == "gemini":
                await _run_gemini(session, mcp_tools, model_name, question, api_key, result, log)
            else:
                await _run_groq(session, mcp_tools, model_name, question, api_key, result, log)
    return result
