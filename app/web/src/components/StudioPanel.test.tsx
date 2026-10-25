// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { StudioPanel } from "./StudioPanel";

vi.mock("./AgentBuilder", () => ({ AgentBuilder: () => <div>Agent builder content</div> }));
vi.mock("./WorkflowBuilder", () => ({
  WorkflowBuilder: () => <div>Workflow builder content</div>,
}));
vi.mock("./McpServerBuilder", () => ({
  McpServerBuilder: () => <div>Custom tools content</div>,
}));

const baseProps = {
  models: [],
  agents: [],
  runModel: null,
  customToolsEnabled: true,
  onAgentsChanged: async () => {},
  onRun: () => {},
  onClose: () => {},
};

afterEach(cleanup);

describe("StudioPanel", () => {
  it("keeps dialog semantics and closes only from Escape or the backdrop", () => {
    const onClose = vi.fn();
    render(<StudioPanel {...baseProps} onClose={onClose} />);
    const dialog = screen.getByRole("dialog", {
      name: "Agents and workflows builder",
    });

    fireEvent.click(screen.getByTestId("studio-surface"));
    expect(onClose).not.toHaveBeenCalled();
    fireEvent.keyDown(dialog, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
    fireEvent.click(dialog);
    expect(onClose).toHaveBeenCalledTimes(2);
  });

  it("uses standard tabs with arrow-key focus and selection", async () => {
    const user = userEvent.setup();
    render(<StudioPanel {...baseProps} />);

    const agents = screen.getByRole("tab", { name: "Agents" });
    const workflows = screen.getByRole("tab", { name: "Workflows" });
    expect(screen.getByRole("tablist", { name: "Studio sections" })).toBeInTheDocument();
    expect(agents).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tabpanel")).toHaveAccessibleName("Agents");

    agents.focus();
    await user.keyboard("{ArrowRight}");

    expect(workflows).toHaveFocus();
    expect(workflows).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tabpanel")).toHaveAccessibleName("Workflows");
    expect(screen.getByText("Workflow builder content")).toBeInTheDocument();
  });

  it.each([true, false])("uses one shrinkable track per offered tab (custom tools: %s)", (customToolsEnabled) => {
    render(<StudioPanel {...baseProps} customToolsEnabled={customToolsEnabled} />);

    expect(screen.getByTestId("studio-surface")).toHaveStyle({
      maxWidth: "100%",
      minWidth: "0",
      overflow: "hidden",
    });
    expect(screen.getByRole("tablist", { name: "Studio sections" })).toHaveStyle({
      gridTemplateColumns: `repeat(var(--studio-tab-columns, ${customToolsEnabled ? 4 : 3}), minmax(0, 1fr))`,
    });
    expect(screen.getByRole("tablist")).toHaveClass("studio-tabs");
    for (const tab of screen.getAllByRole("tab")) {
      expect(tab).toHaveClass("workspace-tab");
      expect(tab.style.minHeight).toBe("");
    }
    expect(screen.getByRole("button", { name: "Close builder" })).toHaveStyle({
      minWidth: "44px",
      minHeight: "44px",
    });
    expect(screen.getByRole("tabpanel")).toHaveClass("studio-tabpanel");
  });
});
