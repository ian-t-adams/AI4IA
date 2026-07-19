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

  it("warns that GPT-5 and o-series models ignore Temperature server-side, without claiming it applies to every model", async () => {
    const user = userEvent.setup();
    setup({}, MODEL);
    await user.click(screen.getByRole("button", { name: "Help: Temperature" }));
    const tooltip = screen.getByRole("tooltip");
    expect(tooltip).toHaveTextContent(/GPT-5 and\s+o-series models ignore/);
    expect(tooltip).not.toHaveTextContent(/all models/i);
  });

  it("warns that GPT-5 and o-series models ignore Top P server-side too", async () => {
    const user = userEvent.setup();
    setup({}, MODEL);
    await user.click(screen.getByRole("button", { name: "Help: Top P" }));
    expect(screen.getByRole("tooltip")).toHaveTextContent(/GPT-5 and\s+o-series models ignore/);
  });

  it("surfaces the active model's max-output ceiling inside the Max tokens help text", async () => {
    const user = userEvent.setup();
    setup({}, MODEL);
    await user.click(screen.getByRole("button", { name: "Help: Max tokens" }));
    expect(screen.getByRole("tooltip")).toHaveTextContent("currently 4,096");
  });

  it("explains that the 1024 default is treated as no preference rather than an honored cap", async () => {
    const user = userEvent.setup();
    setup({}, MODEL);
    await user.click(screen.getByRole("button", { name: "Help: Max tokens" }));
    const tooltip = screen.getByRole("tooltip");
    expect(tooltip).toHaveTextContent(/default, 1024,.*no preference/);
    expect(tooltip).toHaveTextContent(/expands to the model.s full ceiling/);
  });

  it("discloses the higher minimum some flagship models enforce regardless of a lower value", async () => {
    const user = userEvent.setup();
    setup({}, MODEL);
    await user.click(screen.getByRole("button", { name: "Help: Max tokens" }));
    expect(screen.getByRole("tooltip")).toHaveTextContent(/16,384/);
  });

  it("does not claim the 1024 default expands to a ceiling when the model publishes no max-output size", async () => {
    const user = userEvent.setup();
    const modelWithoutCeiling: ModelEntry = { ...MODEL, maxOutputTokens: null };
    setup({}, modelWithoutCeiling);
    await user.click(screen.getByRole("button", { name: "Help: Max tokens" }));
    const tooltip = screen.getByRole("tooltip");
    expect(tooltip).toHaveTextContent(/no maximum-output size/i);
    expect(tooltip).toHaveTextContent(/literal cap/i);
    expect(tooltip).not.toHaveTextContent(/expands to/i);
    expect(tooltip).not.toHaveTextContent(/no preference/i);
  });

  it("still discloses the flagship minimum even when the model publishes no max-output size", async () => {
    const user = userEvent.setup();
    const modelWithoutCeiling: ModelEntry = { ...MODEL, maxOutputTokens: null };
    setup({}, modelWithoutCeiling);
    await user.click(screen.getByRole("button", { name: "Help: Max tokens" }));
    expect(screen.getByRole("tooltip")).toHaveTextContent(/16,384/);
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
