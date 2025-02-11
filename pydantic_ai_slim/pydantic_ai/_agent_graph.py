from __future__ import annotations as _annotations

import asyncio
import dataclasses
from abc import ABC
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import field
from typing import Any, Generic, Literal, Union, cast

import logfire_api
from typing_extensions import TypeVar, assert_never

from pydantic_graph import BaseNode, Graph, GraphRunContext
from pydantic_graph.nodes import End, NodeRunEndT

from . import (
    _result,
    _system_prompt,
    exceptions,
    messages as _messages,
    models,
    usage as _usage,
)
from .models import ModelRequestParameters, StreamedResponse
from .result import MarkFinalResult, ResultDataT
from .settings import ModelSettings, merge_model_settings
from .tools import (
    RunContext,
    Tool,
    ToolDefinition,
)

_logfire = logfire_api.Logfire(otel_scope='pydantic-ai')

# while waiting for https://github.com/pydantic/logfire/issues/745
try:
    import logfire._internal.stack_info
except ImportError:
    pass
else:
    from pathlib import Path

    logfire._internal.stack_info.NON_USER_CODE_PREFIXES += (str(Path(__file__).parent.absolute()),)

T = TypeVar('T')
NoneType = type(None)
EndStrategy = Literal['early', 'exhaustive']
"""The strategy for handling multiple tool calls when a final result is found.

- `'early'`: Stop processing other tool calls once a final result is found
- `'exhaustive'`: Process all tool calls even after finding a final result
"""
DepsT = TypeVar('DepsT')
ResultT = TypeVar('ResultT')


@dataclasses.dataclass
class GraphAgentState:
    """State kept across the execution of the agent graph."""

    message_history: list[_messages.ModelMessage]
    usage: _usage.Usage
    retries: int
    run_step: int

    def increment_retries(self, max_result_retries: int) -> None:
        self.retries += 1
        if self.retries > max_result_retries:
            raise exceptions.UnexpectedModelBehavior(
                f'Exceeded maximum retries ({max_result_retries}) for result validation'
            )


@dataclasses.dataclass
class GraphAgentDeps(Generic[DepsT, ResultDataT]):
    """Dependencies/config passed to the agent graph."""

    user_deps: DepsT

    prompt: str
    new_message_index: int

    model: models.Model
    model_settings: ModelSettings | None
    usage_limits: _usage.UsageLimits
    max_result_retries: int
    end_strategy: EndStrategy

    result_schema: _result.ResultSchema[ResultDataT] | None
    result_tools: list[ToolDefinition]
    result_validators: list[_result.ResultValidator[DepsT, ResultDataT]]

    function_tools: dict[str, Tool[DepsT]] = dataclasses.field(repr=False)

    run_span: logfire_api.LogfireSpan


