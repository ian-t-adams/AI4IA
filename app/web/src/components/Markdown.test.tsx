// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { RetrievedSource } from "@/lib/types";
import { Markdown } from "./Markdown";

afterEach(cleanup);

const SOURCE: RetrievedSource = {
  spanId: "S1",
  documentId: "doc-1",
  filename: "lecture.mp3",
  startMs: 754_000,
  endMs: 760_000,
  excerpt: "Grounded excerpt",
  contentSha256: "abc123",
  retrievedAt: "2026-08-14T12:00:00Z",
};

describe("Markdown", () => {
  it("renders GFM safely without loading model-authored external images or HTML", () => {
    const { container } = render(
      <Markdown
        content={[
          "| Item | State |",
          "| --- | --- |",
          "| [Docs](https://example.com/docs) | Ready |",
          "",
          "![tracking pixel](https://attacker.example/pixel.png)",
          "",
          "<script>window.compromised = true</script>",
        ].join("\n")}
      />,
    );

    expect(screen.getByRole("table")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Docs" })).toHaveAttribute(
      "target",
      "_blank",
    );
    expect(screen.getByRole("link", { name: "Docs" })).toHaveAttribute(
      "rel",
      "noreferrer noopener",
    );
    expect(container.querySelector("img")).toBeNull();
    expect(
      screen.getByText("[External image omitted: tracking pixel]"),
    ).toBeInTheDocument();
    expect(container.querySelector("script")).toBeNull();
  });

  it("makes verified citations actionable and invented span ids visibly unverified", async () => {
    const onCitation = vi.fn();
    const user = userEvent.setup();
    render(
      <Markdown
        content="Verified [[cite:S1]]; invented [[cite:S9]]."
        sources={[SOURCE]}
        onCitation={onCitation}
      />,
    );

    await user.click(
      screen.getByRole("button", {
        name: "Play lecture.mp3 · 12:34, verified source S1",
      }),
    );
    expect(onCitation).toHaveBeenCalledWith({
      documentId: "doc-1",
      filename: "lecture.mp3",
      ms: 754_000,
    });
    expect(screen.getByText("Unverified citation")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /S9/ })).toBeNull();
  });
});
