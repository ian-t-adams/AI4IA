import { PYTHON_WORD_RE } from "./chatPilotPythonWord";

const COMMAND_RE = /^\/[A-Za-z][A-Za-z0-9_.-]*(?:[ \t]+[\s\S]*)?$/;
const MENTION_PREFIX_RE = /^@([A-Za-z][A-Za-z0-9_.-]*)/;
// Python str.lstrip()/re \s include these controls, but exclude JavaScript's BOM.
const PYTHON_LEADING_WHITESPACE_RE = /^[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+/;

export interface PilotInputClassification {
  agentMention: boolean;
  slashCommand: boolean;
  unsupported: boolean;
}

function isPythonWordCharacter(character: string | undefined): boolean {
  return character !== undefined && PYTHON_WORD_RE.test(character);
}

export function classifyPilotInput(content: string): PilotInputClassification {
  let rest = content.replace(PYTHON_LEADING_WHITESPACE_RE, "");
  let agentMention = false;
  const mention = MENTION_PREFIX_RE.exec(rest);

  if (mention) {
    const name = mention[1];
    for (let length = name.length; length > 0; length -= 1) {
      const nextCodePoint = rest.codePointAt(length + 1);
      const next = nextCodePoint === undefined
        ? undefined
        : String.fromCodePoint(nextCodePoint);
      if (
        isPythonWordCharacter(name[length - 1]) ===
        isPythonWordCharacter(next)
      ) {
        continue;
      }

      agentMention = true;
      rest = rest.slice(length + 1).replace(PYTHON_LEADING_WHITESPACE_RE, "");
      break;
    }
  }

  const slashCommand = COMMAND_RE.test(rest);
  return {
    agentMention,
    slashCommand,
    unsupported: agentMention || slashCommand,
  };
}
