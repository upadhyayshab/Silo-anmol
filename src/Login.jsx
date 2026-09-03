import { useState } from "react";
import { API_BASE_URL } from "./api";
import { setToken } from "./auth";

export default function Login({ onLoggedIn }) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState(null);
  const [submitting, setSubmitting] = useState(false);

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError(null);
    setSubmitting(true);

    try {
      const res = await fetch(`${API_BASE_URL}/api/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password }),
      });
      const data = await res.json();

      if (res.ok) {
        setToken(data.token);
        onLoggedIn();
      } else {
        setError(data.detail || "Login failed.");
      }
    } catch (err) {
      console.error("Login error:", err);
      setError("Couldn't reach the server. Is the backend running?");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="min-h-screen bg-gray-50 flex items-center justify-center p-4">
      <form
        onSubmit={handleSubmit}
        className="bg-white rounded-xl shadow-sm border border-gray-100 p-8 w-full max-w-sm"
      >
        <h1 className="text-xl font-bold text-gray-900 mb-1">Social Listening Dashboard</h1>
        <p className="text-sm text-gray-500 mb-6">Enter the password to continue.</p>

        <label htmlFor="password" className="sr-only">
          Password
        </label>
        <input
          id="password"
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder="Password"
          autoFocus
          className="w-full border border-gray-200 rounded-md p-2.5 text-sm outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500 mb-4"
        />

        {error && (
          <p className="text-sm text-red-600 mb-4" role="alert">
            {error}
          </p>
        )}

        <button
          type="submit"
          disabled={submitting || !password}
          className="w-full bg-blue-600 text-white rounded-md py-2.5 text-sm font-medium hover:bg-blue-700 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {submitting ? "Logging in..." : "Log In"}
        </button>
      </form>
    </div>
  );
}
