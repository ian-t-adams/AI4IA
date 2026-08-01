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
  supportsSampling: true,
  reasoningEffortOptions: [],
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

  it("no longer apologises for models that ignore Temperature, now that it is hidden for them", async () => {
    const user = userEvent.setup();
    setup({}, MODEL);
    await user.click(screen.getByRole("button", { name: "Help: Temperature" }));
    const tooltip = screen.getByRole("tooltip");
    // The old copy warned "GPT-5 and o-series models ignore this control on the
    // server". That was a workaround for showing a dead slider; the slider is
    // now hidden for exactly those models, so the warning would be misleading.
    expect(tooltip).not.toHaveTextContent(/ignore/i);
    expect(tooltip).not.toHaveTextContent(/all models/i);
  });

  it("hides Temperature and Top P for a model whose sampling params the gateway strips", () => {
    const reasoning: ModelEntry = { ...MODEL, supportsSampling: false };
    setup({}, reasoning);
    expect(screen.queryByRole("button", { name: "Help: Temperature" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Help: Top P" })).toBeNull();
    // Max tokens is unaffected: it is translated, not stripped.
    expect(screen.getByRole("button", { name: "Help: Max tokens" })).toBeInTheDocument();
  });

  it("offers no reasoning-effort control for a model that does not accept one", () => {
    setup({}, MODEL);
    expect(screen.queryByLabelText("Reasoning effort")).toBeNull();
  });

  it("offers exactly the reasoning-effort values the server says the model allows", () => {
    const reasoning: ModelEntry = {
      ...MODEL,
      supportsSampling: false,
      reasoningEffortOptions: ["minimal", "low", "medium", "high"],
    };
    setup({}, reasoning);
    const select = screen.getByLabelText("Reasoning effort") as HTMLSelectElement;
    expect([...select.options].map((o) => o.value)).toEqual([
      "",
      "minimal",
      "low",
      "medium",
      "high",
    ]);
  });

  it("omits a value the server did not offer for this model", () => {
    // gpt-5.6 rejects the "minimal" that gpt-5.4 accepts, so the option list is
    // per-model data from the server, not something the UI can derive.
    const narrower: ModelEntry = {
      ...MODEL,
      supportsSampling: false,
      reasoningEffortOptions: ["low", "medium", "high"],
    };
    setup({}, narrower);
    const select = screen.getByLabelText("Reasoning effort") as HTMLSelectElement;
    expect([...select.options].map((o) => o.value)).not.toContain("minimal");
  });

  it("labels 'none' so it cannot be read as 'model default'", () => {
    // Two entries that mean opposite things: "" leaves the choice to the model,
    // "none" explicitly tells it not to reason. Bare title-case made them look
    // like the same option.
    const reasoning: ModelEntry = {
      ...MODEL,
      supportsSampling: false,
      reasoningEffortOptions: ["none", "low", "high", "xhigh"],
    };
    setup({}, reasoning);
    const labels = [...(screen.getByLabelText("Reasoning effort") as HTMLSelectElement).options].map(
      (o) => o.textContent,
    );
    expect(labels[0]).toBe("Model default");
    expect(labels).toContain("None (skip reasoning)");
    expect(labels).toContain("Extra high");
    // Unmapped values still render, title-cased.
    expect(labels).toContain("Low");
  });

  it("sends the chosen reasoning effort", () => {
    const reasoning: ModelEntry = {
      ...MODEL,
      supportsSampling: false,
      reasoningEffortOptions: ["minimal", "low", "medium", "high"],
    };
    const { onChange } = setup({}, reasoning);
    fireEvent.change(screen.getByLabelText("Reasoning effort"), {
      target: { value: "high" },
    });
    expect(onChange).toHaveBeenLastCalledWith(
      expect.objectContaining({ reasoning_effort: "high" }),
    );
  });

  it("falls back to the model default when a carried-over effort is not offered", () => {
    // Switching gpt-5.4 -> gpt-5.6 leaves "minimal" in params; gpt-5.6 400s on
    // it. The <select> must not render a value it has no <option> for.
    const narrower: ModelEntry = {
      ...MODEL,
      supportsSampling: false,
      reasoningEffortOptions: ["low", "medium", "high"],
    };
    setup({ reasoning_effort: "minimal" }, narrower);
    const select = screen.getByLabelText("Reasoning effort") as HTMLSelectElement;
    expect(select.value).toBe("");
  });

  it("removes reasoning_effort entirely when returned to the model default", () => {
    // Sending an empty string would reach the provider as an invalid value; the
    // absence of the key is what means "no preference".
    const reasoning: ModelEntry = {
      ...MODEL,
      supportsSampling: false,
      reasoningEffortOptions: ["minimal", "low", "medium", "high"],
    };
    const { onChange } = setup({ reasoning_effort: "high" }, reasoning);
    fireEvent.change(screen.getByLabelText("Reasoning effort"), { target: { value: "" } });
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange.mock.calls[0][0]).not.toHaveProperty("reasoning_effort");
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