@dataclasses.dataclass
class BaseUserPromptNode(BaseNode[GraphAgentState, GraphAgentDeps[DepsT, Any], NodeRunEndT], ABC):
    user_prompt: str

    system_prompts: tuple[str, ...]
    system_prompt_functions: list[_system_prompt.SystemPromptRunner[DepsT]]
    system_prompt_dynamic_functions: dict[str, _system_prompt.SystemPromptRunner[DepsT]]

    async def _get_first_message(
        self, ctx: GraphRunContext[GraphAgentState, GraphAgentDeps[DepsT, Any]]
    ) -> _messages.ModelRequest:
        run_context = build_run_context(ctx)
        history, next_message = await self._prepare_messages(self.user_prompt, ctx.state.message_history, run_context)
        ctx.state.message_history = history
        run_context.messages = history

        # TODO: We need to make it so that function_tools are not shared between runs
        #   See comment on the current_retry field of `Tool` for more details.
        for tool in ctx.deps.function_tools.values():
            tool.current_retry = 0
        return next_message

    async def _prepare_messages(
        self, user_prompt: str, message_history: list[_messages.ModelMessage] | None, run_context: RunContext[DepsT]
    ) -> tuple[list[_messages.ModelMessage], _messages.ModelRequest]:
        try:
            ctx_messages = get_captured_run_messages()
        except LookupError:
            messages: list[_messages.ModelMessage] = []
        else:
            if ctx_messages.used:
                messages = []
            else:
                messages = ctx_messages.messages
                ctx_messages.used = True

        if message_history:
            # Shallow copy messages
            messages.extend(message_history)
            # Reevaluate any dynamic system prompt parts
            await self._reevaluate_dynamic_prompts(messages, run_context)
            return messages, _messages.ModelRequest([_messages.UserPromptPart(user_prompt)])
        else:
            parts = await self._sys_parts(run_context)
            parts.append(_messages.UserPromptPart(user_prompt))
            return messages, _messages.ModelRequest(parts)

    async def _reevaluate_dynamic_prompts(
        self, messages: list[_messages.ModelMessage], run_context: RunContext[DepsT]
    ) -> None:
        """Reevaluate any `SystemPromptPart` with dynamic_ref in the provided messages by running the associated runner function."""
        # Only proceed if there's at least one dynamic runner.
        if self.system_prompt_dynamic_functions:
            for msg in messages:
                if isinstance(msg, _messages.ModelRequest):
                    for i, part in enumerate(msg.parts):
                        if isinstance(part, _messages.SystemPromptPart) and part.dynamic_ref:
                            # Look up the runner by its ref
                            if runner := self.system_prompt_dynamic_functions.get(part.dynamic_ref):
                                updated_part_content = await runner.run(run_context)
                                msg.parts[i] = _messages.SystemPromptPart(
                                    updated_part_content, dynamic_ref=part.dynamic_ref
                                )

    async def _sys_parts(self, run_context: RunContext[DepsT]) -> list[_messages.ModelRequestPart]:
        """Build the initial messages for the conversation."""
        messages: list[_messages.ModelRequestPart] = [_messages.SystemPromptPart(p) for p in self.system_prompts]
        for sys_prompt_runner in self.system_prompt_functions:
            prompt = await sys_prompt_runner.run(run_context)
            if sys_prompt_runner.dynamic:
                messages.append(_messages.SystemPromptPart(prompt, dynamic_ref=sys_prompt_runner.function.__qualname__))
            else:
                messages.append(_messages.SystemPromptPart(prompt))
        return messages


@dataclasses.dataclass
class UserPromptNode(BaseUserPromptNode[DepsT, NodeRunEndT]):
    async def run(
        self, ctx: GraphRunContext[GraphAgentState, GraphAgentDeps[DepsT, Any]]
    ) -> ModelRequestNode[DepsT, NodeRunEndT]:
        return ModelRequestNode[DepsT, NodeRunEndT](request=await self._get_first_message(ctx))


async def _prepare_request_parameters(
    ctx: GraphRunContext[GraphAgentState, GraphAgentDeps[DepsT, NodeRunEndT]],
) -> models.ModelRequestParameters:
    """Build tools and create an agent model."""
    function_tool_defs: list[ToolDefinition] = []

    run_context = build_run_context(ctx)

    async def add_tool(tool: Tool[DepsT]) -> None:
        ctx = run_context.replace_with(retry=tool.current_retry, tool_name=tool.name)
        if tool_def := await tool.prepare_tool_def(ctx):
            function_tool_defs.append(tool_def)

    await asyncio.gather(*map(add_tool, ctx.deps.function_tools.values()))

    result_schema = ctx.deps.result_schema
    return models.ModelRequestParameters(
        function_tools=function_tool_defs,
        allow_text_result=allow_text_result(result_schema),
        result_tools=result_schema.tool_defs() if result_schema is not None else [],
    )


