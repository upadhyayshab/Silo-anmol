import { useState, useEffect } from "react";
import { MessageSquare, CheckCircle, Clock, Filter, ExternalLink, Trash2 } from "lucide-react";
import { authFetch } from "../auth";

export default function MentionsFeed() {
  const [mentions, setMentions] = useState([]);
  const [loading, setLoading] = useState(true);
  const [feedError, setFeedError] = useState(null);
  const [replyingTo, setReplyingTo] = useState(null);
  const [replyText, setReplyText] = useState("");
  const [toast, setToast] = useState(null); // { type: "success" | "error", message: string }
  const [pendingDeleteId, setPendingDeleteId] = useState(null);

  // --- New Google Maps store listing form ---
  const [newStoreName, setNewStoreName] = useState("");
  const [newStoreAddress, setNewStoreAddress] = useState("");
  const [newStorePhone, setNewStorePhone] = useState("");
  const [newStoreCategory, setNewStoreCategory] = useState("");
  const [newStoreLat, setNewStoreLat] = useState("");
  const [newStoreLng, setNewStoreLng] = useState("");
  const [creatingStore, setCreatingStore] = useState(false);

  const showToast = (type, message) => {
    setToast({ type, message });
    setTimeout(() => setToast(null), 4000);
  };

  // --- Filter States ---
  const [source, setSource] = useState("All");
  const [timeRange, setTimeRange] = useState("All");
  const [sentiment, setSentiment] = useState("All");
  const [status, setStatus] = useState("All");

  // --- Date Formatter Helper (UTC to Local Browser Time) ---
  const formatLocalDate = (dateString) => {
    if (!dateString) return "";
    
    const utcDateString = 
      typeof dateString === "string" && !dateString.endsWith("Z") && !dateString.includes("+")
        ? `${dateString}Z`
        : dateString;

    return new Date(utcDateString).toLocaleString("en-US", {
      month: "short",
      day: "numeric",
      year: "numeric",
      hour: "numeric",
      minute: "2-digit",
      hour12: true,
    });
  };

  const fetchMentions = () => {
    setLoading(true);
    // Combines all 4 filters into query parameters
    authFetch(`/api/mentions/recent?source=${encodeURIComponent(source)}&time_range=${timeRange}&sentiment=${sentiment}&status=${status}`)
      .then(res => {
        if (!res.ok) throw new Error(`Request failed with status ${res.status}`);
        return res.json();
      })
      .then(data => {
        setMentions(data);
        setFeedError(null);
        setLoading(false);
      })
      .catch(err => {
        console.error("Error fetching mentions:", err);
        setFeedError("Couldn't load the mentions feed.");
        setLoading(false);
      });
  };

  // Re-fetch whenever ANY dropdown filter changes. fetchMentions() sets loading
  // state (already true on mount; genuinely needed when a filter changes) -
  // harmless for this pattern, so silenced deliberately.
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    fetchMentions();
  }, [source, timeRange, sentiment, status]);

  // --- HANDLE REPLY ---
  const handleSendReply = async (mentionId) => {
    if (!replyText.trim()) return;

    try {
      const res = await authFetch(`/api/mentions/${mentionId}/reply`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reply_text: replyText }),
      });
      
      const data = await res.json();

      if (res.ok) {
        showToast("success", "Success! Reply posted successfully.");
        setReplyingTo(null);
        setReplyText("");
        fetchMentions();
      } else {
        showToast("error", "Failed to reply: " + (data.detail || data.message));
      }
    } catch (error) {
      showToast("error", "Error sending reply.");
      console.error(error);
    }
  };

  // --- HANDLE LIVE DELETE ---
  const handleDeleteMention = (mentionId) => {
    setPendingDeleteId(mentionId);
  };

  const cancelDelete = () => setPendingDeleteId(null);

  const confirmDeleteMention = async () => {
    const mentionId = pendingDeleteId;
    setPendingDeleteId(null);
    if (!mentionId) return;

    try {
      // encodeURIComponent handles special characters like slashes in IDs
      const res = await authFetch(`/api/mentions/${encodeURIComponent(mentionId)}`, {
        method: "DELETE",
      });

      const data = await res.json();

      if (res.ok) {
        showToast("success", "Mention deleted live and removed from dashboard!");
        fetchMentions();
      } else {
        showToast("error", "Deletion Failed: " + (data.detail || data.message));
      }
    } catch (error) {
      console.error("Delete Error:", error);
      showToast("error", "Error deleting mention. Make sure your FastAPI backend server is running and restarted!");
    }
  };

  // --- HANDLE NEW GOOGLE MAPS STORE LISTING ---
  const handleCreateStore = async () => {
    if (!newStoreName.trim() || !newStoreAddress.trim()) return;

    // Match the backend: latitude/longitude must be provided together, or not at all.
    if (!!newStoreLat.trim() !== !!newStoreLng.trim()) {
      showToast("error", "Provide both latitude and longitude, or leave both blank.");
      return;
    }

    setCreatingStore(true);
    try {
      const res = await authFetch("/api/google-maps/stores", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: newStoreName,
          address: newStoreAddress,
          phone: newStorePhone,
          category: newStoreCategory,
          latitude: newStoreLat.trim() ? parseFloat(newStoreLat) : null,
          longitude: newStoreLng.trim() ? parseFloat(newStoreLng) : null,
        }),
      });

      const data = await res.json();

      if (res.ok) {
        showToast("success", data.message || "Store listing created!");
        setNewStoreName("");
        setNewStoreAddress("");
        setNewStorePhone("");
        setNewStoreCategory("");
        setNewStoreLat("");
        setNewStoreLng("");
      } else {
        showToast("error", "Failed to create store: " + (data.detail || data.message));
      }
    } catch (error) {
      showToast("error", "Error creating store listing.");
      console.error(error);
    } finally {
      setCreatingStore(false);
    }
  };

  // Filter out child replies AND developer entries so only customer mentions show as top-level cards
  const topLevelMentions = mentions.filter((m) => !m.parent_id && m.author !== "Developer");

  return (
    <div className="bg-white rounded-xl shadow-sm border border-gray-100 p-6 mt-8">

      {/* TOAST NOTIFICATION */}
      {toast && (
        <div
          className={`fixed top-4 right-4 z-50 max-w-sm px-4 py-3 rounded-lg shadow-lg border text-sm font-medium ${
            toast.type === "success"
              ? "bg-emerald-50 border-emerald-200 text-emerald-800"
              : "bg-red-50 border-red-200 text-red-800"
          }`}
          role="status"
        >
          {toast.message}
        </div>
      )}

      {/* DELETE CONFIRMATION MODAL */}
      {pendingDeleteId && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
          <div className="bg-white rounded-xl shadow-xl border border-gray-100 p-6 max-w-sm w-full">
            <h3 className="text-base font-bold text-gray-900 mb-2">Delete this mention?</h3>
            <p className="text-sm text-gray-600 mb-5">
              This action will attempt to remove it live from social media and delete it from your dashboard.
            </p>
            <div className="flex justify-end gap-3">
              <button
                onClick={cancelDelete}
                className="bg-gray-100 text-gray-600 px-4 py-2 rounded-md text-sm font-medium hover:bg-gray-200 transition-colors"
              >
                Cancel
              </button>
              <button
                onClick={confirmDeleteMention}
                className="bg-rose-600 text-white px-4 py-2 rounded-md text-sm font-medium hover:bg-rose-700 transition-colors"
              >
                Delete
              </button>
            </div>
          </div>
        </div>
      )}

      {/* HEADER & FILTER BAR */}
      <div className="flex flex-col md:flex-row md:justify-between md:items-center mb-6 gap-4">
        <h2 className="text-lg font-bold text-gray-900">Live Mentions Feed</h2>
        
        <div className="flex flex-wrap gap-3 items-center bg-gray-50 p-2 rounded-lg border border-gray-100">
          <Filter size={16} className="text-gray-400 ml-2" aria-hidden="true" />

          {/* SOURCE FILTER */}
          <select
            value={source}
            onChange={(e) => setSource(e.target.value)}
            aria-label="Filter by source"
            className="bg-white border border-gray-200 text-sm rounded-md px-3 py-1.5 outline-none focus:ring-1 focus:ring-blue-500"
          >
            <option value="All">All Sources</option>
            <option value="YouTube">YouTube</option>
            <option value="Facebook">Facebook</option>
            <option value="Facebook DM">Facebook DM</option>
            <option value="Instagram">Instagram</option>
            <option value="Instagram DM">Instagram DM</option>
            <option value="PlayStore">PlayStore</option>
            <option value="News">News</option>
            <option value="Google Maps">Google Maps</option>
          </select>

          {/* TIME RANGE FILTER */}
          <select
            value={timeRange}
            onChange={(e) => setTimeRange(e.target.value)}
            aria-label="Filter by time range"
            className="bg-white border border-gray-200 text-sm rounded-md px-3 py-1.5 outline-none focus:ring-1 focus:ring-blue-500"
          >
            <option value="All">All Time</option>
            <option value="1h">Last 1 Hour</option>
            <option value="1d">Last 24 Hours</option>
            <option value="7d">Last 7 Days</option>
            <option value="30d">Last 30 Days</option>
          </select>

          {/* SENTIMENT FILTER */}
          <select
            value={sentiment}
            onChange={(e) => setSentiment(e.target.value)}
            aria-label="Filter by sentiment"
            className="bg-white border border-gray-200 text-sm rounded-md px-3 py-1.5 outline-none focus:ring-1 focus:ring-blue-500"
          >
            <option value="All">All Sentiments</option>
            <option value="Positive">Positive</option>
            <option value="Negative">Negative</option>
            <option value="Neutral">Neutral</option>
          </select>

          {/* STATUS FILTER */}
          <select
            value={status}
            onChange={(e) => setStatus(e.target.value)}
            aria-label="Filter by status"
            className="bg-white border border-gray-200 text-sm rounded-md px-3 py-1.5 outline-none focus:ring-1 focus:ring-blue-500"
          >
            <option value="All">All Statuses</option>
            <option value="Unanswered">Unanswered</option>
            <option value="Responded">Responded</option>
          </select>
        </div>
      </div>

      {/* ADD A NEW GOOGLE MAPS STORE LISTING - only shown while filtering by Google Maps */}
      {source === "Google Maps" && (
        <div className="mb-6 p-4 bg-blue-50 border border-blue-100 rounded-lg">
          <h3 className="text-sm font-bold text-gray-900 mb-3">Add a new store listing</h3>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3 mb-3">
            <input
              type="text"
              value={newStoreName}
              onChange={(e) => setNewStoreName(e.target.value)}
              placeholder="Store name (required)"
              aria-label="New store name"
              className="border border-gray-200 rounded-md p-2 text-sm outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500"
            />
            <input
              type="text"
              value={newStoreAddress}
              onChange={(e) => setNewStoreAddress(e.target.value)}
              placeholder="Address (required)"
              aria-label="New store address"
              className="border border-gray-200 rounded-md p-2 text-sm outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500"
            />
            <input
              type="text"
              value={newStorePhone}
              onChange={(e) => setNewStorePhone(e.target.value)}
              placeholder="Phone (optional)"
              aria-label="New store phone"
              className="border border-gray-200 rounded-md p-2 text-sm outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500"
            />
            <input
              type="text"
              value={newStoreCategory}
              onChange={(e) => setNewStoreCategory(e.target.value)}
              placeholder="Category (optional)"
              aria-label="New store category"
              className="border border-gray-200 rounded-md p-2 text-sm outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500"
            />
            <input
              type="text"
              inputMode="decimal"
              value={newStoreLat}
              onChange={(e) => setNewStoreLat(e.target.value)}
              placeholder="Latitude (optional)"
              aria-label="New store latitude"
              className="border border-gray-200 rounded-md p-2 text-sm outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500"
            />
            <input
              type="text"
              inputMode="decimal"
              value={newStoreLng}
              onChange={(e) => setNewStoreLng(e.target.value)}
              placeholder="Longitude (optional)"
              aria-label="New store longitude"
              className="border border-gray-200 rounded-md p-2 text-sm outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500"
            />
          </div>
          <p className="text-xs text-gray-500 mb-3">
            Leave latitude/longitude blank to let Google auto-detect the location from the address, or provide both for a precise pin.
          </p>
          <button
            onClick={handleCreateStore}
            disabled={creatingStore || !newStoreName.trim() || !newStoreAddress.trim()}
            className="bg-blue-600 text-white px-4 py-2 rounded-md text-sm font-medium hover:bg-blue-700 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {creatingStore ? "Creating..." : "Create Listing"}
          </button>
        </div>
      )}

      {/* MENTIONS LIST */}
      <div className="flex flex-col gap-4 max-h-[600px] overflow-y-auto pr-2">
        {loading ? (
           <p className="text-gray-500 text-center py-8">Loading feed...</p>
        ) : feedError ? (
           <p className="text-red-500 text-center py-8">{feedError}</p>
        ) : topLevelMentions.length === 0 ? (
          <p className="text-gray-500 text-center py-8">No mentions found for these filters.</p>
        ) : (
          topLevelMentions.map((mention) => {
            const childReplies = mentions.filter((m) => m.parent_id === mention.id);
            const isResponded = mention.status === "Responded" || childReplies.length > 0;
            
            // Must match backend/models.py's MentionSource.REPLYABLE (News has no reply capability).
            const mentionSource = (mention.source || "").toLowerCase();
            const canReply = [
              "youtube", "playstore", "facebook", "facebook dm", "instagram", "instagram dm", "google maps"
            ].includes(mentionSource);

            return (
              <div key={mention.id} className="border border-gray-100 rounded-lg p-4 hover:bg-gray-50 transition-colors">
                
                <div className="flex justify-between items-start mb-2">
                  <div className="flex items-center gap-2">
                    <span className="text-xs font-semibold px-2 py-1 bg-gray-100 text-gray-600 rounded uppercase tracking-wider">
                      {mention.source}
                    </span>
                    {mention.link && (
                      <a 
                        href={mention.link} 
                        target="_blank" 
                        rel="noopener noreferrer"
                        className="flex items-center gap-1 text-[10px] font-semibold text-blue-600 bg-blue-50 px-2 py-1 rounded hover:bg-blue-100 transition-colors"
                        title="View Original Source"
                      >
                        <ExternalLink size={12} />
                        View Source
                      </a>
                    )}
                  </div>
                  
                  <div className="flex flex-col items-end gap-1">
                    <span className={`flex items-center text-xs font-medium px-2 py-1 rounded ${
                      isResponded ? 'bg-emerald-100 text-emerald-700' : 'bg-amber-100 text-amber-700'
                    }`}>
                      {isResponded ? <CheckCircle size={14} className="mr-1"/> : <Clock size={14} className="mr-1"/>}
                      {isResponded ? 'Responded' : 'Unanswered'}
                    </span>
                    
                    {mention.date && (
                      <span className="text-[10px] text-gray-500 font-medium">
                        {formatLocalDate(mention.date)}
                      </span>
                    )}
                  </div>
                </div>
                
                <p className="text-sm font-bold text-gray-900 mb-1">{mention.author}</p>
                <p className="text-sm text-gray-800 mb-4">{mention.content}</p>
                
                {/* AI ANALYSIS BOX */}
                {(mention.label || mention.explanation) && (
                  <div className="mb-4 bg-purple-50 p-3 rounded-md border border-purple-100">
                    {mention.label && (
                      <p className="text-xs font-bold text-purple-700 uppercase tracking-wider mb-1">
                        AI Label: {mention.label}
                      </p>
                    )}
                    {mention.explanation && (
                      <p className="text-sm text-purple-900">
                        {mention.explanation}
                      </p>
                    )}
                  </div>
                )}

                {/* NESTED DEVELOPER/CHILD REPLIES */}
                {childReplies.length > 0 && (
                  <div className="mb-4 space-y-2">
                    {childReplies.map((reply) => (
                      <div key={reply.id} className="bg-blue-50/70 p-3 rounded-lg border border-blue-100 ml-4">
                        <div className="flex justify-between items-center mb-1">
                          <span className="text-xs font-bold text-blue-900 flex items-center gap-1">
                            💬 {reply.author} Response
                          </span>
                          {reply.date && (
                            <span className="text-[10px] text-gray-500">
                              {formatLocalDate(reply.date)}
                            </span>
                          )}
                        </div>
                        <p className="text-xs text-gray-800">{reply.content}</p>
                      </div>
                    ))}
                  </div>
                )}
                
                {/* FOOTER ACTIONS */}
                <div className="flex justify-between items-center mt-2 pt-2 border-t border-gray-100">
                  <span className={`text-xs font-medium px-2 py-0.5 rounded ${
                    mention.sentiment === 'Positive' ? 'text-emerald-600 bg-emerald-50' :
                    mention.sentiment === 'Negative' ? 'text-rose-600 bg-rose-50' :
                    'text-gray-600 bg-gray-50'
                  }`}>
                    {mention.sentiment}
                  </span>
                  
                  <div className="flex items-center gap-3">
                    {!isResponded && canReply && (
                      <button 
                        onClick={() => {
                          setReplyingTo(mention.id);
                          setReplyText("");
                        }}
                        className="text-xs font-medium text-blue-600 hover:text-blue-800 flex items-center"
                      >
                        <MessageSquare size={14} className="mr-1" />
                        Reply
                      </button>
                    )}

                    {/* DELETE BUTTON */}
                    <button 
                      onClick={() => handleDeleteMention(mention.id)}
                      className="text-xs font-medium text-rose-600 hover:text-rose-800 flex items-center transition-colors"
                      title="Delete Comment"
                    >
                      <Trash2 size={14} className="mr-1" />
                      Delete
                    </button>
                  </div>
                </div>

                {/* INLINE REPLY INPUT BOX */}
                {replyingTo === mention.id && (
                  <div className="mt-4 pt-4 border-t border-gray-100 flex gap-2">
                    <input 
                      type="text"
                      value={replyText}
                      onChange={(e) => setReplyText(e.target.value)}
                      placeholder="Type your official reply..."
                      aria-label="Reply text"
                      className="flex-1 border border-gray-200 rounded-md p-2 text-sm outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500"
                    />
                    <button 
                      onClick={() => handleSendReply(mention.id)}
                      className="bg-blue-600 text-white px-4 py-2 rounded-md text-sm font-medium hover:bg-blue-700 transition-colors"
                    >
                      Send
                    </button>
                    <button 
                      onClick={() => setReplyingTo(null)}
                      className="bg-gray-100 text-gray-600 px-3 py-2 rounded-md text-sm font-medium hover:bg-gray-200 transition-colors"
                    >
                      Cancel
                    </button>
                  </div>
                )}
                
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}