// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DecisionPlayground } from "./DecisionPlayground";
import { apiFetch } from "@/lib/auth";

vi.mock("@/lib/auth", () => ({ apiFetch: vi.fn() }));
const fetchMock = vi.mocked(apiFetch);

beforeEach(() => {
  fetchMock.mockReset();
  fetchMock.mockResolvedValue(Response.json({
    models: [{ id: "Microsoft-Decision-1", displayName: "Decision", residencies: ["us"] }],
  }));
});
afterEach(cleanup);

async function choiceQuestion() {
  render(<DecisionPlayground />);
  await waitFor(() => expect(screen.getByRole("button", { name: "Run decision" })).toBeEnabled());
  const question = screen.getByRole("group", { name: "Question 1" });
  fireEvent.change(within(question).getByLabelText("Answer type"), { target: { value: "choice" } });
  return question;
}

describe("Decision draft preservation", () => {
  it.each(["noul", "score"])("retains all choice options after switching through %s", async (type) => {
    const question = await choiceQuestion();
    for (let i = 0; i < 10; i++) {
      fireEvent.click(within(question).getByRole("button", { name: "Add option" }));
    }
    fireEvent.change(within(question).getByLabelText("Option 11 name"), { target: { value: "eleventh" } });
    fireEvent.change(within(question).getByLabelText("Option 11 description"), { target: { value: "Synthetic eleven" } });
    fireEvent.change(within(question).getByLabelText("Option 12 name"), { target: { value: "twelfth" } });
    fireEvent.change(within(question).getByLabelText("Option 12 description"), { target: { value: "Synthetic twelve" } });
    fireEvent.change(within(question).getByLabelText("Answer type"), { target: { value: type } });
    fireEvent.change(within(question).getByLabelText("Answer type"), { target: { value: "choice" } });
    expect(within(question).getByLabelText("Option 11 name")).toHaveValue("eleventh");
    expect(within(question).getByLabelText("Option 11 description")).toHaveValue("Synthetic eleven");
    expect(within(question).getByLabelText("Option 12 name")).toHaveValue("twelfth");
    expect(within(question).getByLabelText("Option 12 description")).toHaveValue("Synthetic twelve");
  });

  it("generates a distinct option name after removing a nonfinal option", async () => {
    const question = await choiceQuestion();
    fireEvent.click(within(question).getByRole("button", { name: "Add option" }));
    fireEvent.click(within(question).getByRole("button", { name: "Add option" }));
    fireEvent.click(within(question).getByRole("button", { name: "Remove option 3 from question 1" }));
    fireEvent.click(within(question).getByRole("button", { name: "Add option" }));
    const names = within(question).getAllByLabelText(/^Option \d+ name$/).map((field) => (field as HTMLInputElement).value);
    expect(new Set(names).size).toBe(names.length);
  });

  it("explains and blocks an oversized score until the user removes excess levels", async () => {
    fetchMock.mockImplementation(async (path) => path === "/api/decisions/options"
      ? Response.json({ models: [{ id: "Microsoft-Decision-1", displayName: "Decision", residencies: ["us"] }] })
      : Response.json({
        model: "Microsoft-Decision-1", reportedModel: "microsoft-decision-1",
        region: "eastus2", residency: "us", latencyMs: 1,
        usage: { known: false, inputTokens: null, outputTokens: null, totalTokens: null },
        costKnown: false, estimatedCostUsd: null,
        answers: { question_1: {
          type: "score", score: 0,
          probabilities: Object.fromEntries(Array.from({ length: 10 }, (_, i) => [String(i), i === 0 ? 1 : 0])),
        } },
      }));
    const question = await choiceQuestion();
    fireEvent.change(screen.getByLabelText("Content to evaluate"), { target: { value: "Synthetic input" } });
    fireEvent.change(within(question).getByLabelText("Instructions"), { target: { value: "Synthetic rating" } });
    for (let i = 0; i < 10; i++) {
      fireEvent.click(within(question).getByRole("button", { name: "Add option" }));
    }
    for (let i = 1; i <= 12; i++) {
      fireEvent.change(within(question).getByLabelText(`Option ${i} description`), { target: { value: `Synthetic level ${i}` } });
    }
    fireEvent.change(within(question).getByLabelText("Answer type"), { target: { value: "score" } });
    expect(screen.getByRole("status")).toHaveTextContent("Remove 2 levels before running, or switch back to Choice to keep all options.");
    fireEvent.submit(screen.getByRole("form", { name: "Decision input" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Use 2 to 10 levels in question_1.");
    expect(fetchMock).toHaveBeenCalledTimes(1);

    fireEvent.click(within(question).getByRole("button", { name: "Remove level 12 from question 1" }));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Remove 1 level before running");
    fireEvent.click(within(question).getByRole("button", { name: "Remove level 11 from question 1" }));
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    fireEvent.submit(screen.getByRole("form", { name: "Decision input" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    const request = JSON.parse(String(fetchMock.mock.calls[1][1]?.body));
    expect(request.questions[0].criteria).toEqual(Array.from({ length: 10 }, (_, i) => `Synthetic level ${i + 1}`));
    expect(await screen.findByText("Score: 0.000 on a 0–9 scale")).toBeInTheDocument();
  });
});