@dataclasses.dataclass
class ModelRequestNode(BaseNode[GraphAgentState, GraphAgentDeps[DepsT, Any], NodeRunEndT]):
    """Make a request to the model using the last message in state.message_history."""

    request: _messages.ModelRequest

    _result: HandleResponseNode[DepsT, NodeRunEndT] | None = field(default=None, repr=False)
    _did_stream: bool = field(default=False, repr=False)

    async def run(
        self, ctx: GraphRunContext[GraphAgentState, GraphAgentDeps[DepsT, NodeRunEndT]]
    ) -> HandleResponseNode[DepsT, NodeRunEndT]:
        if self._result is not None:
            return self._result

        if self._did_stream:
            # `self._result` gets set when exiting the `stream` contextmanager, so hitting this
            # means that the stream was started but not finished before `run()` was called
            raise exceptions.AgentRunError('You must finish streaming before calling run()')

        return await self._make_request(ctx)

    @asynccontextmanager
    async def stream(
        self, ctx: GraphRunContext[GraphAgentState, GraphAgentDeps[DepsT, Any]]
    ) -> AsyncIterator[StreamedResponse]:
        if self._did_stream:
            raise exceptions.AgentRunError('stream() can only be called once')

        model_settings, model_request_parameters = await self._prepare_request(ctx)
        with _logfire.span('model request', run_step=ctx.state.run_step) as span:
            async with ctx.deps.model.request_stream(
                ctx.state.message_history, model_settings, model_request_parameters
            ) as streamed_response:
                self._did_stream = True
                ctx.state.usage.incr(_usage.Usage(), requests=1)
                yield streamed_response
                # In case the user didn't manually consume the full stream, ensure it is fully consumed here,
                # otherwise usage won't be properly counted:
                async for _ in streamed_response:
                    pass
            model_response = streamed_response.get()
            request_usage = streamed_response.usage()
            span.set_attribute('response', model_response)
            span.set_attribute('usage', request_usage)

        self._finish_handling(ctx, model_response, request_usage)
        assert self._result is not None  # this should be set by the previous line

    async def _make_request(
        self, ctx: GraphRunContext[GraphAgentState, GraphAgentDeps[DepsT, NodeRunEndT]]
    ) -> HandleResponseNode[DepsT, NodeRunEndT]:
        if self._result is not None:
            return self._result

        model_settings, model_request_parameters = await self._prepare_request(ctx)
        with _logfire.span('model request', run_step=ctx.state.run_step) as span:
            model_response, request_usage = await ctx.deps.model.request(
                ctx.state.message_history, model_settings, model_request_parameters
            )
            ctx.state.usage.incr(_usage.Usage(), requests=1)
            span.set_attribute('response', model_response)
            span.set_attribute('usage', request_usage)

        return self._finish_handling(ctx, model_response, request_usage)

    async def _prepare_request(
        self, ctx: GraphRunContext[GraphAgentState, GraphAgentDeps[DepsT, NodeRunEndT]]
    ) -> tuple[ModelSettings | None, ModelRequestParameters]:
        ctx.state.message_history.append(self.request)

        # Check usage
        if ctx.deps.usage_limits:
            ctx.deps.usage_limits.check_before_request(ctx.state.usage)

        # Increment run_step
        ctx.state.run_step += 1

        model_settings = merge_model_settings(ctx.deps.model_settings, None)
        with _logfire.span('preparing model request params {run_step=}', run_step=ctx.state.run_step):
            model_request_parameters = await _prepare_request_parameters(ctx)
        return model_settings, model_request_parameters

    def _finish_handling(
        self,
        ctx: GraphRunContext[GraphAgentState, GraphAgentDeps[DepsT, NodeRunEndT]],
        response: _messages.ModelResponse,
        usage: _usage.Usage,
    ) -> HandleResponseNode[DepsT, NodeRunEndT]:
        # Update usage
        ctx.state.usage.incr(usage, requests=0)
        if ctx.deps.usage_limits:
            ctx.deps.usage_limits.check_tokens(ctx.state.usage)

        # Append the model response to state.message_history
        ctx.state.message_history.append(response)

        # Set the `_result` attribute since we can't use `return` in an async iterator
        self._result = HandleResponseNode(response)

        return self._result


