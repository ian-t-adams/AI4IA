// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
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

  it("keeps navigation and content reachable at 390px and 200% zoom", () => {
    Object.defineProperty(window, "innerWidth", { value: 390, configurable: true });
    document.documentElement.style.setProperty("--font-scale", "2");
    render(<StudioPanel {...baseProps} />);

    expect(screen.getByTestId("studio-surface")).toHaveStyle({
      maxWidth: "100%",
      minWidth: "0",
      overflow: "hidden",
    });
    expect(screen.getByRole("tablist", { name: "Studio sections" })).toHaveStyle({
      overflowX: "auto",
      minWidth: "0",
    });
    for (const tab of screen.getAllByRole("tab")) {
      expect(tab).toHaveStyle({ minHeight: "44px" });
    }
    expect(screen.getByRole("button", { name: "Close builder" })).toHaveStyle({
      minWidth: "44px",
      minHeight: "44px",
    });
    expect(screen.getByRole("tabpanel")).toHaveStyle({ overflow: "auto" });
  });
});
