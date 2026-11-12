import { describe, expect, it } from "vitest";

import { classifyPilotInput } from "./chatPilotInput";

describe("classifyPilotInput", () => {
  it.each([
    "/tmp/log.txt",
    "/",
    "@",
    "@42",
    "@café",
    "@agenté hello",
    "@agent\u{31350} hello",
    "@agent\u{20000} hello",
    "@agent\u0661 hello",
    "\uFEFF/help",
  ])("leaves ordinary text unclassified: %s", (content) => {
    expect(classifyPilotInput(content)).toEqual({
      agentMention: false,
      slashCommand: false,
      unsupported: false,
    });
  });

  it.each([
    ["/help", false, true],
    ["/foo bar", false, true],
    ["@agent/hello", true, true],
    ["@agent-", true, false],
    ["@agent.", true, false],
    ["@a-bé", true, false],
    ["@agent\uFEFF/help", true, false],
    ["@agent\u{2ebf0} hello", true, false],
    ["@agent\u1c89 hello", true, false],
    ["@agent\u0301 hello", true, false],
  ] as const)(
    "matches API parser classifications for %s",
    (content, agentMention, slashCommand) => {
      expect(classifyPilotInput(content)).toEqual({
        agentMention,
        slashCommand,
        unsupported: true,
      });
    },
  );

  it.each(["\u0085", "\u001c", "\u001d", "\u001e", "\u001f"])(
    "uses Python whitespace before directives and after mentions: %j",
    (whitespace) => {
      expect(classifyPilotInput(`${whitespace}/help`)).toEqual({
        agentMention: false,
        slashCommand: true,
        unsupported: true,
      });
      expect(classifyPilotInput(`${whitespace}@agent hello`)).toEqual({
        agentMention: true,
        slashCommand: false,
        unsupported: true,
      });
      expect(classifyPilotInput(`@agent${whitespace}/help`)).toEqual({
        agentMention: true,
        slashCommand: true,
        unsupported: true,
      });
      expect(classifyPilotInput(`${whitespace}/tmp/log.txt`)).toEqual({
        agentMention: false,
        slashCommand: false,
        unsupported: false,
      });
    },
  );
});