@dataclasses.dataclass
class HandleResponseNode(BaseNode[GraphAgentState, GraphAgentDeps[DepsT, Any], NodeRunEndT]):
    """Process the response from a model, decide whether to end the run or make a new request."""

    model_response: _messages.ModelResponse

    _stream: AsyncIterator[_messages.HandleResponseEvent] | None = field(default=None, repr=False)
    _next_node: ModelRequestNode[DepsT, NodeRunEndT] | FinalResultNode[DepsT, NodeRunEndT] | None = field(
        default=None, repr=False
    )
    _tool_responses: list[_messages.ModelRequestPart] = field(default_factory=list, repr=False)

    async def run(
        self, ctx: GraphRunContext[GraphAgentState, GraphAgentDeps[DepsT, NodeRunEndT]]
    ) -> Union[ModelRequestNode[DepsT, NodeRunEndT], FinalResultNode[DepsT, NodeRunEndT]]:  # noqa UP007
        async with self.stream(ctx):
            pass

        # the stream should set `self._next_node` before it ends:
        assert (next_node := self._next_node) is not None
        return next_node

    @asynccontextmanager
    async def stream(
        self, ctx: GraphRunContext[GraphAgentState, GraphAgentDeps[DepsT, Any]]
    ) -> AsyncIterator[AsyncIterator[_messages.HandleResponseEvent]]:
        with _logfire.span('handle model response', run_step=ctx.state.run_step) as handle_span:
            stream = self._run_stream(ctx)
            yield stream

            # Run the stream to completion if it was not finished:
            async for _event in stream:
                pass

            # Set the next node based on the final state of the stream
            next_node = self._next_node
            if isinstance(next_node, FinalResultNode):
                handle_span.set_attribute('result', next_node.data)
                handle_span.message = 'handle model response -> final result'
            elif tool_responses := self._tool_responses:
                # TODO: We could drop `self._tool_responses` if we drop this set_attribute
                #   I'm thinking it might be better to just create a span for the handling of each tool
                #   than to set an attribute here.
                handle_span.set_attribute('tool_responses', tool_responses)
                tool_responses_str = ' '.join(r.part_kind for r in tool_responses)
                handle_span.message = f'handle model response -> {tool_responses_str}'

    async def _run_stream(
        self, ctx: GraphRunContext[GraphAgentState, GraphAgentDeps[DepsT, Any]]
    ) -> AsyncIterator[_messages.HandleResponseEvent]:
        if self._stream is None:
            # Ensure that the stream is only run once

            async def _run_stream() -> AsyncIterator[_messages.HandleResponseEvent]:
                texts: list[str] = []
                tool_calls: list[_messages.ToolCallPart] = []
                for part in self.model_response.parts:
                    if isinstance(part, _messages.TextPart):
                        # ignore empty content for text parts, see #437
                        if part.content:
                            texts.append(part.content)
                    elif isinstance(part, _messages.ToolCallPart):
                        tool_calls.append(part)
                    else:
                        assert_never(part)

                # At the moment, we prioritize at least executing tool calls if they are present.
                # In the future, we'd consider making this configurable at the agent or run level.
                # This accounts for cases like anthropic returns that might contain a text response
                # and a tool call response, where the text response just indicates the tool call will happen.
                if tool_calls:
                    async for event in self._handle_tool_calls(ctx, tool_calls):
                        yield event
                elif texts:
                    # No events are emitted during the handling of text responses, so we don't need to yield anything
                    self._next_node = await self._handle_text_response(ctx, texts)
                else:
                    raise exceptions.UnexpectedModelBehavior('Received empty model response')

            self._stream = _run_stream()

        async for event in self._stream:
            yield event

    async def _handle_tool_calls(
        self,
        ctx: GraphRunContext[GraphAgentState, GraphAgentDeps[DepsT, NodeRunEndT]],
        tool_calls: list[_messages.ToolCallPart],
    ) -> AsyncIterator[_messages.HandleResponseEvent]:
        result_schema = ctx.deps.result_schema

        # first look for the result tool call
        final_result: MarkFinalResult[NodeRunEndT] | None = None
        parts: list[_messages.ModelRequestPart] = []
        if result_schema is not None:
            if match := result_schema.find_tool(tool_calls):
                call, result_tool = match
                try:
                    result_data = result_tool.validate(call)
                    result_data = await _validate_result(result_data, ctx, call)
                except _result.ToolRetryError as e:
                    # TODO: Should only increment retry stuff once per node execution, not for each tool call
                    #   Also, should increment the tool-specific retry count rather than the run retry count
                    ctx.state.increment_retries(ctx.deps.max_result_retries)
                    parts.append(e.tool_retry)
                else:
                    final_result = MarkFinalResult(result_data, call.tool_name)

        # Then build the other request parts based on end strategy
        tool_responses: list[_messages.ModelRequestPart] = self._tool_responses
        async for event in process_function_tools(
            tool_calls, final_result and final_result.tool_name, ctx, tool_responses
        ):
            yield event

        if final_result:
            self._next_node = FinalResultNode[DepsT, NodeRunEndT](final_result, tool_responses)
        else:
            if tool_responses:
                parts.extend(tool_responses)
            self._next_node = ModelRequestNode[DepsT, NodeRunEndT](_messages.ModelRequest(parts=parts))

    async def _handle_text_response(
        self,
        ctx: GraphRunContext[GraphAgentState, GraphAgentDeps[DepsT, NodeRunEndT]],
        texts: list[str],
    ) -> ModelRequestNode[DepsT, NodeRunEndT] | FinalResultNode[DepsT, NodeRunEndT]:
        result_schema = ctx.deps.result_schema

        text = '\n\n'.join(texts)
        if allow_text_result(result_schema):
            result_data_input = cast(NodeRunEndT, text)
            try:
                result_data = await _validate_result(result_data_input, ctx, None)
            except _result.ToolRetryError as e:
                ctx.state.increment_retries(ctx.deps.max_result_retries)
                return ModelRequestNode[DepsT, NodeRunEndT](_messages.ModelRequest(parts=[e.tool_retry]))
            else:
                return FinalResultNode[DepsT, NodeRunEndT](MarkFinalResult(result_data, None))
        else:
            ctx.state.increment_retries(ctx.deps.max_result_retries)
            return ModelRequestNode[DepsT, NodeRunEndT](
                _messages.ModelRequest(
                    parts=[
                        _messages.RetryPromptPart(
                            content='Plain text responses are not permitted, please call one of the functions instead.',
                        )
                    ]
                )
            )


