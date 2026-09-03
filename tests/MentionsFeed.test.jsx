import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import MentionsFeed from "../src/components/MentionsFeed";
import { authFetch } from "../src/auth";

vi.mock("../src/auth", () => ({
  authFetch: vi.fn(),
}));

const SAMPLE_MENTION = {
  id: "fb_123",
  source: "Facebook",
  author: "Jane Doe",
  content: "Love this product!",
  sentiment: "Positive",
  label: "Praise",
  explanation: "[English] happy customer",
  link: "https://facebook.com/post?comment_id=123",
  date: "2026-08-20T10:00:00Z",
  parent_id: null,
  status: "Unanswered",
};

describe("MentionsFeed", () => {
  beforeEach(() => {
    authFetch.mockReset();
    authFetch.mockResolvedValue({
      ok: true,
      json: async () => [SAMPLE_MENTION],
      headers: { get: () => null },
    });
  });

  it("shows a confirmation dialog instead of deleting immediately", async () => {
    const user = userEvent.setup();
    render(<MentionsFeed />);

    await screen.findByText("Jane Doe");

    // Only the GET /api/mentions/recent call should have happened so far.
    expect(authFetch).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole("button", { name: /delete/i }));

    // The confirmation modal shows up...
    expect(await screen.findByText("Delete this mention?")).toBeInTheDocument();
    // ...and, critically, no DELETE request has actually been fired yet.
    expect(authFetch).toHaveBeenCalledTimes(1);
  });

  it("only sends the DELETE request after confirming", async () => {
    const user = userEvent.setup();
    render(<MentionsFeed />);

    await screen.findByText("Jane Doe");
    await user.click(screen.getByRole("button", { name: /delete/i }));
    await screen.findByText("Delete this mention?");

    // Both the card's delete button and the modal's confirm button are named
    // "Delete" - the modal renders earlier in the DOM (see MentionsFeed.jsx),
    // so it's the first match.
    const [modalDeleteButton] = screen.getAllByRole("button", { name: "Delete" });
    await user.click(modalDeleteButton);

    // Call 1: initial GET. Call 2: the DELETE. Call 3: the automatic refetch
    // the component correctly triggers after a successful delete.
    await vi.waitFor(() => expect(authFetch).toHaveBeenCalledTimes(3));
    const [path, options] = authFetch.mock.calls[1];
    expect(path).toContain("/api/mentions/fb_123");
    expect(options.method).toBe("DELETE");
  });

  it("clicking Cancel closes the dialog without deleting", async () => {
    const user = userEvent.setup();
    render(<MentionsFeed />);

    await screen.findByText("Jane Doe");
    await user.click(screen.getByRole("button", { name: /delete/i }));
    await screen.findByText("Delete this mention?");

    await user.click(screen.getByRole("button", { name: /cancel/i }));

    expect(screen.queryByText("Delete this mention?")).not.toBeInTheDocument();
    expect(authFetch).toHaveBeenCalledTimes(1);
  });
});
