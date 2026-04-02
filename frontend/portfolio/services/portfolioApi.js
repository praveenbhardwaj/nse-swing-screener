/**
 * portfolioApi.js — HTTP client for the portfolio module.
 *
 * All methods send JSON requests to the Flask backend at the proxy URL
 * configured in <meta name="proxy-url"> (falls back to localhost:5001).
 * Every request has a 12-second timeout and throws on non-OK responses.
 *
 * Mirrors the Flask routes defined in groww_proxy.py:
 *   /portfolio/recommendations  — analyst buy signals (CRUD)
 *   /portfolio/positions        — actual holdings (CRUD, includes live LTP)
 *   /portfolio/history          — append-only audit log
 *   /portfolio/analytics        — KPI summary (P&L, win rate)
 */

/** Maximum wait time per API request before aborting. */
const DEFAULT_TIMEOUT_MS = 12000;

/**
 * Read the backend proxy URL from the HTML meta tag.
 * Allows frontend (Netlify) and backend (Render) to be on different domains
 * without hardcoding URLs in JavaScript.
 * @returns {string} Base URL of the Flask backend.
 */
function getProxy() {
  return (
    document.querySelector('meta[name="proxy-url"]')?.content ||
    "http://localhost:5001"
  );
}

/**
 * Shared fetch wrapper with timeout, JSON body, and error extraction.
 * @param {string} path - API path (e.g. "/portfolio/recommendations")
 * @param {RequestInit} [options] - Fetch options (method, body, headers, etc.)
 * @returns {Promise<object>} Parsed JSON response from the server.
 * @throws {Error} If the request fails, times out, or server returns ok=false.
 */
async function request(path, options = {}) {
  const proxy = getProxy();
  const res = await fetch(`${proxy}${path}`, {
    ...options,
    signal: AbortSignal.timeout(DEFAULT_TIMEOUT_MS),
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
  });
  const data = await res.json();
  // Surface server-side errors (Flask returns {ok: false, error: "..."})
  if (!res.ok || data.ok === false) {
    throw new Error(data.error || "Request failed");
  }
  return data;
}

/** API methods used by the portfolio module components and index.js. */
export const portfolioApi = {
  /** Fetch all recommendations ordered by created_at DESC. */
  getRecommendations() {
    return request("/portfolio/recommendations");
  },

  /**
   * Create a new recommendation.
   * @param {{symbol, rating, target_price, stop_loss, rationale, status}} payload
   */
  createRecommendation(payload) {
    return request("/portfolio/recommendations", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  /**
   * Update an existing recommendation by ID.
   * @param {string} id - UUID of the recommendation row.
   * @param {object} payload - Fields to update (partial update supported).
   */
  updateRecommendation(id, payload) {
    return request(`/portfolio/recommendations/${id}`, {
      method: "PUT",
      body: JSON.stringify(payload),
    });
  },

  /**
   * Delete a recommendation by ID.
   * @param {string} id - UUID of the recommendation row.
   */
  deleteRecommendation(id) {
    return request(`/portfolio/recommendations/${id}`, { method: "DELETE" });
  },

  /**
   * Fetch all positions. Active positions include live LTP from the backend
   * (Flask calls _fetch_ltp_batch internally before returning).
   */
  getPositions() {
    return request("/portfolio/positions");
  },

  /**
   * Create a new portfolio position.
   * @param {{symbol, qty, entry_price, current_price, status, notes}} payload
   */
  createPosition(payload) {
    return request("/portfolio/positions", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  /**
   * Update an existing position by ID.
   * Setting status="closed" automatically records closed_at on the server.
   * @param {string} id - UUID of the position row.
   * @param {object} payload - Fields to update.
   */
  updatePosition(id, payload) {
    return request(`/portfolio/positions/${id}`, {
      method: "PUT",
      body: JSON.stringify(payload),
    });
  },

  /**
   * Delete a position by ID.
   * @param {string} id - UUID of the position row.
   */
  deletePosition(id) {
    return request(`/portfolio/positions/${id}`, { method: "DELETE" });
  },

  /** Fetch the portfolio event audit log (all create/update/close/delete events). */
  getHistory() {
    return request("/portfolio/history");
  },

  /**
   * Fetch portfolio analytics KPIs:
   * active_positions, total_invested, current_value,
   * unrealized_pnl, realized_pnl, win_rate.
   */
  getAnalytics() {
    return request("/portfolio/analytics");
  },
};