@dataclasses.dataclass
class FinalResultNode(BaseNode[GraphAgentState, GraphAgentDeps[DepsT, Any], MarkFinalResult[NodeRunEndT]]):
    """Produce the final result of the run."""

    data: MarkFinalResult[NodeRunEndT]
    """The final result data."""
    extra_parts: list[_messages.ModelRequestPart] = dataclasses.field(default_factory=list)

    async def run(
        self, ctx: GraphRunContext[GraphAgentState, GraphAgentDeps[DepsT, NodeRunEndT]]
    ) -> End[MarkFinalResult[NodeRunEndT]]:
        run_span = ctx.deps.run_span
        usage = ctx.state.usage
        messages = ctx.state.message_history

        # TODO: For backwards compatibility, append a new ModelRequest using the tool returns and retries
        if self.extra_parts:
            messages.append(_messages.ModelRequest(parts=self.extra_parts))

        # TODO: Set this attribute somewhere
        # handle_span = self.handle_model_response_span
        # handle_span.set_attribute('final_data', self.data)
        run_span.set_attribute('usage', usage)
        run_span.set_attribute('all_messages', messages)

        # End the run with self.data
        return End(self.data)


def build_run_context(ctx: GraphRunContext[GraphAgentState, GraphAgentDeps[DepsT, Any]]) -> RunContext[DepsT]:
    return RunContext[DepsT](
        deps=ctx.deps.user_deps,
        model=ctx.deps.model,
        usage=ctx.state.usage,
        prompt=ctx.deps.prompt,
        messages=ctx.state.message_history,
        run_step=ctx.state.run_step,
    )


async def process_function_tools(
    tool_calls: list[_messages.ToolCallPart],
    result_tool_name: str | None,
    ctx: GraphRunContext[GraphAgentState, GraphAgentDeps[DepsT, NodeRunEndT]],
    output_parts: list[_messages.ModelRequestPart],
) -> AsyncIterator[_messages.HandleResponseEvent]:
    """Process function (non-result) tool calls in parallel.

    Also add stub return parts for any other tools that need it.

    Because async iterators can't have return values, we use `parts` as an output argument.
    """
    stub_function_tools = bool(result_tool_name) and ctx.deps.end_strategy == 'early'
    result_schema = ctx.deps.result_schema

    # we rely on the fact that if we found a result, it's the first result tool in the last
    found_used_result_tool = False
    run_context = build_run_context(ctx)

    calls_to_run: list[tuple[Tool[DepsT], _messages.ToolCallPart]] = []
    call_index_to_event_id: dict[int, str] = {}
    for call in tool_calls:
        if call.tool_name == result_tool_name and not found_used_result_tool:
            found_used_result_tool = True
            output_parts.append(
                _messages.ToolReturnPart(
                    tool_name=call.tool_name,
                    content='Final result processed.',
                    tool_call_id=call.tool_call_id,
                )
            )
        elif tool := ctx.deps.function_tools.get(call.tool_name):
            if stub_function_tools:
                output_parts.append(
                    _messages.ToolReturnPart(
                        tool_name=call.tool_name,
                        content='Tool not executed - a final result was already processed.',
                        tool_call_id=call.tool_call_id,
                    )
                )
            else:
                event = _messages.FunctionToolCallEvent(call)
                yield event
                call_index_to_event_id[len(calls_to_run)] = event.call_id
                calls_to_run.append((tool, call))
        elif result_schema is not None and call.tool_name in result_schema.tools:
            # if tool_name is in _result_schema, it means we found a result tool but an error occurred in
            # validation, we don't add another part here
            if result_tool_name is not None:
                part = _messages.ToolReturnPart(
                    tool_name=call.tool_name,
                    content='Result tool not used - a final result was already processed.',
                    tool_call_id=call.tool_call_id,
                )
                output_parts.append(part)
        else:
            output_parts.append(_unknown_tool(call.tool_name, ctx))

    if not calls_to_run:
        return

    # Run all tool tasks in parallel
    results_by_index: dict[int, _messages.ModelRequestPart] = {}
    with _logfire.span('running {tools=}', tools=[call.tool_name for _, call in calls_to_run]):
        # TODO: Should we wrap each individual tool call in a dedicated span?
        tasks = [asyncio.create_task(tool.run(call, run_context), name=call.tool_name) for tool, call in calls_to_run]
        pending = tasks
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                index = tasks.index(task)
                result = task.result()
                yield _messages.FunctionToolResultEvent(result, call_id=call_index_to_event_id[index])
                if isinstance(result, (_messages.ToolReturnPart, _messages.RetryPromptPart)):
                    results_by_index[index] = result
                else:
                    assert_never(result)

    # We append the results at the end, rather than as they are received, to retain a consistent ordering
    # This is mostly just to simplify testing
    for k in sorted(results_by_index):
        output_parts.append(results_by_index[k])


