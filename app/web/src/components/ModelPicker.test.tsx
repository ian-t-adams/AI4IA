// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ModelPicker } from "./ModelPicker";
import type { DeploymentOption, ModelEntry } from "@/lib/types";

afterEach(cleanup);

function model(over: Partial<ModelEntry> & Pick<ModelEntry, "id" | "displayName">): ModelEntry {
  return {
    category: "General",
    format: "chat",
    conversational: true,
    contextWindow: null,
    maxOutputTokens: null,
    supportsSampling: true,
    reasoningEffortOptions: [],
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

  it("shows plain-language category help for the selected model", () => {
    const { rerender } = render(
      <ModelPicker
        value="reason"
        onChange={vi.fn()}
        models={[
          model({ id: "reason", displayName: "Deep Thinker", category: "reasoning" }),
          model({ id: "plain", displayName: "Plain", category: "unknown-category" }),
        ]}
      />,
    );
    expect(screen.getByText(/Reasoning\./)).toBeInTheDocument();
    expect(screen.getByText(/multi-step logic/)).toBeInTheDocument();
    // The note lives inside the same wrapping <label> as the <select>; it
    // must not leak into the select's accessible name (regression test).
    expect(screen.getByRole("combobox", { name: "Model" })).toBeInTheDocument();

    // A category with no curated help copy (or no selection) shows no note
    // instead of a blank/broken block.
    rerender(
      <ModelPicker
        value="plain"
        onChange={vi.fn()}
        models={[
          model({ id: "reason", displayName: "Deep Thinker", category: "reasoning" }),
          model({ id: "plain", displayName: "Plain", category: "unknown-category" }),
        ]}
      />,
    );
    expect(screen.queryByText(/Reasoning\./)).toBeNull();
  });

  it("associates the category note with the select via aria-describedby, with no dangling reference when there's no note", () => {
    const { container, rerender } = render(
      <ModelPicker
        value="reason"
        onChange={vi.fn()}
        models={[model({ id: "reason", displayName: "Deep Thinker", category: "reasoning" })]}
      />,
    );
    const select = screen.getByRole("combobox", { name: "Model" });
    const describedBy = select.getAttribute("aria-describedby");
    expect(describedBy).toBeTruthy();
    // The id must resolve to a real element in the document (the note itself),
    // not a dangling/broken IDREF a screen reader can't follow.
    const note = container.querySelector(`#${describedBy}`);
    expect(note).not.toBeNull();
    expect(note).toHaveTextContent(/Reasoning\./);

    // No curated help for this category -> no note element -> the select must
    // not point aria-describedby at an id that doesn't exist in the DOM.
    rerender(
      <ModelPicker
        value="plain"
        onChange={vi.fn()}
        models={[model({ id: "plain", displayName: "Plain", category: "unknown-category" })]}
      />,
    );
    expect(select).not.toHaveAttribute("aria-describedby");
  });
});

// The residency note is the point where a user decides whether a model is
// acceptable for their data. It reports the residency the SERVER derived from
// each deployment's SKU — never the endpoint's geography, because a
// GlobalStandard deployment in an EU region may still be processed anywhere and
// calling that "EU" would assert a guarantee Azure is not making.
describe("ModelPicker data-residency note", () => {
  function option(over: Partial<DeploymentOption> & Pick<DeploymentOption, "residency">) {
    return {
      region: "eastus2",
      dataZone: "US",
      sku: "DataZoneStandard",
      deploymentName: "d",
      ...over,
    } satisfies DeploymentOption;
  }

  it("names the single zone a bounded model stays in", () => {
    render(
      <ModelPicker
        value="m"
        onChange={vi.fn()}
        models={[model({ id: "m", displayName: "M", options: [option({ residency: "eu" })] })]}
      />,
    );
    expect(screen.getByText(/Processing stays in the EU data zone/)).toBeInTheDocument();
  });

  it("names every zone when a model could land in more than one", () => {
    // Under the `zonal` policy a model may have a compliant deployment in each
    // zone. Naming only one would be a guess; the user needs the set.
    render(
      <ModelPicker
        value="m"
        onChange={vi.fn()}
        models={[
          model({
            id: "m",
            displayName: "M",
            options: [option({ residency: "us" }), option({ residency: "eu" })],
          }),
        ]}
      />,
    );
    expect(
      screen.getByText(/Processing stays in the EU or US data zone/),
    ).toBeInTheDocument();
  });

  it("says plainly when a model may process anywhere", () => {
    render(
      <ModelPicker
        value="m"
        onChange={vi.fn()}
        models={[
          model({
            id: "m",
            displayName: "M",
            options: [option({ residency: "global", sku: "GlobalStandard" })],
          }),
        ]}
      />,
    );
    expect(screen.getByText(/May process in any Azure region worldwide/)).toBeInTheDocument();
  });

  it("does not claim a boundary when any eligible deployment is global", () => {
    // A model with one bounded and one global deployment could be served by
    // either, so the honest statement is the weaker one.
    render(
      <ModelPicker
        value="m"
        onChange={vi.fn()}
        models={[
          model({
            id: "m",
            displayName: "M",
            options: [option({ residency: "eu" }), option({ residency: "global" })],
          }),
        ]}
      />,
    );
    expect(screen.getByText(/May process in any Azure region worldwide/)).toBeInTheDocument();
    expect(screen.queryByText(/stays in the/)).toBeNull();
  });

  it("renders nothing without a selection or deployment metadata", () => {
    const { rerender } = render(
      <ModelPicker value={null} onChange={vi.fn()} models={[model({ id: "m", displayName: "M" })]} />,
    );
    expect(screen.queryByText(/Data residency/)).toBeNull();

    // Selected, but the catalog carried no deployment metadata: say nothing
    // rather than guessing a residency.
    rerender(
      <ModelPicker value="m" onChange={vi.fn()} models={[model({ id: "m", displayName: "M" })]} />,
    );
    expect(screen.queryByText(/Data residency/)).toBeNull();
  });

  it("is announced to screen readers alongside the category help", () => {
    render(
      <ModelPicker
        value="m"
        onChange={vi.fn()}
        models={[
          model({
            id: "m",
            displayName: "M",
            category: "chat",
            options: [option({ residency: "us" })],
          }),
        ]}
      />,
    );
    const describedBy = screen.getByLabelText("Model").getAttribute("aria-describedby");
    expect(describedBy).toBeTruthy();
    // Both notes are referenced, and every referenced id exists in the DOM.
    const ids = (describedBy ?? "").split(" ").filter(Boolean);
    expect(ids.length).toBe(2);
    for (const id of ids) expect(document.getElementById(id)).not.toBeNull();
  });
});
