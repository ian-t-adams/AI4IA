// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ModelPicker } from "./ModelPicker";
import type { ModelEntry } from "@/lib/types";

afterEach(cleanup);

function model(over: Partial<ModelEntry> & Pick<ModelEntry, "id" | "displayName">): ModelEntry {
  return {
    category: "General",
    format: "chat",
    conversational: true,
    contextWindow: null,
    maxOutputTokens: null,
    options: [],
    ...over,
  };
}

describe("ModelPicker", () => {
  it("lists only conversational models and excludes capability models", () => {
    const { container } = render(
      <ModelPicker
        value={null}
        onChange={vi.fn()}
        models={[
          model({ id: "gpt", displayName: "GPT Chat", category: "OpenAI", contextWindow: 400_000 }),
          model({ id: "dalle", displayName: "DALL-E", category: "Image", conversational: false }),
          model({ id: "llama", displayName: "Llama", category: "Meta", contextWindow: 1_000_000 }),
        ]}
      />,
    );

    expect(screen.getByRole("option", { name: /GPT Chat — 400K ctx/ })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /Llama — 1M ctx/ })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: /DALL-E/ })).toBeNull();

    // Grouped by category — image group is dropped entirely with its model.
    expect(container.querySelector('optgroup[label="OpenAI"]')).not.toBeNull();
    expect(container.querySelector('optgroup[label="Meta"]')).not.toBeNull();
    expect(container.querySelector('optgroup[label="Image"]')).toBeNull();
  });

  it("renders a disabled placeholder option", () => {
    render(<ModelPicker value={null} onChange={vi.fn()} models={[]} />);
    expect(screen.getByRole("option", { name: "Select a model…" })).toBeDisabled();
  });

  it("formats context windows compactly and omits the suffix when unknown", () => {
    render(
      <ModelPicker
        value={null}
        onChange={vi.fn()}
        models={[
          model({ id: "mid", displayName: "Mid", contextWindow: 128_000 }),
          model({ id: "big", displayName: "Big", contextWindow: 1_500_000 }),
          model({ id: "plain", displayName: "Plain", contextWindow: null }),
        ]}
      />,
    );
    expect(screen.getByRole("option", { name: /Mid — 128K ctx/ })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /Big — 1\.5M ctx/ })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Plain" })).toBeInTheDocument();
  });

  it("calls onChange with the selected model id", async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(
      <ModelPicker
        value={null}
        onChange={onChange}
        models={[
          model({ id: "gpt", displayName: "GPT Chat", category: "OpenAI" }),
          model({ id: "llama", displayName: "Llama", category: "Meta" }),
        ]}
      />,
    );
    await user.selectOptions(screen.getByRole("combobox", { name: "Model" }), "llama");
    expect(onChange).toHaveBeenCalledWith("llama");
  });
});
