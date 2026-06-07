from ai4ia_api.agents.commands import CommandKind, parse_input


def test_plain_text_has_no_directives():
    parsed = parse_input("hello there")
    assert parsed.agent is None
    assert parsed.command is None
    assert parsed.text == "hello there"
    assert parsed.is_command is False


def test_agent_mention_is_extracted_and_lowercased():
    parsed = parse_input("@Researcher find the latest on pgvector")
    assert parsed.agent == "researcher"
    assert parsed.command is None
    assert parsed.text == "find the latest on pgvector"


def test_known_slash_command_resolves_kind_and_args():
    parsed = parse_input("/system You are a terse assistant.")
    assert parsed.command is not None
    assert parsed.command.kind is CommandKind.system
    assert parsed.command.name == "system"
    assert parsed.command.args == "You are a terse assistant."
    assert parsed.text == "You are a terse assistant."


def test_unknown_slash_command_is_marked_unknown():
    parsed = parse_input("/wibble do a thing")
    assert parsed.command is not None
    assert parsed.command.kind is CommandKind.unknown
    assert parsed.command.name == "wibble"
    assert parsed.command.args == "do a thing"


def test_command_with_no_args():
    parsed = parse_input("/clear")
    assert parsed.command is not None
    assert parsed.command.kind is CommandKind.clear
    assert parsed.command.args == ""
    assert parsed.text == ""


def test_mention_and_command_combine():
    parsed = parse_input("@planner /model gpt-5.2")
    assert parsed.agent == "planner"
    assert parsed.command is not None
    assert parsed.command.kind is CommandKind.model
    assert parsed.command.args == "gpt-5.2"


def test_mid_string_slash_is_not_a_command():
    parsed = parse_input("what is 1/2 of 10?")
    assert parsed.command is None
    assert parsed.agent is None
    assert parsed.text == "what is 1/2 of 10?"


def test_mid_string_at_is_not_a_mention():
    parsed = parse_input("email me @ bob please")
    assert parsed.agent is None
    assert parsed.text == "email me @ bob please"


def test_leading_whitespace_is_tolerated():
    parsed = parse_input("   /help")
    assert parsed.command is not None
    assert parsed.command.kind is CommandKind.help


def test_raw_is_preserved():
    parsed = parse_input("  @bot /clear  ")
    assert parsed.raw == "  @bot /clear  "
    assert parsed.agent == "bot"
    assert parsed.command is not None and parsed.command.kind is CommandKind.clear
