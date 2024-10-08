import json
import signal
from copy import copy
from contextlib import contextmanager
from dataclasses import dataclass
import asyncio

from . import models


class Conversation:
    def __init__(self, conversation_data):
        self.messages = conversation_data.get("messages", [])
        self.plugins = conversation_data.get("plugins", [])
        self.tags = conversation_data.get("tags", [])
        self.model = conversation_data.get("model")
        self.usage = conversation_data.get("usage")
        self.completion = conversation_data.get("completion")
        self.timestamp = conversation_data.get("timestamp")

    def append(self, role, content):
        self.messages.append({"role": role, "content": content})

    def __contains__(self, search_term):
        question = (
            self.messages[-2]["content"]
            if len(self.messages) > 1
            else self.messages[-1]["content"]
        )
        return search_term in question

    def to_json(self):
        return json.dumps(self.__dict__)

    def find(self, predicate):
        for message in reversed(self.messages):
            if predicate(message):
                return message
        raise ValueError("No matching message found")

    async def complete(self, *, stream=True, callback=None):
        if stream:
            completion = await stream_request(self.messages, self.model, callback)
        else:
            completion = synchroneous_request(self.messages, self.model, callback)

        # TODO: handle multiple choices
        response_message = completion.choices[0].message
        self.append(
            role=response_message.role,
            content=response_message.content,
        )
        self.completion = completion
        self.usage = completion_usage(self.messages[:-1], self.model, completion)

        return response_message

    def add_tag(self, tag):
        self.tags = [t for t in self.tags if t != tag]
        self.tags.append(tag)

    def clone(self, *, model=None):
        data = copy(self.__dict__)
        data["tags"] = (
            [data["tags"][-1]]
            if data["tags"] and not is_personality(data["tags"][-1])
            else []
        )
        data.pop("completion", None)
        if model:
            data["model"] = model
        return type(self)(data)


def is_personality(tag):
    return tag.startswith("^")


def completion_usage(request_messages, model, completion):
    if "usage" in completion:
        return completion.usage

    import tiktoken

    try:
        encoding = tiktoken.encoding_for_model(model)
    except KeyError:
        encoding = tiktoken.encoding_for_model("gpt-3.5-turbo")

    request_text = " ".join(
        "role: " + x["role"] + " content: " + x["content"] + "\n"
        for x in request_messages
    )
    request_tokens = len(encoding.encode(request_text))
    completion_tokens = len(encoding.encode(completion.choices[0].message.content))
    return {
        "prompt_tokens": request_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": request_tokens + completion_tokens,
    }


def synchroneous_request(request_messages, model, callback):
    from openai import OpenAI

    client = OpenAI(
        base_url=models.api_base(model),
        api_key=models.api_key(model),
    )

    completion = client.chat.completions.create(
        model=models.api_model_name(model),
        messages=request_messages,
    )
    if callback:
        callback(completion.choices[0].message.content)
    return completion


@contextmanager
def handle_sigint():
    @dataclass
    class State:
        running: bool

    state = State(running=True)

    def handle_sigint(_signal, _frame):
        state.running = False

    try:
        signal.signal(signal.SIGINT, handle_sigint)
        yield state
    finally:
        signal.signal(signal.SIGINT, signal.SIG_DFL)


async def stream_request(request_messages, model, callback):
    from openai import AsyncOpenAI

    aclient = AsyncOpenAI(
        base_url=models.api_base(model),
        api_key=models.api_key(model),
    )

    stream = await aclient.chat.completions.create(
        model=models.api_model_name(model),
        messages=request_messages,
        stream=True,
    )

    response = await accumulate_streaming_response(stream, callback)
    await stream.close()

    return response


async def accumulate_streaming_response(stream, callback=None):
    from openai.types.chat import ChatCompletionMessage
    from openai.types.completion import Completion, CompletionChoice

    if callback is None:
        callback = lambda _: None  # noqa: E731

    completion = {}

    accumulated_content = ""

    try:
        async for chunk in stream:
            if chunk.choices[0].delta.content:
                chunk_content = chunk.choices[0].delta.content
                accumulated_content += chunk_content
                callback(chunk_content)

            if completion.get("id") is None:
                completion["id"] = chunk.id
            if completion.get("created") is None:
                completion["created"] = chunk.created
            if completion.get("model") is None:
                completion["model"] = chunk.model

    except asyncio.CancelledError:
        pass

    message = ChatCompletionMessage(
        text=accumulated_content,
        content=accumulated_content,
        role="assistant",
        function_call=None,
        tool_calls=None,
    )

    choice = CompletionChoice(
        finish_reason="stop",
        index=0,
        text=accumulated_content,
        message=message,
        logprobs=None,
    )

    return Completion(
        object="text_completion",
        usage=None,  # Usage information is not available in streaming mode
        choices=[choice],
        **completion,
    )


def get_choice_content(completion, index=0):
    return choices_by_index(completion.choices).get(index, {}).content


def choices_by_index(choices):
    return {x["index"]: x["delta"] for x in choices}


def add_deltas(completion, chunk):
    if not completion:
        completion = copy(chunk)
        completion.choices = [{"message": {}} for choice in chunk["choices"]]

    choices = completion.choices

    for idx, delta in choices_by_index(chunk["choices"]).items():
        choices[idx]["message"] = append_delta(choices[idx]["message"], delta)

    return completion


def append_delta(message, delta):
    result = copy(message)
    for key, value in delta.items():
        if key == "role":
            result[key] = value
        else:
            result[key] = message.get(key, "") + value
    return result
