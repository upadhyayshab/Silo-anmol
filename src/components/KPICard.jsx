import { TrendingUp, TrendingDown, Minus } from "lucide-react";

// Added isAlert to the properties
export default function KPICard({ title, value, trend, trendValue, icon: Icon, timeLabel, isAlert }) {
  
  const getTrendColor = (t) => {
    if (t === "up") {
      // If it's an alert, UP is Red. Otherwise, UP is Green.
      return isAlert ? "text-rose-700 bg-rose-50" : "text-emerald-700 bg-emerald-50";
    }
    if (t === "down") {
      // If it's an alert, DOWN is Green. Otherwise, DOWN is Red.
      return isAlert ? "text-emerald-700 bg-emerald-50" : "text-rose-700 bg-rose-50";
    }
    return "text-gray-700 bg-gray-50";
  };

  const trendClasses = "flex items-center px-2 py-0.5 rounded font-medium " + getTrendColor(trend);

  return (
    <div className="bg-white p-6 rounded-xl border border-gray-100 shadow-sm transition-all hover:shadow-md">
      <div className="flex justify-between items-start mb-4">
        <h3 className="text-sm font-semibold text-gray-500 uppercase tracking-wider">{title}</h3>
        <div className="p-2 bg-blue-50 rounded-lg">
          <Icon size={20} className="text-blue-600" />
        </div>
      </div>
      
      <div className="text-3xl font-bold text-gray-900 mb-2">{value}</div>
      
      <div className="flex items-center text-sm">
        <span className={trendClasses}>
          {trend === "up" && <TrendingUp size={14} className="mr-1" />}
          {trend === "down" && <TrendingDown size={14} className="mr-1" />}
          {trend === "neutral" && <Minus size={14} className="mr-1" />}
          {trendValue}
        </span>
        
        <span className="text-gray-400 ml-2">vs {timeLabel}</span>
      </div>
    </div>
  );
}