import os
import sys
import itertools
import functools
import asyncio
from datetime import datetime, timezone
import dateutil.parser
from pathlib import Path
import click
from click_default_group import DefaultGroup
import prompt_toolkit

from .log import (
    write_log,
    search_conversations,
    conversation_log,
    create_initial_log,
    find_log,
)
from .conversation import Conversation, is_personality
from . import models

from .models import get_models

MESSAGE_COLORS = {
    "user": (186, 85, 211),
    "system": (100, 150, 200),
    "assistant": None,
}

DEFAULT_MODEL = "gpt-3.5-turbo-1106"


class PartialChoice(click.types.ParamType):
    def __init__(self, name, get_choices, fail_message, **kwargs):
        self.name = name
        self._get_choices = get_choices
        self.fail_message = fail_message
        super().__init__(**kwargs)

    @property
    def choices(self):
        return self._get_choices()

    def convert(self, value, param, ctx):
        for choice in self.choices:
            if value in choice:
                return choice
        return self.fail(value + "\n" + self.fail_message, param, ctx)


MODEL_CHOICE = PartialChoice(
    name="MODEL",
    get_choices=lambda: [model["id"] for model in get_models()],
    fail_message="Run `chatcli models list` to see available models.",
)


class LogFileLocation(click.Path):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    def convert(self, *args, **kwargs):
        path = super().convert(*args, **kwargs)
        return find_log(path)


log_file_option = click.option(
    "--log-file",
    "--log",
    type=LogFileLocation(exists=True, path_type=Path),
    default=lambda: find_log(Path()),
    help="Conversation log file path, or the directory to search for a log file.",
)


