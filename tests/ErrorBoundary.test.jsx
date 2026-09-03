import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import ErrorBoundary from "../src/ErrorBoundary";

function Bomb() {
  throw new Error("simulated render crash - mentions.filter is not a function");
}

describe("ErrorBoundary", () => {
  it("renders its children normally when nothing throws", () => {
    render(
      <ErrorBoundary>
        <p>Dashboard content</p>
      </ErrorBoundary>
    );
    expect(screen.getByText("Dashboard content")).toBeInTheDocument();
  });

  it("catches a render-time crash and shows the fallback instead of going blank", () => {
    // React logs the caught error to the console by default; silence it for this test.
    const consoleSpy = vi.spyOn(console, "error").mockImplementation(() => {});

    render(
      <ErrorBoundary>
        <Bomb />
      </ErrorBoundary>
    );

    expect(screen.getByText("Something went wrong")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /refresh/i })).toBeInTheDocument();

    consoleSpy.mockRestore();
  });
});
