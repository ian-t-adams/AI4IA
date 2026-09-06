// @vitest-environment jsdom
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { MessageList, type DisplayMessage } from "./MessageList";
import type { ExecutionReceipt, RetrievedSource } from "@/lib/types";

// Speech playback owns <audio> + object-URL plumbing and hits the TTS endpoint on
// toggle. Stub the hook so we can assert the speak button wiring without audio or
// network, and observe the toggle call.
const { mockToggle, scrollIntoViewMock } = vi.hoisted(() => ({
  mockToggle: vi.fn(),
  scrollIntoViewMock: vi.fn(),
}));
vi.mock("@/lib/api", () => ({
  fetchImageArtifact: vi.fn(() => new Promise<Blob>(() => {})),
  fetchVideoArtifact: vi.fn(() => new Promise<Blob>(() => {})),
  fetchDocumentArtifact: vi.fn(() => new Promise<Blob>(() => {})),
}));
vi.mock("@/lib/voice", () => ({
  useSpeechPlayback: () => ({ activeId: null, busyId: null, toggle: mockToggle }),
}));

// jsdom has no layout engine, so scrollIntoView (called in an effect after every
// render) is undefined; provide a no-op so rendering doesn't throw.
beforeAll(() => {
  window.HTMLElement.prototype.scrollIntoView = scrollIntoViewMock;
});

afterEach(cleanup);
beforeEach(() => {
  mockToggle.mockReset();
  scrollIntoViewMock.mockReset();
});

function msg(over: Partial<DisplayMessage> & Pick<DisplayMessage, "id" | "role">): DisplayMessage {
  return { content: "", ...over };
}