def coro(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        return asyncio.run(f(*args, **kwargs))

    return wrapper


@click.group(cls=DefaultGroup, default="chat", default_if_no_args=True)
@click.version_option()
def cli():
    pass


def select_conversation(command):
    @click.argument("offset", type=int, required=False)
    @click.option("-s", "--search", help="Select by search term")
    @click.option("-t", "--tag", help="Select by tag")
    @log_file_option
    @functools.wraps(command)
    def wrapper(*args, log_file=None, offset=None, search=None, tag=None, **kwargs):
        if (kwargs.get("continue_conversation") or kwargs.get("retry")) and not offset:
            offset = 1
        if kwargs.get("select_personality") and not (tag or search or offset):
            tag = "^" + kwargs["select_personality"]
        kwargs.pop("select_personality", None)
        if kwargs.get("new") and not (tag or search or offset):
            conversation = Conversation({})
        else:
            conversation = get_logged_conversation(
                log_file, offset=offset, search=search, tag=tag
            )
        return command(
            *args,
            conversation=conversation,
            log_file=log_file,
            **kwargs,
        )

    return wrapper


def filter_conversations(command):
    @click.argument("offsets", type=int, nargs=-1)
    @click.option("-s", "--search", help="Select by search term")
    @click.option("-t", "--tag", help="Select by tag")
    @log_file_option
    @functools.wraps(command)
    def wrapper(*args, log_file=None, offsets=None, search=None, tag=None, **kwargs):
        if kwargs.get("select_personality") and not (tag or search):
            tag = "^" + kwargs["select_personality"]
        kwargs.pop("select_personality", None)
        return command(
            *args,
            conversations=search_conversations(
                log_file, offsets=offsets, search=search, tag=tag
            ),
            log_file=log_file,
            **kwargs,
        )

    return wrapper


@cli.command(help="Ask a question of ChatGPT.")
@click.option(
    "-q", "--quick", is_flag=True, help="Just handle a one single-line question."
)
@click.option(
    "-c",
    "--continue_conversation",
    "--continue",
    is_flag=True,
    help="Continue previous conversation.",
)
@click.option(
    "-p",
    "--personality",
    "select_personality",
    default="default",
    help="Personality to use.",
)
@click.option(
    "-f",
    "--file",
    type=click.Path(exists=True),
    multiple=True,
    help="Add a file to the conversation for context.",
)
@click.option("-r", "--retry", is_flag=True, help="Retry previous question")
@click.option("--stream/--sync", default=True, help="Stream or sync mode.")
@click.option(
    "-m",
    "--model",
    type=MODEL_CHOICE,
    help="Model to use. Run `chatcli models list` to see available models.",
)
@click.option("--plugin", "additional_plugins", multiple=True, help="Load a plugin.")
@select_conversation
def chat(log_file, conversation, **kwargs):
    conversation = conversation.clone()

    for filename in kwargs["file"]:
        with Path(filename).open(encoding="utf-8") as fh:
            file_contents = fh.read()

        conversation.append(
            "user", f"The file {filename!r} contains:\n```\n{file_contents}```"
        )

    conversation.plugins.extend(kwargs["additional_plugins"])
    conversation.model = kwargs["model"] or conversation.model or DEFAULT_MODEL

    quick = kwargs["quick"] or not os.isatty(0)
    multiline = not quick

    if kwargs["retry"]:
        conversation.messages.pop()
        add_answer(log_file, conversation, stream=kwargs["stream"])
        if kwargs["quick"]:
            return

    run_conversation(
        log_file,
        conversation,
        multiline=multiline,
        quick=quick,
        stream=kwargs["stream"],
    )


@cli.command(help="Create initial conversation log.")
@click.option(
    "-r",
    "--reinit",
    is_flag=True,
    help="re-initialize the personalities to default values",
)
def init(reinit):
    try:
        create_initial_log(reinit)
    except FileExistsError as error:
        click.echo(f"{error}: Conversation log already exists.", file=sys.stderr)
        sys.exit(1)


@cli.command(help="Add a message to a new or existing conversation.")
@click.option("--multiline/--singleline", default=True)
@click.option("-p", "--personality", help="")
@click.option(
    "--role", type=click.Choice(["system", "user", "assistant"]), default="system"
)
@click.option("--plugin", multiple="True", help="Activate plugins.")
@click.option("-m", "--model", type=MODEL_CHOICE)
@click.option("--plugin", "additional_plugins", multiple=True, help="Load a plugin.")
@click.option(
    "--new/--continue",
    "-n/-c",
    default=True,
    help="Create a new conversation or continue.",
)
@select_conversation
def add(log_file, conversation, personality, role, multiline, **kwargs):
    tags = conversation.tags
    tags_to_apply = [tags[-1]] if tags and not is_personality(tags[-1]) else []

    conversation.plugins.extend(kwargs["additional_plugins"])
    conversation.tags = tags_to_apply
    conversation.model = kwargs["model"] or conversation.model or "gpt-3.5-turbo"

    if personality:
        conversation.tags.append("^" + personality)

    if multiline and os.isatty(0):
        click.echo("(Finish input with <Alt-Enter> or <Esc><Enter>)")
    content = prompt(multiline=True)
    conversation.append(role, content)
    write_log(log_file, conversation)


def merge_list(input_list, additions):
    for item in additions:
        if item not in input_list:
            input_list.append(item)


@cli.command(help="Edit the last message in a conversation.")
@click.option("-m", "--model", type=MODEL_CHOICE)
@click.option("--prompt/--no-prompt", default=True)
@click.option(
    "-p",
    "--personality",
    "select_personality",
    default="default",
    help="Personality to use.",
)
@select_conversation
def edit(log_file, conversation, **kwargs):
    if kwargs["prompt"]:
        content = prompt(
            multiline=True,
            default=conversation.messages[-1]["content"],
        )

        conversation.messages[-1]["content"] = content

    if kwargs.get("model"):
        conversation.model = kwargs["model"]

    if kwargs.get("personality"):
        conversation.add_tag(f"^{kwargs['personality']}")

    write_log(log_file, conversation)


@cli.command(help="Remove the last message in a conversation.")
@select_conversation
def drop(log_file, conversation):
    conversation.messages.pop()
    write_log(log_file, conversation)


@cli.command(help="Create a new conversation by merging existing conversations.")
@click.option("-p", "--personality", help="Set personality for new conversation.")
@filter_conversations
def merge(log_file, conversations, personality):
    merged_conversation = {
        "messages": [],
        "tags": [],
        "plugins": [],
        "model": None,
    }
    if personality:
        merged_conversation["tags"].append("^" + personality)

    for _, item in reversed(list(conversations)):
        merge_list(merged_conversation["messages"], item.messages)
        merge_list(
            merged_conversation["tags"],
            (tag for tag in item.tags if not is_personality(tag)),
        )
        merge_list(merged_conversation["plugins"], item.plugins)
        merged_conversation["model"] = item.model or merged_conversation["model"]

    write_log(log_file, Conversation(merged_conversation))


@cli.command(help="List tags.", name="tags")
@log_file_option
def list_tags(log_file=None):
    tags = set()
    for conversation in conversation_log(log_file):
        tags |= set(conversation.tags)
    for tag in sorted(tags):
        click.echo(tag)


@cli.command(help="List personalities.", name="personalities")
@log_file_option
def list_personalities(log_file):
    personalities = set()
    for conversation in conversation_log(log_file):
        for tag in conversation.tags:
            if is_personality(tag):
                personalities.add(tag[1:])
    for personality in sorted(personalities):
        click.echo(personality)


@cli.command(help="Add tags to an conversation.", name="tag")
@click.argument("new_tag")
@select_conversation
def add_tag(new_tag, log_file, conversation):
    conversation.add_tag(new_tag)
    write_log(log_file, conversation)


@cli.command(help="Remove tags from an conversation.")
@click.argument("tag_to_remove")
@select_conversation
def untag(tag_to_remove, log_file, conversation):
    conversation.tags = [tag for tag in conversation.tags if tag != tag_to_remove]
    write_log(log_file, conversation)


@cli.command(help="Current tag")
@select_conversation
def show_tag(conversation, **_kwargs):
    if conversation.tags:
        click.echo(conversation.tags[-1])


@cli.command(help="Show a conversation.")
@click.option(
    "-p",
    "--personality",
    "select_personality",
    help="Select conversation by personality.",
)
@select_conversation
@click.option(
    "-l/-s",
    "--long/--short",
    help="Show full conversation or just the most recent message.",
)
@click.option(
    "--format-json", "--json", is_flag=True, help="Output conversation in JSON format."
)
def show(long, conversation, format_json, **_kwargs):
    if format_json:
        click.echo(conversation.to_json())
        return

    messages = conversation.messages if long else conversation.messages[-1:]

    for message in messages:
        prefix = ""
        if message["role"] == "user":
            prefix = ">> "
        click.echo(
            click.style(prefix + message["content"], fg=MESSAGE_COLORS[message["role"]])
        )


@cli.command(help="Display conversation log.")
@click.option(
    "-p",
    "--personality",
    "select_personality",
    help="Select conversation by personality.",
)
@filter_conversations
@click.option("--limit", "-l", type=int, help="Limit number of results")
@click.option("--usage", "-u", is_flag=True, help="Show token usage")
@click.option("--cost", is_flag=True, help="Show token cost")
@click.option("--plugins", is_flag=True, help="Show enabled plugins")
@click.option("-m", "--model", is_flag=True, help="Show model")
@click.option(
    "--json", "format_json", is_flag=True, help="Output conversation in JSON format."
)
def log(conversations, limit, format_json, **kwargs):
    for offset, conversation in reversed(list(itertools.islice(conversations, limit))):
        if format_json:
            click.echo(conversation.to_json())
            continue
        try:
            question = conversation.find(
                lambda message: message["role"] != "assistant"
            )["content"]
        except ValueError:
            question = conversation.messages[-1]["content"]
        trimmed_message = question.strip().split("\n", 1)[0][:80]

        fields = []
        fields.append(click.style(f"{offset: 4d}:", fg="blue"))

        if kwargs["usage"]:
            total_tokens = (
                conversation.usage["total_tokens"] if conversation.usage else 0
            )
            fields.append(f"{total_tokens: 5d}")

        if kwargs["cost"]:
            fields.append(f"${conversation_cost(conversation): 2.3f}")

        fields.append(trimmed_message)
        if conversation.tags:
            fields.append(click.style(",".join(conversation.tags), fg="green"))

        if kwargs["plugins"]:
            fields.append(",".join(conversation.plugins))

        if kwargs["model"]:
            fields.append(click.style(conversation.model, fg="yellow"))

        click.echo(" ".join(fields))


cli.add_command(models.models)


def run_conversation(
    log_file, conversation, *, stream=True, multiline=True, quick=False
):
    if multiline and os.isatty(0):
        click.echo("(Finish input with <Alt-Enter> or <Esc><Enter>)")

    while True:
        question = prompt(multiline=multiline)
        if not question:
            break
        conversation.append("user", question)
        add_answer(log_file, conversation, stream=stream)

        if quick:
            break


def prompt(*, multiline=True, **kwargs):
    if os.isatty(0):
        try:
            return prompt_toolkit.prompt(
                ">> ",
                multiline=multiline,
                prompt_continuation=".. ",
                **kwargs,
            ).strip()
        except EOFError:
            return None
    else:
        return sys.stdin.read().strip()


@cli.command(help="Add an answer to a question")
@click.option("--stream/--sync", default=True, help="Stream or sync mode.")
@click.option("-m", "--model", type=MODEL_CHOICE)
@select_conversation
def answer(log_file, conversation, stream, **kwargs):
    conversation = conversation.clone(**kwargs)
    add_answer(log_file, conversation, stream=stream)


@coro
async def add_answer(log_file, conversation, *, stream=True):
    while True:
        response = await conversation.complete(
            stream=stream, callback=lambda token: click.echo(token, nl=False)
        )
        click.echo()
        write_log(
            log_file,
            conversation,
            completion=conversation.completion,
            usage=conversation.usage,
        )
        from . import plugins

        plugin_response = plugins.evaluate_plugins(
            response.content, conversation.plugins
        )
        if not plugin_response:
            break
        click.echo(click.style(plugin_response, fg=(200, 180, 90)))
        conversation.append("user", plugin_response)


def conversation_cost(conversation):
    usage_costs = {model["id"]: model["pricing"] for model in get_models()}

    if not conversation.usage:
        return 0
    model = conversation.completion["model"]
    model_price = (
        usage_costs.get(model)
        or usage_costs.get("-".join(model.split("-")[:-1]))
        or usage_costs["openrouter/" + model]
    )

    usage = conversation.usage
    return (
        float(model_price["prompt"]) * usage["prompt_tokens"]
        + float(model_price["completion"]) * usage["completion_tokens"]
    )


@cli.command(help="Display number of tokens and token cost.", name="usage")
@click.option("--today", is_flag=True, help="Show usage for today only.")
@log_file_option
def show_usage(today, log_file):
    conversations = conversation_log(log_file)

    def is_today(conversation):
        return (
            dateutil.parser.parse(conversation.timestamp).date()
            == datetime.now(tz=timezone.utc).date()
        )

    if today:
        conversations = [c for c in conversations if is_today(c)]
    tokens = sum(
        conversation.usage["total_tokens"]
        for conversation in conversations
        if conversation.usage
    )

    total_cost = sum(conversation_cost(conversation) for conversation in conversations)
    click.echo(f"Tokens: {tokens}")
    click.echo(f"Cost: ${total_cost:.2f}")


def get_logged_conversation(log_path, offset, search=None, tag=None):
    offsets = [offset] if offset else []
    try:
        return next(search_conversations(log_path, offsets, search, tag))[1]
    except StopIteration:
        click.echo("Matching conversation not found", file=sys.stderr)
        sys.exit(1)


def main():
    try:
        cli()
    except FileNotFoundError as error:
        click.echo(f"{error}: Chatcli not initialized. Run `chatcli init` first.")
        sys.exit(1)


if __name__ == "__main__":
    main()
