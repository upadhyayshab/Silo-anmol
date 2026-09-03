import { useState, useEffect } from "react";
import Dashboard from "./Dashboard";
import Login from "./Login";
import ErrorBoundary from "./ErrorBoundary";
import { getToken, onSessionExpired } from "./auth";

function App() {
  const [isAuthenticated, setIsAuthenticated] = useState(!!getToken());

  // If any request comes back unauthenticated (missing/expired/invalid token),
  // drop back to the login screen.
  useEffect(() => {
    return onSessionExpired(() => setIsAuthenticated(false));
  }, []);

  return (
    <main className="min-h-screen bg-gray-50">
      {isAuthenticated ? (
        <ErrorBoundary>
          <Dashboard />
        </ErrorBoundary>
      ) : (
        <Login onLoggedIn={() => setIsAuthenticated(true)} />
      )}
    </main>
  );
}

export default App;
