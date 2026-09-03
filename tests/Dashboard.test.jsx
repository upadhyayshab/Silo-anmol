import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import Dashboard from "../src/Dashboard";
import { authFetch } from "../src/auth";

vi.mock("../src/auth", () => ({
  authFetch: vi.fn(),
}));

describe("Dashboard", () => {
  beforeEach(() => {
    // Every child (KPIs, sentiment chart, mentions feed) fetches through the same
    // mocked authFetch - fail everything, but this test only checks the KPI banner.
    authFetch.mockReset();
    authFetch.mockResolvedValue({
      ok: false,
      status: 500,
      json: async () => ({ detail: "Internal Server Error" }),
      headers: { get: () => null },
    });
  });

  it("shows an error banner instead of silently displaying stale/zero stats when the KPI fetch fails", async () => {
    render(<Dashboard />);

    expect(
      await screen.findByText("Couldn't load latest stats. Showing last known data.")
    ).toBeInTheDocument();

    // The KPI cards should still be visible underneath the banner, just showing 0s.
    expect(screen.getByText("Total Mentions")).toBeInTheDocument();
  });

  it("shows no error banner when the KPI fetch succeeds", async () => {
    authFetch.mockImplementation(async (path) => {
      if (path.includes("/api/analytics/kpis")) {
        return {
          ok: true,
          json: async () => ({
            total_mentions: 42,
            net_sentiment: 10,
            critical_alerts: 0,
            mention_trend_pct: 5,
          }),
          headers: { get: () => null },
        };
      }
      return { ok: true, json: async () => [], headers: { get: () => null } };
    });

    render(<Dashboard />);

    expect(await screen.findByText("42")).toBeInTheDocument();
    expect(
      screen.queryByText("Couldn't load latest stats. Showing last known data.")
    ).not.toBeInTheDocument();
  });
});
