import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import Login from "../src/Login";

describe("Login", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it("shows an error message and does not log in on a wrong password", async () => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: false,
      json: async () => ({ detail: "Incorrect password" }),
    });
    const onLoggedIn = vi.fn();
    const user = userEvent.setup();

    render(<Login onLoggedIn={onLoggedIn} />);

    await user.type(screen.getByPlaceholderText("Password"), "wrong-password");
    await user.click(screen.getByRole("button", { name: /log in/i }));

    expect(await screen.findByText("Incorrect password")).toBeInTheDocument();
    expect(onLoggedIn).not.toHaveBeenCalled();
    expect(localStorage.getItem("dashboard_token")).toBeNull();
  });

  it("stores the token and logs in on a correct password", async () => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ token: "real-token-abc" }),
    });
    const onLoggedIn = vi.fn();
    const user = userEvent.setup();

    render(<Login onLoggedIn={onLoggedIn} />);

    await user.type(screen.getByPlaceholderText("Password"), "correct-password");
    await user.click(screen.getByRole("button", { name: /log in/i }));

    await vi.waitFor(() => expect(onLoggedIn).toHaveBeenCalled());
    expect(localStorage.getItem("dashboard_token")).toBe("real-token-abc");
  });
});
