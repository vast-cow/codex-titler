import asyncio
import contextlib

import structlog

from .titles import INSTRUCTIONS, Candidate, prepare, validate_title
from .transport import RpcError

MODEL = "gpt-6-luna"
log = structlog.get_logger()


class ContextTooLarge(Exception):
    pass


class InvalidOutput(ValueError):
    pass


class Generator:
    def __init__(self, api, state, settings, helpers):
        self.api, self.state, self.s, self.helpers = api, state, settings, helpers
        self.mcp_overrides = {}

    async def verify(self):
        runtime = await self.api.read("config/read", {"includeLayers": False})
        servers = runtime.get("config", {}).get("mcp_servers", {})
        self.mcp_overrides = {name: {"enabled": False} for name in servers}
        models = []
        async for page in self.api.pages("model/list", {"includeHidden": True}):
            models.extend(page["data"])
        model = next((m for m in models if m.get("model") == MODEL), None)
        if not model or not any(
            e.get("reasoningEffort") == "low" for e in model.get("supportedReasoningEfforts", [])
        ):
            raise ValueError("gpt-6-luna with low effort is unavailable; no fallback permitted")

    async def generate(self, snapshot, original, job=None):
        budget, invalid, contexts = self.s.input_budget, 0, 0
        attempts = 0
        while True:
            prompt, expected, truncated = prepare(snapshot, original, self.s.title_limit, budget)
            attempts += 1
            if job:
                await self.state.update(job, attempts=attempts)
            log.info(
                "generation_attempt",
                job_id=job,
                attempt=attempts,
                input_budget=budget,
                truncated=truncated,
            )
            try:
                raw = await self.attempt(prompt)
            except ContextTooLarge:
                contexts += 1
                if contexts > self.s.context_retries:
                    raise
                budget //= 2
                log.warning("generation_retry", reason="context_limit", budget=budget)
                continue
            except InvalidOutput:
                raw = ""  # A definite completed turn without a single final response.
            # Only output validation failures regenerate. Malformed/ambiguous protocol
            # results must not accidentally replay helper creation or turn start.
            try:
                return validate_title(raw, self.s.title_limit, expected), truncated
            except (ValueError, TypeError):
                invalid += 1
                log.warning("generation_retry", reason="invalid_output", attempt=invalid)
                if invalid > self.s.invalid_retries:
                    raise

    async def attempt(self, prompt):
        rpc = self.api.rpc
        queue = asyncio.Queue(maxsize=1000)
        helper = turn_id = None
        rerouted = False

        def event(method, params):
            nonlocal rerouted, turn_id
            if params.get("threadId") != helper:
                return
            if method == "turn/started":
                turn_id = params["turn"]["id"]
            if method in ("item/started", "item/completed"):
                if params.get("item", {}).get("type") not in (
                    "userMessage",
                    "agentMessage",
                    "reasoning",
                ):
                    rerouted = True  # Tool activity makes this attempt ineligible.
            if method == "model/rerouted":
                rerouted = True
            if method in ("item/completed", "turn/completed"):
                try:
                    queue.put_nowait((method, params))
                except asyncio.QueueFull:
                    # Fail closed if output events exceed our bounded buffer.
                    rerouted = True

        rpc.listeners.add(event)
        try:
            # Config keys checked against the 0.169.0 config schema. Policies may reject overrides.
            config = {
                **self.s.helper_config,
                "web_search": "disabled",
                "include_environment_context": False,
                "features.shell_tool": False,
                "features.unified_exec": False,
                "features.apps": False,
                "features.collab": False,
                "features.code_mode": False,
                "features.view_image": False,
                "features.browser_use": False,
                "features.computer_use": False,
                "features.apply_patch_freeform": False,
                "features.tool_search": False,
                "mcp_servers": self.mcp_overrides,
                "model_context_window": self.s.context_limit,
                "model_auto_compact_token_limit": self.s.auto_compact_limit or self.s.context_limit,
            }
            response = await rpc.call(
                "thread/start",
                {
                    "model": MODEL,
                    "ephemeral": True,
                    "approvalPolicy": "never",
                    "sandbox": "read-only",
                    "baseInstructions": INSTRUCTIONS,
                    "developerInstructions": "Only generate a title. Never call tools.",
                    "serviceName": "codex_titler",
                    "config": config,
                },
            )
            helper = response["thread"]["id"]
            self.helpers.add(helper)
            await self.state.helper(helper)
            if response.get("model") != MODEL:
                raise RuntimeError("helper model differs from requested model")
            async with asyncio.timeout(self.s.generation_timeout):
                result = await rpc.call(
                    "turn/start",
                    {
                        "threadId": helper,
                        "model": MODEL,
                        "effort": "low",
                        "input": [{"type": "text", "text": prompt}],
                        "outputSchema": Candidate.model_json_schema(),
                    },
                )
                turn_id = result["turn"]["id"]
                finals = {}
                while True:
                    event_task = asyncio.create_task(queue.get())
                    closed_task = asyncio.create_task(rpc.closed.wait())
                    try:
                        done, _ = await asyncio.wait(
                            [event_task, closed_task], return_when=asyncio.FIRST_COMPLETED
                        )
                        if closed_task in done:
                            raise ConnectionError("connection lost during generation")
                        method, params = event_task.result()
                    finally:
                        for task in (event_task, closed_task):
                            task.cancel()
                        await asyncio.gather(event_task, closed_task, return_exceptions=True)
                    if method == "item/completed":
                        if params.get("turnId") != turn_id:
                            continue
                        item = params["item"]
                        if item.get("type") == "agentMessage" and item.get("phase") in (
                            None,
                            "final_answer",
                        ):
                            finals[item["id"]] = item["text"]
                    elif params["turn"]["id"] == turn_id:
                        turn = params["turn"]
                        if turn["status"] != "completed":
                            error = str(turn.get("error", {}))
                            if (
                                "contextWindowExceeded" in error
                                or "context_length_exceeded" in error
                            ):
                                raise ContextTooLarge(error)
                            raise RuntimeError("generation did not complete: " + error)
                        if rerouted:
                            raise RuntimeError("rerouted or incomplete helper output")
                        if len(finals) != 1:
                            raise InvalidOutput("expected exactly one final agent message")
                        return next(iter(finals.values()))
        except RpcError as exc:
            if "contextWindowExceeded" in str(exc) or "context_length_exceeded" in str(exc):
                raise ContextTooLarge() from exc
            raise
        finally:
            rpc.listeners.discard(event)
            if helper:
                if turn_id:
                    with contextlib.suppress(Exception):
                        await rpc.call("turn/interrupt", {"threadId": helper, "turnId": turn_id})
                with contextlib.suppress(Exception):
                    await rpc.call("thread/unsubscribe", {"threadId": helper})
