import os
import sys
import itertools
import functools
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
)
from .plugins import evaluate_plugins
from .conversation import Conversation

MODELS = [
    "gpt-4",
    "gpt-3.5-turbo",
]

MESSAGE_COLORS = {
    "user": (186, 85, 211),
    "system": (100, 150, 200),
    "assistant": None,
}

USAGE_COSTS = {
    "gpt-3.5-turbo": {"prompt_tokens": 0.002, "completion_tokens": 0.002},
    "gpt-4": {"prompt_tokens": 0.03, "completion_tokens": 0.06},
}


@click.group(cls=DefaultGroup, default="chat", default_if_no_args=True)
@click.version_option()
def cli():
    pass


def cli_search_options(command):
    @click.argument("offset", type=int, required=False)
    @click.option("-s", "--search", help="Select by search term")
    @click.option("-t", "--tag", help="Select by tag")
    @functools.wraps(command)
    def wrapper(*args, offset=None, search=None, tag=None, **kwargs):
        return command(
            *args,
            search_options={"offset": offset, "search": search, "tag": tag},
            **kwargs,
        )

    return wrapper


def select_conversation(command):
    @click.argument("offset", type=int, required=False)
    @click.option("-s", "--search", help="Select by search term")
    @click.option("-t", "--tag", help="Select by tag")
    @functools.wraps(command)
    def wrapper(*args, offset=None, search=None, tag=None, **kwargs):
        return command(
            *args,
            conversation=get_logged_conversation(offset=offset, search=search, tag=tag),
            **kwargs,
        )

    return wrapper


def filter_conversations(command):
    @click.argument("offsets", type=int, nargs=-1)
    @click.option("-s", "--search", help="Select by search term")
    @click.option("-t", "--tag", help="Select by tag")
    @functools.wraps(command)
    def wrapper(*args, offsets=None, search=None, tag=None, **kwargs):
        return command(
            *args,
            conversations=search_conversations(offsets=offsets, search=search, tag=tag),
            **kwargs,
        )

    return wrapper


@cli.command(help="Ask a question of ChatGPT.")
@click.option("-q", "--quick", is_flag=True, help="Just handle a one single-line question.")
@click.option(
    "-c",
    "--continue_conversation",
    "--continue",
    is_flag=True,
    help="Continue previous conversation.",
)
@click.option("-p", "--personality", default="concise")
@click.option(
    "-f",
    "--file",
    type=click.Path(exists=True),
    multiple=True,
    help="Add a file to the conversation for context.",
)
@click.option("-r", "--retry", is_flag=True, help="Retry previous question")
@click.option("--stream/--sync", default=True, help="Stream or sync mode.")
@click.option("--model", type=click.Choice(MODELS))
@click.option("--plugin", "additional_plugins", multiple=True, help="Load a plugin.")
@cli_search_options
def chat(search_options, **kwargs):
    if (kwargs["continue_conversation"] or kwargs["retry"]) and not search_options["offset"]:
        search_options["offset"] = 1
    elif (
        kwargs["personality"]
        and not search_options["tag"]
        and not search_options["search"]
        and not search_options["offset"]
    ):
        search_options["tag"] = "^" + kwargs["personality"]

    conversation = get_logged_conversation(**search_options)

    for filename in kwargs["file"]:
        with Path(filename).open(encoding="utf-8") as fh:
            file_contents = fh.read()

        conversation.append("user", f"The file {filename!r} contains:\n```\n{file_contents}```")

    tags = conversation.tags
    tags_to_apply = [tags[-1]] if tags and not is_personality(tags[-1]) else []

    conversation.plugins.extend(kwargs["additional_plugins"])
    conversation.tags = tags_to_apply
    conversation.model = kwargs["model"] or conversation.model or "gpt-3.5-turbo"

    quick = kwargs["quick"] or not os.isatty(0)
    multiline = not quick

    if kwargs["retry"]:
        conversation.messages.pop()
        add_answer(conversation, stream=kwargs["stream"])
        if kwargs["quick"]:
            return

    run_conversation(conversation, multiline=multiline, quick=quick, stream=kwargs["stream"])


