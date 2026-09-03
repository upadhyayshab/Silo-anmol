import { useState, useEffect } from "react";
import {
  ResponsiveContainer,
  LineChart,
  Line,
  XAxis,
  YAxis,
  Tooltip,
  Legend,
  CartesianGrid,
} from "recharts";
import { authFetch } from "../auth";

// 1. Accept timeFilter as a prop from Dashboard.jsx
export default function SentimentChart({ timeFilter }) {
  const [chartData, setChartData] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  // 2. Fetch new data when the prop changes
  useEffect(() => {
    // Flags a re-fetch in progress (already true on mount; genuinely needed when
    // timeFilter changes) - harmless for this pattern, so silenced deliberately.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLoading(true);
    authFetch(`/api/analytics/sentiment-trend?time_range=${timeFilter}`)
      .then((res) => {
        if (!res.ok) throw new Error(`Request failed with status ${res.status}`);
        return res.json();
      })
      .then((data) => {
        setChartData(data);
        setError(null);
        setLoading(false);
      })
      .catch((err) => {
        console.error("Error fetching sentiment trend:", err);
        setError("Couldn't load sentiment trend data.");
        setLoading(false);
      });
  }, [timeFilter]);

  const chartMargin = { top: 10, right: 10, left: -20, bottom: 0 };
  const tooltipStyle = { backgroundColor: "#ffffff", borderRadius: "8px", border: "1px solid #f3f4f6" };
  const dotStyle = { r: 4 };

  return (
    <div className="bg-white p-6 rounded-xl border border-gray-100 shadow-sm">
      <h2 className="text-lg font-bold text-gray-900 mb-6">Sentiment Over Time</h2>
      
      <div className="h-72 w-full">
        {loading ? (
          <div className="h-full w-full flex items-center justify-center text-gray-400">
            Updating chart data...
          </div>
        ) : error ? (
          <div className="h-full w-full flex items-center justify-center text-red-500 text-sm">
            {error}
          </div>
        ) : (
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={chartData} margin={chartMargin}>
              <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="#f3f4f6" />
              <XAxis dataKey="date" stroke="#9ca3af" fontSize={12} tickLine={false} />
              <YAxis stroke="#9ca3af" fontSize={12} tickLine={false} allowDecimals={false} />
              <Tooltip contentStyle={tooltipStyle} />
              <Legend />
              <Line type="monotone" dataKey="Positive" stroke="#10b981" strokeWidth={3} dot={dotStyle} />
              <Line type="monotone" dataKey="Negative" stroke="#f43f5e" strokeWidth={3} dot={dotStyle} />
              <Line type="monotone" dataKey="Neutral" stroke="#6b7280" strokeWidth={3} dot={dotStyle} />
            </LineChart>
          </ResponsiveContainer>
        )}
      </div>
    </div>
  );
}