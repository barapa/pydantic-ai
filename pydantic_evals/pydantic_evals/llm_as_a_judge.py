from textwrap import dedent
from typing import Any

from pydantic import BaseModel, Field
from pydantic_core import to_json

from pydantic_ai import Agent, models


class GradingOutput(BaseModel, populate_by_name=True):
    """The output of a grading operation."""

    reason: str
    pass_: bool = Field(alias='pass')
    score: float


_judge_output_agent = Agent(
    system_prompt=dedent(
        """
        You are grading output according to a user-specified rubric. If the statement in the rubric is true, then the output passes the test. You respond with a JSON object with this structure: {reason: string, pass: boolean, score: number}

        Examples:

        <Output>Hello world</Output>
        <Rubric>Content contains a greeting</Rubric>
        {"reason": "the content contains the word 'Hello'", "pass": true, "score": 1.0}

        <Output>Avast ye swabs, repel the invaders!</Output>
        <Rubric>Does not speak like a pirate</Rubric>
        {"reason": "'avast ye' is a common pirate term", "pass": false, "score": 0.0}
        """
    ),
    result_type=GradingOutput,
)


async def judge_output(
    output: Any, rubric: str, model: models.Model | models.KnownModelName = 'openai:gpt-4o'
) -> GradingOutput:
    """Judge the output of a model based on a rubric."""
    user_prompt = f'<Output>\n{_stringify(output)}\n</Output>\n<Rubric>\n{rubric}\n</Rubric>'
    return (await _judge_output_agent.run(user_prompt, model=model)).data


_judge_input_output_agent = Agent(
    system_prompt=dedent(
        """
        You are grading output according to a user-specified rubric. If the statement in the rubric is true for the provided input and output, then the output passes the test. You respond with a JSON object with this structure: {reason: string, pass: boolean, score: number}

        Examples:

        <Input>Hello world</Input>
        <Output>Hello</Output>
        <Rubric>Content contains a greeting word which is present in the input</Rubric>
        {"reason": "the content contains the word 'Hello'", "pass": true, "score": 1.0}

        <Input>Pirate</Input>
        <Output>Avast ye swabs, repel the invaders!</Output>
        <Rubric>Does not speak in the style described by the input</Rubric>
        {"reason": "'avast ye' is a common pirate term", "pass": false, "score": 0.0}
        """
    ),
    result_type=GradingOutput,
)


async def judge_input_output(
    inputs: Any, output: Any, rubric: str, model: models.Model | models.KnownModelName = 'openai:gpt-4o'
) -> GradingOutput:
    """Judge the output of a model based on the inputs and a rubric."""
    user_prompt = f'<Input>\n{_stringify(inputs)}\n</Input><Output>\n{_stringify(output)}\n</Output>\n<Rubric>\n{rubric}\n</Rubric>'
    return (await _judge_input_output_agent.run(user_prompt, model=model)).data


# async def judge(rubric: str, output: Any, inputs: Any = UNSET, expected_output: Any = UNSET, model: models.Model | models.KnownModelName = 'openai:gpt-4o') -> GradingOutput:
#     # TODO: Implement something like this that has a cleaner API for providing different kinds of data to the LLM
#     if inputs is UNSET:
#         return await judge_output(output, rubric, model=model)
#     if expected_output is UNSET:
#         return await judge_input_output(inputs, output, rubric, model=model)
#     raise ValueError('expected_output must be unset if inputs is set')


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return to_json(value).decode()
    except Exception:
        return repr(value)
