const DEFAULT_TIMEOUT_MS = 12000;

function getProxy() {
  return (
    document.querySelector('meta[name="proxy-url"]')?.content ||
    "http://localhost:5001"
  );
}

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
  if (!res.ok || data.ok === false) {
    throw new Error(data.error || "Request failed");
  }
  return data;
}

export const portfolioApi = {
  getRecommendations() {
    return request("/portfolio/recommendations");
  },
  createRecommendation(payload) {
    return request("/portfolio/recommendations", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },
  updateRecommendation(id, payload) {
    return request(`/portfolio/recommendations/${id}`, {
      method: "PUT",
      body: JSON.stringify(payload),
    });
  },
  deleteRecommendation(id) {
    return request(`/portfolio/recommendations/${id}`, { method: "DELETE" });
  },
  getPositions() {
    return request("/portfolio/positions");
  },
  createPosition(payload) {
    return request("/portfolio/positions", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },
  updatePosition(id, payload) {
    return request(`/portfolio/positions/${id}`, {
      method: "PUT",
      body: JSON.stringify(payload),
    });
  },
  deletePosition(id) {
    return request(`/portfolio/positions/${id}`, { method: "DELETE" });
  },
  getHistory() {
    return request("/portfolio/history");
  },
  getAnalytics() {
    return request("/portfolio/analytics");
  },
};
