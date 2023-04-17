import json
import openai
import tiktoken


class Conversation:
    def __init__(self, messages=None, plugins=None, tags=None, model=None, usage=None, completion=None, timestamp=None):
        self.messages = messages or []
        self.plugins = plugins or []
        self.tags = tags or []
        self.model = model
        self.usage = usage
        self.completion = completion
        self.timestamp = timestamp

    def append(self, role, content):
        self.messages.append({"role": role, "content": content})

    def __contains__(self, search_term):
        if len(self.messages) > 1:
            question = self.messages[-2]["content"]
        else:
            question = self.messages[-1]["content"]
        return search_term in question

    def to_json(self):
        return json.dumps(self.__dict__)

    def find(self, predicate):
        for message in reversed(self.messages):
            if predicate(message):
                return message
        raise ValueError("No matching message found")

    def complete(self, *, stream=True, callback=None):
        if stream:
            completion = stream_request(self.messages, self.model, callback)
        else:
            completion = synchroneous_request(self.messages, self.model, callback)

        # TODO: handle multiple choices
        response_message = completion["choices"][0]["message"]
        self.append(**response_message)
        self.completion = completion
        self.usage = completion_usage(self.messages[:-1], self.model, completion)

        return response_message


def completion_usage(request_messages, model, completion):
    if "usage" in completion:
        return completion["usage"]

    encoding = tiktoken.encoding_for_model(model)
    request_text = " ".join("role: " + x["role"] + " content: " + x["content"] + "\n" for x in request_messages)
    request_tokens = len(encoding.encode(request_text))
    completion_tokens = len(encoding.encode(completion["choices"][0]["message"]["content"]))
    return {
        "prompt_tokens": request_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": request_tokens + completion_tokens,
    }


def synchroneous_request(request_messages, model, callback):
    completion = openai.ChatCompletion.create(model=model, messages=request_messages)
    if callback:
        callback(completion["choices"][0]["message"]["content"])
    return completion


def stream_request(request_messages, model, callback):
    completion = {}
    for chunk in openai.ChatCompletion.create(model=model, messages=request_messages, stream=True):
        if not completion:
            for key, value in chunk.items():
                completion[key] = value
            completion["choices"] = [{"message": {}} for choice in chunk["choices"]]

        for choice in chunk["choices"]:
            if choice.get("delta"):
                for key, value in choice["delta"].items():
                    message = completion["choices"][choice["index"]]["message"]
                    if key not in message:
                        message[key] = ""
                    message[key] += value

        content_chunk = chunk["choices"][0]["delta"].get("content")
        if content_chunk and callback:
            callback(content_chunk, nl=False)

    callback()
    return completion