@cli.command(help="Create initial conversation log.")
@click.option("-r", "--reinit", is_flag=True, help="re-initialize the personalities to default values")
def init(reinit):
    try:
        create_initial_log(reinit)
    except FileExistsError as error:
        click.echo(f"{error}: Conversation log already exists.", file=sys.stderr)
        sys.exit(1)


@cli.command(help="Add a message to a new or existing conversation.")
@click.option("--multiline/--singleline", default=True)
@click.option("-p", "--personality")
@click.option("--role", type=click.Choice(["system", "user", "assistant"]), default="system")
@click.option("--plugin", multiple="True", help="Activate plugins.")
@click.option("--model", type=click.Choice(MODELS), default="gpt-3.5-turbo")
@click.option("--plugin", "additional_plugins", multiple=True, help="Load a plugin.")
@cli_search_options
def add(personality, role, multiline, search_options, **kwargs):
    conversation = get_logged_conversation(**search_options) if any(search_options.values()) else Conversation({})

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
    write_log(conversation)


def merge_list(input_list, additions):
    for item in additions:
        if item not in input_list:
            input_list.append(item)


@cli.command(help="Create a new conversation by merging existing conversations.")
@click.option("-p", "--personality", help="Set personality for new conversation.")
@filter_conversations
def merge(conversations, personality):
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
        merge_list(merged_conversation["tags"], (tag for tag in item.tags if not is_personality(tag)))
        merge_list(merged_conversation["plugins"], item.plugins)
        merged_conversation["model"] = item.model or merged_conversation["model"]

    write_log(Conversation(merged_conversation))


@cli.command(help="List tags.", name="tags")
def list_tags():
    tags = set()
    for conversation in conversation_log():
        for tag in conversation.tags:
            tags.add(tag)
    for tag in sorted(tags):
        click.echo(tag)


@cli.command(help="Add tags to an conversation.", name="tag")
@click.argument("new_tag")
@select_conversation
def add_tag(new_tag, conversation):
    new_tags = [tag for tag in conversation.tags if tag != new_tag]
    new_tags.append(new_tag)
    conversation.tags = new_tags

    write_log(conversation)


@cli.command(help="Remove tags from an conversation.")
@click.argument("tag_to_remove")
@select_conversation
def untag(tag_to_remove, conversation):
    conversation.tags = [tag for tag in conversation.tags if tag != tag_to_remove]
    write_log(conversation)


@cli.command(help="Current tag")
@select_conversation
def show_tag(conversation):
    if conversation.tags:
        click.echo(conversation.tags[-1])


@cli.command(help="Show a conversation.")
@select_conversation
@click.option(
    "-l/-s",
    "--long/--short",
    help="Show full conversation or just the most recent message.",
)
@click.option("--format-json", "--json", is_flag=True, help="Output conversation in JSON format.")
def show(long, conversation, format_json):
    if format_json:
        click.echo(conversation.to_json())
        return

    messages = conversation.messages if long else conversation.messages[-1:]

    for message in messages:
        prefix = ""
        if message["role"] == "user":
            prefix = ">> "
        click.echo(click.style(prefix + message["content"], fg=MESSAGE_COLORS[message["role"]]))


