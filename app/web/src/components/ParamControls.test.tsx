// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ParamControls } from "./ParamControls";
import type { ChatParams, ModelEntry } from "@/lib/types";

afterEach(cleanup);

const MODEL: ModelEntry = {
  id: "gpt-test",
  displayName: "GPT Test",
  category: "chat",
  format: "chat",
  conversational: true,
  contextWindow: 128000,
  maxOutputTokens: 4096,
  options: [],
};

function setup(params: ChatParams = {}, model?: ModelEntry | null) {
  const onChange = vi.fn();
  render(<ParamControls params={params} onChange={onChange} model={model} />);
  return { onChange };
}

describe("ParamControls", () => {
  it("gives Temperature, Top P, and Max tokens each an accessible, keyboard-reachable help trigger", () => {
    setup({}, MODEL);
    expect(screen.getByRole("button", { name: "Help: Temperature" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Help: Top P" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Help: Max tokens" })).toBeInTheDocument();
  });

  it("explains Temperature's tradeoff and default in plain language when opened", async () => {
    const user = userEvent.setup();
    setup({}, MODEL);
    await user.click(screen.getByRole("button", { name: "Help: Temperature" }));
    const tooltip = screen.getByRole("tooltip");
    expect(tooltip).toHaveTextContent(/randomness/i);
    expect(tooltip).toHaveTextContent("Default is 0.7");
  });

  it("surfaces the active model's max-output ceiling inside the Max tokens help text", async () => {
    const user = userEvent.setup();
    setup({}, MODEL);
    await user.click(screen.getByRole("button", { name: "Help: Max tokens" }));
    expect(screen.getByRole("tooltip")).toHaveTextContent("currently 4,096");
  });

  it("still clamps the max tokens input to the model's cap on change", async () => {
    const { onChange } = setup({ max_tokens: 100 }, MODEL);
    const input = screen.getByRole("spinbutton");
    fireEvent.change(input, { target: { value: "999999" } });
    expect(onChange).toHaveBeenLastCalledWith(
      expect.objectContaining({ max_tokens: 4096 }),
    );
  });
});
