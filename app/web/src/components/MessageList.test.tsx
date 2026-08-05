// @vitest-environment jsdom
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { MessageList, type DisplayMessage } from "./MessageList";

// Speech playback owns <audio> + object-URL plumbing and hits the TTS endpoint on
// toggle. Stub the hook so we can assert the speak button wiring without audio or
// network, and observe the toggle call.
const { mockToggle } = vi.hoisted(() => ({ mockToggle: vi.fn() }));
vi.mock("@/lib/voice", () => ({
  useSpeechPlayback: () => ({ activeId: null, busyId: null, toggle: mockToggle }),
}));

// jsdom has no layout engine, so scrollIntoView (called in an effect after every
// render) is undefined; provide a no-op so rendering doesn't throw.
beforeAll(() => {
  window.HTMLElement.prototype.scrollIntoView = vi.fn();
});

afterEach(cleanup);
beforeEach(() => mockToggle.mockReset());

function msg(over: Partial<DisplayMessage> & Pick<DisplayMessage, "id" | "role">): DisplayMessage {
  return { content: "", ...over };
}

describe("MessageList", () => {
  it("renders the empty-state prompt when there are no messages", () => {
    render(<MessageList messages={[]} />);
    expect(screen.getByText("Start a conversation")).toBeInTheDocument();
    expect(
      screen.getByRole("log", { name: "Conversation" }),
    ).toBeInTheDocument();
  });

  it("renders user and assistant bubbles but hides system messages", () => {
    render(
      <MessageList
        messages={[
          msg({ id: "u1", role: "user", content: "Hello there" }),
          msg({ id: "a1", role: "assistant", content: "General reply" }),
          msg({ id: "s1", role: "system", content: "SYSTEM_SECRET" }),
        ]}
      />,
    );
    expect(screen.getByText("Hello there")).toBeInTheDocument();
    expect(screen.getByText("General reply")).toBeInTheDocument();
    expect(screen.getByText("You")).toBeInTheDocument();
    expect(screen.getByText("Assistant")).toBeInTheDocument();
    expect(screen.queryByText("SYSTEM_SECRET")).toBeNull();
  });

  it("shows a generating indicator and no speak button while pending", () => {
    render(
      <MessageList
        messages={[
          msg({ id: "a2", role: "assistant", content: "streaming…", pending: true }),
        ]}
      />,
    );
    expect(screen.getByLabelText("Generating")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Read message aloud/i }),
    ).toBeNull();
  });

  it("shows a thinking indicator before any step or token arrives", () => {
    render(
      <MessageList
        messages={[msg({ id: "a4", role: "assistant", content: "", pending: true })]}
      />,
    );
    expect(screen.getByText("Thinking…")).toBeInTheDocument();
  });

  it("shows live agent activity with finished and running steps while pending", () => {
    render(
      <MessageList
        messages={[
          msg({
            id: "a3",
            role: "assistant",
            content: "",
            pending: true,
            steps: [
              { kind: "tool_result", label: "Searched the web", tool: "web_search" },
              { kind: "tool_start", label: "Reading a web page", tool: "browse_url" },
            ],
          }),
        ]}
      />,
    );
    expect(screen.getByLabelText("Agent activity")).toBeInTheDocument();
    expect(screen.getByText("Searched the web")).toBeInTheDocument();
    expect(screen.getByText("Reading a web page")).toBeInTheDocument();
  });

  it("renders a collapsible activity trace under a finished tool answer", () => {
    render(
      <MessageList
        messages={[
          msg({
            id: "a5",
            role: "assistant",
            content: "Here is what I found.",
            steps: [
              {
                kind: "tool_result",
                label: "Searched the web",
                tool: "web_search",
                detail: "build 2025",
              },
            ],
          }),
        ]}
      />,
    );
    expect(screen.getByText(/Activity . 1 step/)).toBeInTheDocument();
    expect(screen.getByText("Searched the web")).toBeInTheDocument();
    expect(screen.getByText("build 2025")).toBeInTheDocument();
  });

  it("wires the speak button to the playback toggle for finished replies", async () => {
    const user = userEvent.setup();
    render(
      <MessageList
        messages={[msg({ id: "a3", role: "assistant", content: "Read me aloud" })]}
      />,
    );
    await user.click(
      screen.getByRole("button", { name: "Read message aloud" }),
    );
    expect(mockToggle).toHaveBeenCalledWith("a3", "Read me aloud");
  });

  it("renders citation tokens as clickable chips when onCitation is supplied", async () => {
    const user = userEvent.setup();
    const onCitation = vi.fn();
    render(
      <MessageList
        messages={[
          msg({
            id: "a4",
            role: "assistant",
            content: "Listen [[cite:lecture.mp3@12:34]] here",
          }),
        ]}
        onCitation={onCitation}
      />,
    );
    expect(screen.queryByText(/\[\[cite:/)).toBeNull();
    const chip = screen.getByRole("button", { name: /Play lecture\.mp3/ });
    await user.click(chip);
    expect(onCitation).toHaveBeenCalledWith("lecture.mp3", 12 * 60_000 + 34_000);
  });

  it("renders citation tokens as static labels when no onCitation is supplied", () => {
    render(
      <MessageList
        messages={[
          msg({
            id: "a5",
            role: "assistant",
            content: "Listen [[cite:lecture.mp3@12:34]] here",
          }),
        ]}
      />,
    );
    expect(screen.queryByText(/\[\[cite:/)).toBeNull();
    expect(screen.getByText(/lecture\.mp3/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Play/ })).toBeNull();
  });

  it("renders assistant markdown: emphasis, headings and lists", () => {
    const { container } = render(
      <MessageList
        messages={[
          msg({
            id: "md1",
            role: "assistant",
            content: "## Title\n\nSome **bold** text\n\n- one\n- two",
          }),
        ]}
      />,
    );
    // Heading, strong and list items become real elements, not raw markdown text.
    expect(screen.getByRole("heading", { name: "Title" })).toBeInTheDocument();
    expect(container.querySelector("strong")?.textContent).toBe("bold");
    expect(screen.getAllByRole("listitem").map((li) => li.textContent)).toEqual(["one", "two"]);
    // The raw markers must not survive into the rendered text.
    expect(screen.queryByText(/\*\*bold\*\*/)).toBeNull();
    expect(screen.queryByText(/## Title/)).toBeNull();
  });

  it("keeps user messages as plain text (no markdown parsing)", () => {
    render(
      <MessageList messages={[msg({ id: "u2", role: "user", content: "literal **stars**" })]} />,
    );
    // A user typing ** should see it verbatim, never bolded.
    expect(screen.getByText("literal **stars**")).toBeInTheDocument();
  });

  it("renders citation chips inside markdown content", async () => {
    const user = userEvent.setup();
    const onCitation = vi.fn();
    const { container } = render(
      <MessageList
        messages={[
          msg({
            id: "md2",
            role: "assistant",
            content: "See **this** then [[cite:lecture.mp3@12:34]] now",
          }),
        ]}
        onCitation={onCitation}
      />,
    );
    // Markdown formatting and the citation chip coexist in one paragraph.
    expect(container.querySelector("strong")?.textContent).toBe("this");
    expect(screen.queryByText(/\[\[cite:/)).toBeNull();
    await user.click(screen.getByRole("button", { name: /Play lecture\.mp3/ }));
    expect(onCitation).toHaveBeenCalledWith("lecture.mp3", 12 * 60_000 + 34_000);
  });

  it("does not render raw HTML embedded in assistant markdown", () => {
    const { container } = render(
      <MessageList
        messages={[
          msg({ id: "md3", role: "assistant", content: "before <b>x</b> after" }),
        ]}
      />,
    );
    // Raw HTML is disabled, so no injected element is created from model output.
    expect(container.querySelector("b")).toBeNull();
  });
});

// Every model runs under an annotate-only Responsible AI policy: the filters are
// enabled but never block. That makes this panel the only place the safety
// system is observable, so these tests pin that it reports honestly — it must
// never imply the answer was withheld or altered, because it never was.
describe("MessageList content-safety panel", () => {
  const safe = { category: "hate", scope: "prompt" as const, severity: "safe", filtered: false };
  const flagged = {
    category: "violence",
    scope: "completion" as const,
    severity: "medium",
    filtered: false,
  };
  const jailbreak = {
    category: "jailbreak",
    scope: "prompt" as const,
    detected: true,
    filtered: false,
  };

  it("summarises how many verdicts were flagged", async () => {
    render(
      <MessageList
        messages={[
          msg({
            id: "s1",
            role: "assistant",
            content: "answer",
            safety: { signals: [safe, flagged, jailbreak] },
          }),
        ]}
      />,
    );
    expect(screen.getByText("Content safety · 2 flagged")).toBeInTheDocument();

    // Detail is collapsed until asked for, so a routine turn stays quiet.
    await userEvent.click(screen.getByText("Content safety · 2 flagged"));
    expect(screen.getByText("Violence")).toBeInTheDocument();
    expect(screen.getByText("Jailbreak attempt")).toBeInTheDocument();
    expect(screen.getByText("medium")).toBeInTheDocument();
    expect(screen.getByText("detected")).toBeInTheDocument();
  });

  it("says so explicitly when nothing was flagged", () => {
    render(
      <MessageList
        messages={[
          msg({ id: "s2", role: "assistant", content: "answer", safety: { signals: [safe] } }),
        ]}
      />,
    );
    // "The filters ran and found nothing" is a different statement from "no
    // filters ran", and the panel has to make the first one visible.
    expect(screen.getByText("Content safety · nothing flagged")).toBeInTheDocument();
  });

  it("states that nothing was blocked or rewritten", async () => {
    render(
      <MessageList
        messages={[
          msg({ id: "s3", role: "assistant", content: "answer", safety: { signals: [flagged] } }),
        ]}
      />,
    );
    await userEvent.click(screen.getByText("Content safety · 1 flagged"));
    expect(
      screen.getByText(/Nothing was blocked or rewritten/i),
    ).toBeInTheDocument();
  });

  it("distinguishes the prompt from the reply", async () => {
    render(
      <MessageList
        messages={[
          msg({
            id: "s4",
            role: "assistant",
            content: "answer",
            safety: { signals: [safe, flagged] },
          }),
        ]}
      />,
    );
    await userEvent.click(screen.getByText("Content safety · 1 flagged"));
    expect(screen.getByText("your message")).toBeInTheDocument();
    expect(screen.getByText("the reply")).toBeInTheDocument();
  });

  it("renders nothing when the provider reported no annotations", () => {
    render(
      <MessageList
        messages={[
          msg({ id: "s5", role: "assistant", content: "answer" }),
          msg({ id: "s6", role: "assistant", content: "answer", safety: { signals: [] } }),
        ]}
      />,
    );
    expect(screen.queryByText(/Content safety/)).toBeNull();
  });

  it("stays hidden while the turn is still streaming", () => {
    // A partial verdict must never be presented as the final one.
    render(
      <MessageList
        messages={[
          msg({
            id: "s7",
            role: "assistant",
            content: "partial",
            pending: true,
            safety: { signals: [flagged] },
          }),
        ]}
      />,
    );
    expect(screen.queryByText(/Content safety/)).toBeNull();
  });

  it("falls back to the raw category name for an unknown filter", async () => {
    render(
      <MessageList
        messages={[
          msg({
            id: "s8",
            role: "assistant",
            content: "answer",
            safety: {
              signals: [
                { category: "new_filter", scope: "completion", severity: "high", filtered: false },
              ],
            },
          }),
        ]}
      />,
    );
    // A newly added Foundry filter must still surface rather than vanish.
    await userEvent.click(screen.getByText("Content safety · 1 flagged"));
    expect(screen.getByText("new filter")).toBeInTheDocument();
  });
});