@cli.command(help="List all the questions we've asked")
@filter_conversations
@click.option("-l", "--limit", type=int, help="Limit number of results")
@click.option("-u", "--usage", is_flag=True, help="Show token usage")
@click.option("--cost", is_flag=True, help="Show token cost")
@click.option("--plugins", is_flag=True, help="Show enabled plugins")
@click.option("--model", is_flag=True, help="Show model")
@click.option("--format-json", "--json", is_flag=True, help="Output conversation in JSON format.")
def log(conversations, limit, usage, cost, plugins, model, format_json):
    for offset, conversation in reversed(list(itertools.islice(conversations, limit))):
        if format_json:
            click.echo(conversation.to_json())
            continue
        try:
            question = conversation.find(lambda message: message["role"] != "assistant")["content"]
        except ValueError:
            question = conversation.messages[-1]["content"]
        trimmed_message = question.strip().split("\n", 1)[0][:80]

        fields = []
        fields.append(click.style(f"{offset: 4d}:", fg="blue"))

        if usage:
            total_tokens = conversation.usage["total_tokens"] if conversation.usage else 0
            fields.append(f"{total_tokens: 5d}")

        if cost:
            fields.append(f"${conversation_cost(conversation): 2.3f}")

        fields.append(trimmed_message)
        if conversation.tags:
            fields.append(click.style(",".join(conversation.tags), fg="green"))

        if plugins:
            fields.append(",".join(conversation.plugins))

        if model:
            fields.append(click.style(conversation.model, fg="yellow"))

        click.echo(" ".join(fields))


def run_conversation(conversation, *, stream=True, multiline=True, quick=False):
    if multiline and os.isatty(0):
        click.echo("(Finish input with <Alt-Enter> or <Esc><Enter>)")

    while True:
        question = prompt(multiline=multiline)
        if not question:
            break
        conversation.append("user", question)
        add_answer(conversation, stream=stream)

        if quick:
            break


def prompt(*, multiline=True):
    if os.isatty(0):
        try:
            return prompt_toolkit.prompt(">> ", multiline=multiline, prompt_continuation=".. ").strip()
        except EOFError:
            return None
    else:
        return sys.stdin.read().strip()


@cli.command(help="Add an answer to a question")
@click.option("--stream/--sync", default=True, help="Stream or sync mode.")
@select_conversation
def answer(conversation, stream):
    add_answer(conversation, stream=stream)


def add_answer(conversation, *, stream=True):
    while True:
        response = conversation.complete(stream=stream, callback=click.echo)
        write_log(conversation, completion=conversation.completion, usage=conversation.usage)
        plugin_response = evaluate_plugins(response["content"], conversation.plugins)
        if not plugin_response:
            break
        click.echo(click.style(plugin_response, fg=(200, 180, 90)))
        conversation.append("user", plugin_response)


def conversation_cost(conversation):
    if not conversation.usage:
        return 0
    model = conversation.completion["model"]
    if model not in USAGE_COSTS:
        model = "-".join(model.split("-")[:-1])
    model_price = USAGE_COSTS[model]

    usage = conversation.usage
    return (
        model_price["prompt_tokens"] * usage["prompt_tokens"] / 1000
        + model_price["completion_tokens"] * usage["completion_tokens"] / 1000
    )


@cli.command(help="Display number of tokens and token cost.", name="usage")
@click.option("--today", is_flag=True, help="Show usage for today only.")
def show_usage(today):
    conversations = conversation_log()

    def is_today(conversation):
        return dateutil.parser.parse(conversation.timestamp).date() == datetime.now(tz=timezone.utc).date()

    if today:
        conversations = [c for c in conversations if is_today(c)]
    tokens = sum(conversation.usage["total_tokens"] for conversation in conversations if conversation.usage)

    total_cost = sum(conversation_cost(conversation) for conversation in conversations)
    click.echo(f"Tokens: {tokens}")
    click.echo(f"Cost: ${total_cost:.2f}")


def get_logged_conversation(offset, search=None, tag=None):
    offsets = [offset] if offset else []
    try:
        return next(search_conversations(offsets, search, tag))[1]
    except StopIteration:
        click.echo("Matching conversation not found", file=sys.stderr)
        sys.exit(1)


def is_personality(tag):
    return tag.startswith("^")


def main():
    try:
        cli()
    except FileNotFoundError as error:
        click.echo(f"{error}: Chatcli not initialized. Run `chatcli init` first.")
        sys.exit(1)


if __name__ == "__main__":
    main()
