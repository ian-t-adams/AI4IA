// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import userEvent from "@testing-library/user-event";
import { DecisionPlayground } from "./DecisionPlayground";
import { apiFetch } from "@/lib/auth";
import type { DecisionRequest } from "@/lib/decisions";

vi.mock("@/lib/auth", () => ({ apiFetch: vi.fn() }));
const fetchMock = vi.mocked(apiFetch);
const model = "Microsoft-Decision-1";
const options = { models: [{ id: model, displayName: "Microsoft Decision 1 (Preview)", residencies: ["us"] }] };
function result(request: DecisionRequest) {
  return {
    model, reportedModel: "microsoft-decision-1", region: "eastus2", residency: "us", latencyMs: 125,
    usage: { known: true, inputTokens: 10, outputTokens: 2, totalTokens: 12 },
    costKnown: false, estimatedCostUsd: null,
    answers: Object.fromEntries(request.questions.map((q) => [q.name, q.type === "noul"
      ? { type: "noul", noul: .8 }
      : q.type === "choice" ? {
        type: "choice", choice: Object.keys(q.criteria)[0], confidence: .7,
        probabilities: Object.fromEntries(Object.keys(q.criteria).map((key, i) => [key, i === 0 ? .8 : .1])),
      } : { type: "score", score: .3, confidence: .9, probabilities: { "0": .8, "1": .1, "2": .1 } }])),
  };
}
beforeEach(() => {
  fetchMock.mockReset();
  fetchMock.mockImplementation(async (path, init) => path === "/api/decisions/options"
    ? Response.json(options) : Response.json(result(JSON.parse(String(init?.body)))));
});
afterEach(cleanup);
async function example() {
  render(<DecisionPlayground />);
  await waitFor(() => expect(screen.getByRole("button", { name: "Load synthetic example" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "Load synthetic example" }));
}

describe("Decision playground", () => {
  it("waits for explicit Run, sends all native question shapes and renders typed results", async () => {
    await example();
    const form = screen.getByRole("form", { name: "Decision input" });
    for (const field of form.querySelectorAll("input, select, textarea")) {
      expect(field).toHaveClass("field-control");
    }
    for (const button of form.querySelectorAll("button")) {
      expect(button).toHaveClass("btn");
    }
    expect(screen.getByRole("button", { name: "Run decision" })).toHaveClass("btn-primary");
    expect(fetchMock).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "Run decision" }));
    expect(await screen.findByRole("heading", { name: "Results" })).toHaveFocus();
    expect(screen.getByText("Yes probability: 80.0%")).toBeInTheDocument();
    expect(screen.getByText("Selected: design")).toBeInTheDocument();
    expect(screen.getByText("Score: 0.300 on a 0–2 scale")).toBeInTheDocument();
    expect(screen.getByText(/Reported confidence: 70.0%/)).toBeInTheDocument();
    expect(screen.getByRole("table", { name: "severity probability distribution" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Decision results" })).toHaveTextContent("Cost: Unknown");
    expect(screen.getByRole("region", { name: "Decision results" })).toHaveTextContent("microsoft-decision-1");
    const sent = JSON.parse(String(fetchMock.mock.calls[1][1]?.body));
    expect(sent.questions.map((q: { type: string }) => q.type)).toEqual(["noul", "choice", "score"]);
    expect(sent.questions[1].criteria).toEqual({ design: "Visual appearance and layout", engineering: "Crashes and broken functions", unclear: "Not enough information" });
    fireEvent.change(screen.getByLabelText("Content to evaluate"), { target: { value: "Changed" } });
    expect(screen.queryByRole("region", { name: "Decision results" })).not.toBeInTheDocument();
  });

  it("prevents double submission, locks edits, and supports cancellation without retry", async () => {
    let resolve: (value: Response) => void = () => {};
    fetchMock.mockImplementation(async (path) => path === "/api/decisions/options" ? Response.json(options)
      : new Promise<Response>((done) => { resolve = done; }));
    await example();
    const form = screen.getByRole("form", { name: "Decision input" });
    fireEvent.submit(form); fireEvent.submit(form);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(screen.getByLabelText("Content to evaluate")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Running..." })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Cancel run" }));
    expect(fetchMock.mock.calls[1][1]?.signal?.aborted).toBe(true);
    await act(async () => resolve(Response.json(result(JSON.parse(String(fetchMock.mock.calls[1][1]?.body))))));
    expect(screen.queryByRole("heading", { name: "Results" })).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("shows errors without a successful empty result or automatic retry", async () => {
    fetchMock.mockImplementation(async (path) => path === "/api/decisions/options" ? Response.json(options)
      : Response.json({ detail: "Rate limited. Run again later." }, { status: 429 }));
    await example();
    fireEvent.click(screen.getByRole("button", { name: "Run decision" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Rate limited");
    expect(screen.queryByRole("heading", { name: "Results" })).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(screen.getByRole("button", { name: "Run decision" })).toBeEnabled();
  });

  it("validates parsed JSON and unique names before dispatch", async () => {
    await example();
    fireEvent.change(screen.getByLabelText("Input format"), { target: { value: "json" } });
    fireEvent.click(screen.getByRole("button", { name: "Run decision" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("not valid JSON");
    expect(fetchMock).toHaveBeenCalledTimes(1);
    fireEvent.change(screen.getByLabelText("Content to evaluate"), { target: { value: '{"message":"Synthetic"}' } });
    const names = screen.getAllByLabelText("Name");
    fireEvent.change(names[1], { target: { value: "repeat_contact" } });
    fireEvent.click(screen.getByRole("button", { name: "Run decision" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("names must be unique");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("edits all types, optional criteria and ordered levels with keyboard controls", async () => {
    const user = userEvent.setup();
    render(<DecisionPlayground />);
    await waitFor(() => expect(screen.getByRole("button", { name: "Run decision" })).toBeEnabled());
    const question = screen.getByRole("group", { name: "Question 1" });
    const type = within(question).getByLabelText("Answer type");
    await user.selectOptions(type, "score");
    expect(within(question).getByLabelText("Level 0 label")).toBeInTheDocument();
    await user.click(within(question).getByRole("button", { name: "Add level" }));
    expect(within(question).getByLabelText("Level 2 label")).toBeInTheDocument();
    await user.selectOptions(type, "choice");
    expect(within(question).getByLabelText("Option 1 name")).toBeInTheDocument();
    await user.selectOptions(type, "noul");
    await user.click(screen.getByText("Optional yes/no criteria"));
    await user.type(screen.getByLabelText("Yes means"), "Reported");
    expect(screen.getByLabelText("Yes means")).toHaveValue("Reported");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("shows unavailable and failed availability distinctly and never enables Run", async () => {
    fetchMock.mockResolvedValueOnce(Response.json({ models: [] }));
    render(<DecisionPlayground />);
    expect(await screen.findByText(/No Decision model is available/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run decision" })).toBeDisabled();
    cleanup();
    fetchMock.mockRejectedValueOnce(new Error("Availability failed"));
    render(<DecisionPlayground />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Availability failed");
    expect(screen.getByRole("button", { name: "Run decision" })).toBeDisabled();
  });

  it("rejects malformed or missing numeric feedback", async () => {
    fetchMock.mockImplementation(async (path) => path === "/api/decisions/options" ? Response.json(options)
      : Response.json({ model, answers: {}, usage: {} }));
    await example();
    fireEvent.click(screen.getByRole("button", { name: "Run decision" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("invalid data");
    expect(screen.queryByRole("heading", { name: "Results" })).not.toBeInTheDocument();
  });
});