function setScrollMetrics(
  element: HTMLElement,
  { scrollHeight, clientHeight, scrollTop }: Record<"scrollHeight" | "clientHeight" | "scrollTop", number>,
) {
  Object.defineProperties(element, {
    scrollHeight: { configurable: true, value: scrollHeight },
    clientHeight: { configurable: true, value: clientHeight },
    scrollTop: { configurable: true, writable: true, value: scrollTop },
  });
}
describe("MessageList", () => {
  it("renders the empty-state prompt when there are no messages", () => {
    render(<MessageList messages={[]} />);
    expect(screen.getByText("Start a conversation")).toBeInTheDocument();
    expect(
      screen.getByRole("log", { name: "Conversation" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("main")).toBeNull();
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

  it("renders image comparisons in order with per-result provenance and cost", () => {
    render(
      <MessageList
        messages={[
          msg({
            id: "images",
            role: "assistant",
            attachments: [
              {
                id: "one",
                kind: "image",
                mimeType: "image/png",
                prompt: "A lighthouse",
                model: "FLUX-1.1-pro",
                provider: "black_forest_labs",
                deployment: "flux-eastus2",
                region: "eastus2",
                dataZone: "us",
                residency: "global",
                size: "1024x1024",
                quality: "auto",
                costKnown: true,
                estimatedCostUsd: 0.04,
                pricingBasis: "image",
                priceVersion: "test",
              },
              {
                id: "two",
                kind: "image_error",
                mimeType: "application/problem+json",
                prompt: "A lighthouse",
                model: "MAI-Image-2.5",
                provider: "microsoft",
                deployment: "mai-eastus2",
                region: "eastus2",
                dataZone: "us",
                residency: "global",
                size: "1024x1024",
                quality: "auto",
                costKnown: false,
                estimatedCostUsd: null,
                pricingBasis: null,
                priceVersion: "test",
                status: "error",
                error: "Provider capacity was unavailable.",
              },
            ],
          }),
        ]}
      />,
    );

    const comparison = screen.getByRole("list", {
      name: "Image model comparison",
    });
    const captions = comparison.querySelectorAll("figcaption");
    expect(captions).toHaveLength(1);
    expect(captions[0]).toHaveTextContent(
      /FLUX-1\.1-pro.*black_forest_labs.*eastus2.*estimated \$0\.040/,
    );
    expect(comparison).toHaveTextContent(
      /MAI-Image-2\.5.*Provider capacity was unavailable.*microsoft.*cost estimate unavailable/,
    );
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
    // A legacy token carries no span, so the app can only offer the filename --
    // which is exactly the ambiguity the span id removes for new turns.
    expect(onCitation).toHaveBeenCalledWith({
      documentId: null,
      filename: "lecture.mp3",
      ms: 12 * 60_000 + 34_000,
    });
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
    // A legacy token carries no span, so the app can only offer the filename --
    // which is exactly the ambiguity the span id removes for new turns.
    expect(onCitation).toHaveBeenCalledWith({
      documentId: null,
      filename: "lecture.mp3",
      ms: 12 * 60_000 + 34_000,
    });
  });

  it("does not let emphasis or headings hide an invented citation", () => {
    render(
      <MessageList
        messages={[
          msg({
            id: "md-nested-cite",
            role: "assistant",
            content: "## **[[cite:S9]]**",
            sources: [],
          }),
        ]}
      />,
    );
    expect(screen.getByText("Unverified citation")).toBeInTheDocument();
    expect(screen.getByRole("heading")).toContainElement(
      screen.getByText("Unverified citation").parentElement,
    );
  });

  it("does not auto-load remote images from assistant markdown", () => {
    const { container } = render(
      <MessageList
        messages={[
          msg({
            id: "md-image",
            role: "assistant",
            content: "![tracking pixel](https://attacker.example/pixel)",
          }),
        ]}
      />,
    );
    expect(container.querySelector("img")).toBeNull();
    expect(screen.getByText("[External image omitted: tracking pixel]")).toBeInTheDocument();
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

// Audit P1-14. The reader's whole protection here is that a citation the model
// invented does not look like one backed by a span that was actually retrieved,
// so every catching test has a passing control beside it: same markup, same
// answer shape, only the id differs.
describe("MessageList citation provenance", () => {
  const span = (over: Partial<RetrievedSource> = {}): RetrievedSource => ({
    spanId: "S1",
    documentId: "doc-1",
    filename: "lecture.mp3",
    startMs: 12 * 60_000 + 34_000,
    excerpt: "The mitochondria is the powerhouse of the cell.",
    contentSha256: "a".repeat(64),
    retrievedAt: "2026-08-07T00:00:00Z",
    ...over,
  });

  it("renders a cited span that was retrieved as an actionable chip", async () => {
    const user = userEvent.setup();
    const onCitation = vi.fn();
    render(
      <MessageList
        messages={[
          msg({
            id: "p1",
            role: "assistant",
            content: "It is the powerhouse [[cite:S1]].",
            sources: [span()],
          }),
        ]}
        onCitation={onCitation}
      />,
    );
    expect(screen.queryByText(/\[\[cite:/)).toBeNull();
    expect(screen.queryByText("Unverified citation")).toBeNull();
    await user.click(screen.getByRole("button", { name: /Play lecture\.mp3/ }));
    // Resolution is by document id, not by name.
    expect(onCitation).toHaveBeenCalledWith({
      documentId: "doc-1",
      filename: "lecture.mp3",
      ms: 12 * 60_000 + 34_000,
    });
  });

  it("marks a cited span that was never retrieved, and refuses to make it actionable", () => {
    const onCitation = vi.fn();
    render(
      <MessageList
        messages={[
          msg({
            id: "p2",
            role: "assistant",
            // Byte-identical to the test above except for the id.
            content: "It is the powerhouse [[cite:S9]].",
            sources: [span()],
            citations: [
              { spanId: "S9", status: "unverified", occurrences: 1, raw: "[[cite:S9]]" },
            ],
          }),
        ]}
        onCitation={onCitation}
      />,
    );
    expect(screen.getByText("Unverified citation")).toBeInTheDocument();
    // Nothing was retrieved under that id, so there is nothing to open. A
    // playable-looking chip would be the same lie in a different costume.
    expect(screen.queryByRole("button", { name: /Play/ })).toBeNull();
  });

  it("does not lose an invented id hidden inside a grouped citation", () => {
    render(
      <MessageList
        messages={[
          msg({
            id: "p3",
            role: "assistant",
            content: "Both agree [[cite:S1,S9]].",
            sources: [span()],
          }),
        ]}
      />,
    );
    // The good id must not launder the bad one.
    expect(screen.getByText("Unverified citation")).toBeInTheDocument();
  });

  it("resolves duplicate filenames to the span's own document", async () => {
    const user = userEvent.setup();
    const onCitation = vi.fn();
    render(
      <MessageList
        messages={[
          msg({
            id: "p4",
            role: "assistant",
            content: "Later on [[cite:S2]].",
            sources: [
              span({ spanId: "S1", documentId: "dup-a" }),
              span({ spanId: "S2", documentId: "dup-b", startMs: 900_000 }),
            ],
          }),
        ]}
        onCitation={onCitation}
      />,
    );
    await user.click(screen.getByRole("button", { name: /Play lecture\.mp3/ }));
    // Filename matching would have taken the first of the two; the span id
    // cannot, which is the media half of the P1-14 defect.
    expect(onCitation).toHaveBeenCalledWith({
      documentId: "dup-b",
      filename: "lecture.mp3",
      ms: 900_000,
    });
  });

  it("lists the retrieval receipt, marking which spans the answer used", () => {
    render(
      <MessageList
        messages={[
          msg({
            id: "p5",
            role: "assistant",
            content: "It is the powerhouse [[cite:S1]].",
            sources: [
              span({ spanId: "S1" }),
              span({ spanId: "S2", documentId: "doc-2", filename: "notes.pdf", startMs: null }),
            ],
            citations: [
              {
                spanId: "S1",
                status: "verified",
                documentId: "doc-1",
                filename: "lecture.mp3",
                occurrences: 1,
              },
            ],
          }),
        ]}
      />,
    );
    expect(screen.getByText(/Sources · 2 retrieved, 1 cited/)).toBeInTheDocument();
    // The excerpt travels with the answer so support is the reader's judgement
    // rather than a verdict the app is not entitled to reach.
    expect(
      screen.getAllByText(/The mitochondria is the powerhouse/).length,
    ).toBeGreaterThan(0);
    expect(screen.getByText(/S2 · notes\.pdf · not cited/)).toBeInTheDocument();
  });

  it("shows no receipt and no verdict for a turn that was never attested", () => {
    render(
      <MessageList
        messages={[
          msg({
            id: "p6",
            role: "assistant",
            content: "Listen [[cite:lecture.mp3@12:34]] here",
          }),
        ]}
      />,
    );
    // Every row written before this feature lands here. Absent evidence is not
    // evidence of fabrication, so nothing is marked and nothing is claimed.
    expect(screen.queryByText("Unverified citation")).toBeNull();
    expect(screen.queryByText(/Sources ·/)).toBeNull();
    expect(screen.getByText(/lecture\.mp3/)).toBeInTheDocument();
  });

  // The pair below is the point: this component renders `onCitation` in
  // production whenever the library is enabled (ChatApp passes `handleCitation`),
  // so a test that omits the handler exercises a branch real users never see. The
  // control proves the accent styling is reachable at all, so the unattested
  // assertion cannot pass merely because nothing rendered.
  it("marks an actionable VERIFIED citation as attested", () => {
    render(
      <MessageList
        messages={[
          msg({
            id: "p7",
            role: "assistant",
            content: "Listen [[cite:S1]] here",
            sources: [span({ spanId: "S1" })],
          }),
        ]}
        onCitation={vi.fn()}
      />,
    );
    const chip = screen.getByRole("button", { name: /verified source S1/ });
    expect(chip).toHaveStyle({ color: "var(--accent)" });
  });

  it("does not dress an actionable UNATTESTED citation as a verified one", () => {
    render(
      <MessageList
        messages={[
          msg({
            id: "p8",
            role: "assistant",
            content: "Listen [[cite:lecture.mp3@12:34]] here",
          }),
        ]}
        onCitation={vi.fn()}
      />,
    );
    // Still clickable — a legacy row should keep seeking to its media — but it
    // must not borrow the accent that means "this answer actually retrieved it".
    const chip = screen.getByRole("button", { name: /not verified for this answer/ });
    expect(chip).toHaveStyle({ color: "var(--fg-muted)" });
    expect(chip).not.toHaveStyle({ color: "var(--accent)" });
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

  it("distinguishes app enforcement from provider-native behavior", async () => {
    render(
      <MessageList
        messages={[
          msg({ id: "s3", role: "assistant", content: "answer", safety: { signals: [flagged] } }),
        ]}
      />,
    );
    await userEvent.click(screen.getByText("Content safety · 1 flagged"));
    expect(
      screen.getByText(/AI4IA did not add an application-level block or rewrite/i),
    ).toBeInTheDocument();
  });

  it("calls out a provider-reported filtered result without claiming app blocking", async () => {
    render(
      <MessageList
        messages={[
          msg({
            id: "s-filtered",
            role: "assistant",
            content: "answer",
            safety: { signals: [{ ...flagged, filtered: true }] },
          }),
        ]}
      />,
    );
    await userEvent.click(screen.getByText("Content safety · 1 flagged"));
    expect(
      screen.getByText(/model platform reported filtered content/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/AI4IA did not add a separate application-level block/i),
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

  it("renders nothing for a turn that carries no safety record at all", () => {
    // A row written before assessments were recorded says nothing about
    // whether the filters ran, so the panel makes no claim on its behalf.
    render(
      <MessageList
        messages={[msg({ id: "s5", role: "assistant", content: "answer" })]}
      />,
    );
    expect(screen.queryByText(/Content safety/)).toBeNull();
  });

  it("says plainly when no assessment was returned", async () => {
    // Under an annotate-only policy an omitted panel reads as "nothing was
    // flagged" — a claim nobody made. An explicit record must say otherwise.
    render(
      <MessageList
        messages={[
          msg({
            id: "s6",
            role: "assistant",
            content: "answer",
            safety: { signals: [], status: "unavailable", provider: "azure_openai" },
          }),
        ]}
      />,
    );
    const summary = screen.getByText("Content safety · not assessed");
    expect(summary).toBeInTheDocument();

    await userEvent.click(summary);
    expect(
      screen.getByText(/No platform guardrail assessment was returned/i),
    ).toBeInTheDocument();
    // It must not be mistakable for a verdict.
    expect(screen.getByText(/That is not a verdict/i)).toBeInTheDocument();
  });

  it("shows the normalized severity level beside the provider's own wording", async () => {
    render(
      <MessageList
        messages={[
          msg({
            id: "s9",
            role: "assistant",
            content: "answer",
            safety: {
              status: "reported",
              coverage: ["completion"],
              signals: [{ ...flagged, severityLevel: 2 }],
            },
          }),
        ]}
      />,
    );
    await userEvent.click(screen.getByText("Content safety · 1 flagged"));
    // "medium" alone means nothing without the scale.
    expect(screen.getByText("medium (level 2 of 3)")).toBeInTheDocument();
  });

  it("leaves an unranked severity as the provider wrote it", async () => {
    // Control for the test above: without a server-supplied ordinal the panel
    // must not invent a position on a scale the value may not belong to.
    render(
      <MessageList
        messages={[
          msg({
            id: "s10",
            role: "assistant",
            content: "answer",
            safety: {
              status: "reported",
              signals: [
                {
                  category: "hate",
                  scope: "completion",
                  severity: "catastrophic",
                  filtered: false,
                },
              ],
            },
          }),
        ]}
      />,
    );
    await userEvent.click(screen.getByText("Content safety · 1 flagged"));
    expect(screen.getByText("catastrophic")).toBeInTheDocument();
    expect(screen.queryByText(/level .* of 3/)).toBeNull();
  });

  it("reports which halves of the exchange were assessed", async () => {
    render(
      <MessageList
        messages={[
          msg({
            id: "s11",
            role: "assistant",
            content: "answer",
            safety: {
              status: "reported",
              coverage: ["prompt", "completion"],
              signals: [safe, flagged],
            },
          }),
        ]}
      />,
    );
    await userEvent.click(screen.getByText("Content safety · 1 flagged"));
    expect(
      screen.getByText(/Assessed: your message and the reply\./),
    ).toBeInTheDocument();
  });

  it("labels multi-call assessments and visible truncation", async () => {
    render(
      <MessageList
        messages={[
          msg({
            id: "s12",
            role: "assistant",
            content: "answer",
            safety: {
              status: "reported",
              coverage: ["completion"],
              signals: [{ ...flagged, modelCall: 2 }],
              signalCount: 40,
              truncated: true,
            },
          }),
        ]}
      />,
    );
    await userEvent.click(screen.getByText("Content safety · 1 flagged"));
    expect(screen.getByText("the reply · model call 2")).toBeInTheDocument();
    expect(
      screen.getByText(/Showing 1 of 40 returned assessments/),
    ).toBeInTheDocument();
  });

  it("shows a partial content-filter assessment error", async () => {
    render(
      <MessageList
        messages={[
          msg({
            id: "s-partial",
            role: "assistant",
            content: "answer",
            safety: {
              status: "partial",
              coverage: ["completion"],
              signals: [flagged],
              errors: ["content_filter_timeout"],
            },
          }),
        ]}
      />,
    );
    await userEvent.click(screen.getByText("Content safety · 1 flagged"));
    expect(
      screen.getByText(/Assessment coverage was partial \(content_filter_timeout\)/),
    ).toBeInTheDocument();
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
  it("labels deployment status truthfully and announces the new tab", () => {
    render(<MessageList messages={[]} />);

    expect(screen.queryByRole("link", { name: /Live status/i })).toBeNull();
    expect(
      screen.getByRole("link", {
        name: /Deployment status.*opens in a new tab/i,
      }),
    ).toHaveAttribute("target", "_blank");
  });

  it("continues following streaming content while the reader is near the bottom", () => {
    const first = msg({ id: "a-scroll", role: "assistant", content: "A" });
    const { rerender } = render(<MessageList messages={[first]} />);
    const viewport = screen.getByRole("log", { name: "Conversation" });
    setScrollMetrics(viewport, {
      scrollHeight: 1_000,
      clientHeight: 400,
      scrollTop: 550,
    });
    fireEvent.scroll(viewport);
    scrollIntoViewMock.mockClear();

    rerender(
      <MessageList
        messages={[msg({ ...first, content: "A streaming delta" })]}
      />,
    );

    expect(scrollIntoViewMock).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("button", { name: "Jump to latest" })).toBeNull();
  });

  it("preserves a reader's position and offers Jump to latest for offscreen updates", async () => {
    const user = userEvent.setup();
    const first = msg({ id: "a-scroll", role: "assistant", content: "A" });
    const { rerender } = render(<MessageList messages={[first]} />);
    const viewport = screen.getByRole("log", { name: "Conversation" });
    setScrollMetrics(viewport, {
      scrollHeight: 1_000,
      clientHeight: 400,
      scrollTop: 100,
    });
    fireEvent.scroll(viewport);
    scrollIntoViewMock.mockClear();

    rerender(
      <MessageList
        messages={[msg({ ...first, content: "A streaming delta" })]}
      />,
    );

    expect(scrollIntoViewMock).not.toHaveBeenCalled();
    expect(viewport.scrollTop).toBe(100);
    const jump = screen.getByRole("button", { name: "Jump to latest" });
    jump.focus();
    expect(jump).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(scrollIntoViewMock).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("button", { name: "Jump to latest" })).toBeNull();
  });

  it("resets follow state when switching between long conversations", () => {
    const sessionA = Array.from({ length: 24 }, (_, index) =>
      msg({
        id: `a-${index}`,
        role: "assistant",
        content: `Session A message ${index}`,
      }),
    );
    const sessionB = Array.from({ length: 24 }, (_, index) =>
      msg({
        id: `b-${index}`,
        role: "assistant",
        content: `Session B message ${index}`,
      }),
    );
    const { rerender } = render(
      <MessageList conversationId="session-a" messages={sessionA} />,
    );
    const viewport = screen.getByRole("log", { name: "Conversation" });
    setScrollMetrics(viewport, {
      scrollHeight: 4_000,
      clientHeight: 400,
      scrollTop: 100,
    });
    fireEvent.scroll(viewport);
    scrollIntoViewMock.mockClear();

    rerender(
      <MessageList
        conversationId="session-a"
        messages={[
          ...sessionA,
          msg({ id: "a-new", role: "assistant", content: "New" }),
        ]}
      />,
    );
    expect(
      screen.getByRole("button", { name: "Jump to latest" }),
    ).toBeInTheDocument();
    scrollIntoViewMock.mockClear();

    rerender(<MessageList conversationId="session-b" messages={sessionB} />);

    expect(scrollIntoViewMock).toHaveBeenCalledWith({
      behavior: "auto",
      block: "end",
    });
    expect(screen.queryByRole("button", { name: "Jump to latest" })).toBeNull();
  });
});

// The execution receipt is the answer to "what was supplied to the model, what
// was it allowed to do, and what did it do with it?" — a question the coarse
// Activity trace deliberately cannot answer. These pin that the panel reports
// what the server recorded and claims nothing beyond it.
describe("MessageList execution receipt", () => {
  const payload = (text: string) => ({
    text,
    sha256: "a".repeat(64),
    bytes: text.length,
    truncated: false,
  });

  const receipt = (over: Partial<ExecutionReceipt> = {}): ExecutionReceipt => ({
    version: 1,
    correlationId: "corr-1",
    runtime: {
      modelId: "gpt-4o",
      deployment: "gpt-4o-eastus2",
      region: "eastus2",
      sku: "GlobalStandard",
      dataZone: "us",
      residency: "global",
      api: "chat",
      agent: null,
    },
    prompt: [{ role: "user", content: payload("what is the weather?") }],
    promptMessageCount: 1,
    promptBytes: 20,
    contextBlocks: [],
    droppedHistoryMessages: 0,
    droppedContextBlocks: [],
    toolsOffered: [],
    toolsOfferedCount: 0,
    toolCalls: [],
    toolCallCount: 0,
    approvalsRequested: 0,
    approvalsGranted: 0,
    usage: {
      known: true,
      complete: true,
      calls: 1,
      promptTokens: 12,
      completionTokens: 8,
      totalTokens: 20,
    },
    safety: {
      status: "reported",
      provider: "azure_openai",
      mode: "annotate_only",
      coverage: ["prompt", "completion"],
      signalCount: 8,
      truncated: false,
    },
    iterations: 1,
    status: "complete",
    partial: false,
    truncated: false,
    notes: [],
    ...over,
  });

  function renderReceipt(over: Partial<ExecutionReceipt> = {}) {
    render(
      <MessageList
        messages={[
          msg({
            id: "r1",
            role: "assistant",
            content: "answer",
            executionReceipt: receipt(over),
          }),
        ]}
      />,
    );
  }

  it("stays collapsed until asked for", async () => {
    renderReceipt();
    const summary = screen.getByText(/Execution receipt/);
    const panel = summary.closest("details");
    // jsdom keeps collapsed <details> children in the DOM, so the disclosure
    // state is the `open` attribute, not the presence of the content.
    expect(panel).not.toBeNull();
    expect(panel).not.toHaveAttribute("open");

    await userEvent.click(summary);
    expect(panel).toHaveAttribute("open");
    expect(screen.getByText("Runtime")).toBeInTheDocument();
  });

  it("is reachable from the keyboard through a native disclosure", async () => {
    renderReceipt();
    const summary = screen.getByText(/Execution receipt/);
    // The accessibility guarantee is structural: a real <summary> inside a real
    // <details> is focusable and toggles on Enter/Space in every browser, and
    // is announced as a disclosure by screen readers, with no re-implemented
    // ARIA to drift. (jsdom does not implement the Enter toggle itself, so the
    // structure is what is asserted here.)
    expect(summary.tagName).toBe("SUMMARY");
    expect(summary.parentElement?.tagName).toBe("DETAILS");
    summary.focus();
    expect(summary).toHaveFocus();

    // Every subsection is the same native disclosure rather than a div.
    await userEvent.click(summary);
    for (const label of ["Runtime", "Tools offered · 0", "Tool calls · 0"]) {
      const section = screen.getByText(label);
      expect(section.tagName).toBe("SUMMARY");
      expect(section.parentElement?.tagName).toBe("DETAILS");
    }
  });

  it("reports the resolved deployment, region and residency", async () => {
    renderReceipt();
    await userEvent.click(screen.getByText(/Execution receipt/));
    await userEvent.click(screen.getByText("Runtime"));

    expect(screen.getByText("gpt-4o-eastus2")).toBeInTheDocument();
    expect(screen.getByText("eastus2")).toBeInTheDocument();
    expect(screen.getByText(/20 tokens across 1 model call/)).toBeInTheDocument();
    expect(
      screen.getByText(/reported · azure_openai · prompt \+ completion · 8 assessments/),
    ).toBeInTheDocument();
    // Residency is a different claim from the data zone and is labelled as one.
    expect(screen.getByText("Processing residency")).toBeInTheDocument();
    expect(screen.getByText("global")).toBeInTheDocument();
  });

  it("distinguishes a tool that was offered from one that was invoked", async () => {
    renderReceipt({
      toolsOffered: [
        { name: "web_search", description: "search the web", parametersSha256: "x" },
        { name: "send_mail", description: "send mail", parametersSha256: "y" },
      ],
      toolsOfferedCount: 2,
      toolCalls: [
        {
          tool: "web_search",
          outcome: "result",
          detail: null,
          arguments: payload('{"q":"weather"}'),
          result: payload('{"items":3}'),
        },
      ],
      toolCallCount: 1,
    });
    await userEvent.click(screen.getByText(/Execution receipt/));
    await userEvent.click(screen.getByText("Tools offered · 2"));

    expect(screen.getByText("web_search · invoked")).toBeInTheDocument();
    // The whole point: "could have sent mail and chose not to" is invisible
    // from the call list alone.
    expect(screen.getByText("send_mail · not invoked")).toBeInTheDocument();
  });

  it("shows a tool call's redacted arguments and result", async () => {
    renderReceipt({
      toolCalls: [
        {
          tool: "browse_url",
          outcome: "result",
          detail: null,
          arguments: payload('{"api_key":"***REDACTED***","url":"https://example.test"}'),
          result: payload('{"ok":true}'),
        },
      ],
      toolCallCount: 1,
    });
    await userEvent.click(screen.getByText(/Execution receipt/));
    await userEvent.click(screen.getByText("Tool calls · 1"));

    expect(screen.getByText("browse_url · result")).toBeInTheDocument();
    expect(
      screen.getByText(/"api_key":"\*\*\*REDACTED\*\*\*"/),
    ).toBeInTheDocument();
    expect(screen.getByText(/"ok":true/)).toBeInTheDocument();
  });

  it("marks a truncated payload and keeps its original size", async () => {
    renderReceipt({
      toolCalls: [
        {
          tool: "fetch_document",
          outcome: "result",
          detail: null,
          arguments: null,
          result: { text: "partial…", sha256: "b".repeat(64), bytes: 5_242_880, truncated: true },
        },
      ],
      toolCallCount: 1,
    });
    await userEvent.click(screen.getByText(/Execution receipt/));
    await userEvent.click(screen.getByText("Tool calls · 1"));

    // The fact that a tool returned five megabytes must survive the truncation.
    expect(screen.getByText(/result · 5\.0 MB · truncated/)).toBeInTheDocument();
  });

  it("says when a context block was built but never reached the model", async () => {
    renderReceipt({
      contextBlocks: [
        { kind: "memory", admitted: true, content: payload("you prefer Python") },
        { kind: "library", admitted: false, content: null },
      ],
      droppedContextBlocks: ["library"],
      droppedHistoryMessages: 4,
    });

    await userEvent.click(screen.getByText(/Execution receipt/));
    await userEvent.click(screen.getByText(/Prompt and context/));

    expect(screen.getByText("Context: memory")).toBeInTheDocument();
    expect(screen.getByText("admitted to the prompt")).toBeInTheDocument();
    expect(screen.getByText("you prefer Python")).toBeInTheDocument();
    // A displaced block never influenced the answer, and the panel must not
    // let it look as though it did.
    expect(
      screen.getByText("built but displaced — never reached the model"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/4 earlier messages were dropped to fit the context budget/),
    ).toBeInTheDocument();
  });

  it("shows memory identity, version, score, and content digest", async () => {
    renderReceipt({
      contextBlocks: [
        {
          kind: "memory",
          admitted: true,
          content: payload("you prefer Python"),
          sourceCount: 1,
          sources: [
            {
              id: "memory-1",
              version: "4",
              kind: "user_message",
              score: 0.91234,
              contentSha256: "c".repeat(64),
            },
          ],
        },
      ],
    });
    await userEvent.click(screen.getByText(/Execution receipt/));
    await userEvent.click(screen.getByText(/Prompt and context/));

    expect(screen.getByText("memory-1")).toBeInTheDocument();
    expect(
      screen.getByText(/user_message · version 4 · score 0\.912 · sha256 c{16}/),
    ).toBeInTheDocument();
  });

  it("progressively reveals a linked agent's nested receipt", async () => {
    renderReceipt({
      delegations: [
        receipt({
          runtime: { agent: "helper" },
          prompt: [
            { role: "system", content: payload("You are helper.") },
            { role: "user", content: payload("calculate") },
          ],
          promptMessageCount: 2,
          iterations: 2,
          toolsOffered: [
            {
              name: "calculator",
              description: "Calculate.",
              parametersSha256: "d".repeat(64),
            },
          ],
          toolsOfferedCount: 1,
          toolCalls: [
            {
              tool: "calculator",
              outcome: "result",
              arguments: payload('{"expression":"6*7"}'),
              result: payload('{"result":42}'),
            },
          ],
          toolCallCount: 1,
        }),
      ],
    });
    await userEvent.click(screen.getByText(/Execution receipt/));
    await userEvent.click(screen.getByText("Delegated runs · 1"));
    await userEvent.click(screen.getByText("@helper · 2 model iterations · 1 tool call"));

    expect(screen.getByText("You are helper.")).toBeInTheDocument();
    expect(screen.getByText(/"result":42/)).toBeInTheDocument();
  });

  it("shows later model requests with tool-call protocol ids", async () => {
    renderReceipt({
      modelRequests: [
        {
          iteration: 2,
          promptMessageCount: 2,
          promptBytes: 64,
          prompt: [
            {
              role: "assistant",
              content: payload(""),
              toolCalls: payload('[{"id":"call-1"}]'),
            },
            {
              role: "tool",
              content: payload('{"result":42}'),
              toolCallId: "call-1",
            },
          ],
        },
      ],
    });
    await userEvent.click(screen.getByText(/Execution receipt/));
    await userEvent.click(screen.getByText(/Prompt and context/));
    await userEvent.click(screen.getByText(/Model request 2/));

    expect(screen.getByText(/^Assistant tool calls/)).toBeInTheDocument();
    expect(screen.getByText("call-1")).toBeInTheDocument();
  });

  it("disclaims any access to model-internal reasoning", async () => {
    renderReceipt();
    await userEvent.click(screen.getByText(/Execution receipt/));
    expect(
      screen.getByText(/does not show model-internal reasoning/i),
    ).toBeInTheDocument();
  });

  it("renders nothing for a turn that predates receipts", () => {
    render(
      <MessageList messages={[msg({ id: "r0", role: "assistant", content: "old" })]} />,
    );
    expect(screen.queryByText(/Execution receipt/)).toBeNull();
  });

  it("stays hidden while the turn is still streaming", () => {
    // A receipt for a turn still in flight is not the whole record.
    render(
      <MessageList
        messages={[
          msg({
            id: "r2",
            role: "assistant",
            content: "partial",
            pending: true,
            executionReceipt: receipt(),
          }),
        ]}
      />,
    );
    expect(screen.queryByText(/Execution receipt/)).toBeNull();
  });
  it("keeps tool activity and payloads visible when approval was automatic", async () => {
    render(<MessageList messages={[msg({
      id: "auto-session", role: "assistant", content: "Done",
      steps: [{ kind: "tool_result", tool: "finance_search", label: "Financial information retrieved" }],
      executionReceipt: receipt({
        toolCallCount: 1,
        toolCalls: [{ tool: "finance_search", outcome: "result", approval: "session", consentId: "consent-123",
          arguments: payload('{"query":"MSFT"}'), result: payload('{"currency":"USD"}') }],
      }),
    })]} />);
    await userEvent.click(screen.getByText("Activity · 1 step"));
    expect(screen.getByText("Financial information retrieved")).toBeVisible();
    await userEvent.click(screen.getByText(/Execution receipt/));
    await userEvent.click(screen.getByText("Tool calls · 1"));
    expect(screen.getByText(/Auto-approved for this session/)).toBeVisible();
    expect(screen.getByText("consent-123")).toBeVisible();
    expect(screen.getByText('{"query":"MSFT"}')).toBeInTheDocument();
    expect(screen.getByText('{"currency":"USD"}')).toBeInTheDocument();
    expect(screen.queryByText(/1 requested|1 granted/)).toBeNull();
  });

  it("distinguishes run, per-invocation, not-required, operator and legacy approval provenance", async () => {
    renderReceipt({
      toolCallCount: 5,
      approvalsRequested: 1, approvalsGranted: 1,
      toolCalls: [
        { tool: "run-tool", outcome: "result", approval: "run", consentId: "run-consent" },
        { tool: "one-call", outcome: "result", approval: "invocation" },
        { tool: "safe-call", outcome: "result", approval: "not_required" },
        { tool: "operator-call", outcome: "result", approval: "operator" },
        { tool: "old-call", outcome: "result" },
      ],
    });
    await userEvent.click(screen.getByText(/Execution receipt/));
    await userEvent.click(screen.getByText("Runtime"));
    expect(screen.getByText("Per-call approvals")).toBeVisible();
    expect(screen.getByText("1 requested · 1 granted")).toBeVisible();
    await userEvent.click(screen.getByText("Tool calls · 5"));
    for (const text of ["Auto-approved for this run", "Approved for this invocation", "Approval not required", "Approved by operator policy", "Approval provenance not recorded"]) {
      expect(screen.getByText(new RegExp(text))).toBeVisible();
    }
  });

  it("does not turn a bounded, empty tool list into a false zero-call receipt", async () => {
    renderReceipt({ toolCallCount: 9, toolsOfferedCount: 12, toolCalls: [], toolsOffered: [], truncated: true });
    await userEvent.click(screen.getByText(/12 tools offered, 9 invoked/));
    await userEvent.click(screen.getByText("Tool calls · 9"));
    expect(screen.getByText("Tool-call details were not retained")).toBeVisible();
    expect(screen.queryByText("No tools were invoked")).toBeNull();
  });

});
