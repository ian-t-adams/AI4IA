// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ImageGenerationControls } from "./ImageGenerationControls";
import type { ImageOptionsResponse } from "@/lib/types";

const mocks = vi.hoisted(() => ({
  getImageOptions: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  getImageOptions: mocks.getImageOptions,
  apiErrorDetail: (reason: unknown) =>
    reason instanceof Error ? reason.message : "Something went wrong.",
}));

const OPTIONS: ImageOptionsResponse = {
  maxSelectedModels: 3,
  currency: "USD",
  priceVersion: "test",
  models: [
    {
      id: "flux",
      displayName: "FLUX",
      provider: "black_forest_labs",
      sizes: ["1024x1024", "1024x1440"],
      qualities: ["auto"],
      dataZones: ["us"],
      residencies: ["global"],
      prices: [
        {
          size: "1024x1024",
          quality: "auto",
          costKnown: true,
          estimatedCostUsd: 0.04,
          pricingBasis: "image",
        },
        {
          size: "1024x1440",
          quality: "auto",
          costKnown: true,
          estimatedCostUsd: 0.04,
          pricingBasis: "image",
        },
      ],
    },
    {
      id: "mai",
      displayName: "MAI Image",
      provider: "microsoft",
      sizes: ["1024x1024"],
      qualities: ["auto"],
      dataZones: ["us"],
      residencies: ["global"],
      prices: [
        {
          size: "1024x1024",
          quality: "auto",
          costKnown: false,
          estimatedCostUsd: null,
          pricingBasis: null,
        },
      ],
    },
  ],
};

beforeEach(() => {
  mocks.getImageOptions.mockResolvedValue(OPTIONS);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("ImageGenerationControls", () => {
  it("saves a bounded multi-model comparison with the common options", async () => {
    const onSave = vi.fn();
    const user = userEvent.setup();
    render(
      <ImageGenerationControls
        preferences={{ models: [], size: null, quality: null }}
        disabled={false}
        onSave={onSave}
        onReset={() => {}}
        onStart={() => {}}
      />,
    );

    await user.click(await screen.findByRole("checkbox", { name: /FLUX/ }));
    await user.click(screen.getByRole("checkbox", { name: /MAI Image/ }));
    await user.click(screen.getByRole("button", { name: "Save image setup" }));

    expect(onSave).toHaveBeenCalledWith({
      models: ["flux", "mai"],
      size: "1024x1024",
      quality: "auto",
    });
    expect(screen.getByText(/Estimated \$0.040 per image/)).toBeInTheDocument();
    expect(screen.getByText("Cost estimate unavailable")).toBeInTheDocument();
    expect(
      screen.getByText(
        "Known subtotal: $0.040; 1 model cost estimate unavailable",
      ),
    ).toBeInTheDocument();
  });

  it("requires saving a changed setup before starting the chat command", async () => {
    const onStart = vi.fn();
    const user = userEvent.setup();
    const { rerender } = render(
      <ImageGenerationControls
        preferences={{ models: ["flux"], size: "1024x1024", quality: "auto" }}
        disabled={false}
        onSave={() => {}}
        onReset={() => {}}
        onStart={onStart}
      />,
    );

    const start = await screen.findByRole("button", {
      name: "Start image in chat",
    });
    expect(start).toBeEnabled();
    await user.click(start);
    expect(onStart).toHaveBeenCalledOnce();

    await user.click(screen.getByRole("checkbox", { name: /MAI Image/ }));
    expect(
      screen.getByRole("button", { name: "Start comparison in chat" }),
    ).toBeDisabled();

    rerender(
      <ImageGenerationControls
        preferences={{
          models: ["flux", "mai"],
          size: "1024x1024",
          quality: "auto",
        }}
        disabled={false}
        onSave={() => {}}
        onReset={() => {}}
        onStart={onStart}
      />,
    );
    expect(
      screen.getByRole("button", { name: "Start comparison in chat" }),
    ).toBeEnabled();
  });
});