def _unknown_tool(
    tool_name: str,
    ctx: GraphRunContext[GraphAgentState, GraphAgentDeps[DepsT, NodeRunEndT]],
) -> _messages.RetryPromptPart:
    ctx.state.increment_retries(ctx.deps.max_result_retries)
    tool_names = list(ctx.deps.function_tools.keys())
    if result_schema := ctx.deps.result_schema:
        tool_names.extend(result_schema.tool_names())

    if tool_names:
        msg = f'Available tools: {", ".join(tool_names)}'
    else:
        msg = 'No tools available.'

    return _messages.RetryPromptPart(content=f'Unknown tool name: {tool_name!r}. {msg}')


async def _validate_result(
    result_data: T,
    ctx: GraphRunContext[GraphAgentState, GraphAgentDeps[DepsT, T]],
    tool_call: _messages.ToolCallPart | None,
) -> T:
    for validator in ctx.deps.result_validators:
        run_context = build_run_context(ctx)
        result_data = await validator.validate(result_data, tool_call, run_context)
    return result_data


def allow_text_result(result_schema: _result.ResultSchema[Any] | None) -> bool:
    return result_schema is None or result_schema.allow_text_result


@dataclasses.dataclass
class _RunMessages:
    messages: list[_messages.ModelMessage]
    used: bool = False


_messages_ctx_var: ContextVar[_RunMessages] = ContextVar('var')


@contextmanager
def capture_run_messages() -> Iterator[list[_messages.ModelMessage]]:
    """Context manager to access the messages used in a [`run`][pydantic_ai.Agent.run], [`run_sync`][pydantic_ai.Agent.run_sync], or [`run_stream`][pydantic_ai.Agent.run_stream] call.

    Useful when a run may raise an exception, see [model errors](../agents.md#model-errors) for more information.

    Examples:
    ```python
    from pydantic_ai import Agent, capture_run_messages

    agent = Agent('test')

    with capture_run_messages() as messages:
        try:
            result = agent.run_sync('foobar')
        except Exception:
            print(messages)
            raise
    ```

    !!! note
        If you call `run`, `run_sync`, or `run_stream` more than once within a single `capture_run_messages` context,
        `messages` will represent the messages exchanged during the first call only.
    """
    try:
        yield _messages_ctx_var.get().messages
    except LookupError:
        messages: list[_messages.ModelMessage] = []
        token = _messages_ctx_var.set(_RunMessages(messages))
        try:
            yield messages
        finally:
            _messages_ctx_var.reset(token)


def get_captured_run_messages() -> _RunMessages:
    return _messages_ctx_var.get()


def build_agent_graph(
    name: str | None, deps_type: type[DepsT], result_type: type[ResultT]
) -> Graph[GraphAgentState, GraphAgentDeps[DepsT, Any], MarkFinalResult[ResultT]]:
    # We'll define the known node classes:
    nodes = (
        UserPromptNode[DepsT],
        ModelRequestNode[DepsT],
        HandleResponseNode[DepsT],
        FinalResultNode[DepsT, ResultT],
    )
    graph = Graph[GraphAgentState, GraphAgentDeps[DepsT, Any], MarkFinalResult[ResultT]](
        nodes=nodes,
        name=name or 'Agent',
        state_type=GraphAgentState,
        run_end_type=MarkFinalResult[result_type],
        auto_instrument=False,
    )
    return graph
