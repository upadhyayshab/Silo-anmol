import { useState, useEffect } from "react";
import KPICard from "./components/KPICard";
import SentimentChart from "./components/SentimentChart";
import MentionsFeed from "./components/MentionsFeed";
import { MessageCircle, HeartPulse, AlertTriangle } from "lucide-react";
import { authFetch } from "./auth";

export default function Dashboard() {
  const [timeFilter, setTimeFilter] = useState("7d");
  const [kpis, setKpis] = useState({
    total_mentions: 0,
    net_sentiment: 0,
    critical_alerts: 0,
    mention_trend_pct: 0
  });
  const [kpiError, setKpiError] = useState(null);

  const timeLabels = {
    "All": "all time",
    "1h": "last 1 hour",
    "6h": "last 6 hours",
    "12h": "last 12 hours",
    "1d": "last 1 day",
    "2d": "last 2 days",
    "4d": "last 4 days",
    "7d": "last 7 days",
    "14d": "last 14 days",
    "30d": "last 30 days"
  };

  useEffect(() => {
    authFetch(`/api/analytics/kpis?time_range=${timeFilter}`)
      .then((res) => {
        if (!res.ok) throw new Error(`Request failed with status ${res.status}`);
        return res.json();
      })
      .then((data) => {
        if (data) {
          setKpis(data);
          setKpiError(null);
        }
      })
      .catch((err) => {
        console.error("Error fetching KPIs:", err);
        setKpiError("Couldn't load latest stats. Showing last known data.");
      });
  }, [timeFilter]);

  const currentLabel = timeLabels[timeFilter] || "selected period";
  
  // Safe extractions to prevent undefined crashes
  const totalMentions = (kpis?.total_mentions || 0).toLocaleString();
  const netSentiment = (kpis?.net_sentiment || 0) + "%";
  const criticalAlerts = (kpis?.critical_alerts || 0).toString();
  const trendPct = Math.abs(kpis?.mention_trend_pct || 0) + "%";

  return (
    <div className="p-8 bg-gray-50 min-h-screen">
      <div className="flex justify-between items-center mb-6">
        <h1 className="text-2xl font-bold text-gray-900">Social Listening Overview</h1>
        
        <select
          value={timeFilter}
          onChange={(e) => setTimeFilter(e.target.value)}
          aria-label="Filter dashboard stats by time range"
          className="bg-white border border-gray-200 text-gray-700 text-sm rounded-lg focus:ring-blue-500 focus:border-blue-500 block p-2.5 outline-none cursor-pointer shadow-sm"
        >
          <option value="All">All Time</option>
          <option value="1h">Last 1 Hour</option>
          <option value="6h">Last 6 Hours</option>
          <option value="12h">Last 12 Hours</option>
          <option value="1d">Last 1 Day</option>
          <option value="2d">Last 2 Days</option>
          <option value="4d">Last 4 Days</option>
          <option value="7d">Last 7 Days</option>
          <option value="14d">Last 14 Days</option>
          <option value="30d">Last 30 Days</option>
        </select>
      </div>

      {kpiError && (
        <div className="mb-6 p-3 rounded-lg bg-red-50 border border-red-200 text-red-700 text-sm">
          {kpiError}
        </div>
      )}

      <div className="grid grid-cols-1 md:grid-cols-3 gap-6 mb-8">
        <KPICard 
          title="Total Mentions" 
          value={totalMentions} 
          trend={(kpis?.mention_trend_pct || 0) >= 0 ? "up" : "down"} 
          trendValue={trendPct} 
          icon={MessageCircle}
          timeLabel={currentLabel} 
        />
        <KPICard 
          title="Net Sentiment" 
          value={netSentiment} 
          trend="up" 
          trendValue="Live" 
          icon={HeartPulse}
          timeLabel={currentLabel} 
        />
        <KPICard 
          title="Critical Alerts" 
          value={criticalAlerts} 
          trend={(kpis?.critical_alerts || 0) > 0 ? "up" : "down"} 
          trendValue={(kpis?.critical_alerts || 0) > 0 ? "Needs review" : "All clear"} 
          icon={AlertTriangle}
          timeLabel={currentLabel} 
          isAlert={true}
        />
      </div>

      <div className="flex flex-col gap-8">
        <SentimentChart timeFilter={timeFilter} />
        <MentionsFeed />
      </div>
    </div>
  );
}