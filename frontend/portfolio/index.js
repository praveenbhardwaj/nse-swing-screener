import { portfolioApi } from "./services/portfolioApi.js";
import { createPortfolioStore } from "./state/portfolioStore.js";
import { renderRecommendationsTable } from "./components/RecommendationsTable.js";
import { renderActivePositionsTable } from "./components/ActivePositionsTable.js";
import { renderPortfolioAnalyticsCards } from "./components/PortfolioAnalyticsCards.js";
import { renderHistoryTimeline } from "./components/HistoryTimeline.js";

const store = createPortfolioStore();

function promptRecommendation(existing) {
  const symbol = prompt("Symbol (e.g. TCS)", existing?.symbol || "");
  if (!symbol) return null;
  const rating = prompt("Rating (buy / strong_buy)", existing?.rating || "buy");
  const targetPrice = Number(prompt("Target price", existing?.target_price || "0"));
  const stopLoss = Number(prompt("Stop loss", existing?.stop_loss || "0"));
  const rationale = prompt("Rationale", existing?.rationale || "");
  return {
    symbol: symbol.toUpperCase(),
    rating: String(rating || "buy").toLowerCase(),
    target_price: targetPrice,
    stop_loss: stopLoss,
    rationale,
    status: existing?.status || "active",
  };
}

function promptPosition(existing) {
  const symbol = prompt("Symbol (e.g. INFY)", existing?.symbol || "");
  if (!symbol) return null;
  const qty = Number(prompt("Quantity", existing?.qty || "0"));
  const entryPrice = Number(prompt("Entry price", existing?.entry_price || "0"));
  const currentPrice = Number(prompt("Current price", existing?.current_price || existing?.entry_price || "0"));
  const status = prompt("Status (active / closed)", existing?.status || "active");
  return {
    symbol: symbol.toUpperCase(),
    qty,
    entry_price: entryPrice,
    current_price: currentPrice,
    status: String(status || "active").toLowerCase(),
  };
}

async function refreshAll() {
  store.set({ loading: true, error: "" });
  try {
    const [recs, positions, history, analytics] = await Promise.all([
      portfolioApi.getRecommendations(),
      portfolioApi.getPositions(),
      portfolioApi.getHistory(),
      portfolioApi.getAnalytics(),
    ]);
    store.set({
      loading: false,
      recommendations: recs.recommendations || [],
      positions: (positions.positions || []).filter((p) => p.status === "active"),
      history: history.history || [],
      analytics: analytics.analytics || null,
    });
  } catch (e) {
    store.set({ loading: false, error: e.message || "Failed to load portfolio module." });
  }
}

function mount(root) {
  root.innerHTML = `
    <div class="module-grid">
      <div class="tcard" style="padding:14px"><div id="portfolioAnalyticsCards"></div></div>
      <div class="tcard" style="padding:14px"><div id="portfolioHistoryTimeline"></div></div>
    </div>
    <div class="tcard" style="padding:14px;margin-top:14px"><div id="portfolioRecommendationsTable"></div></div>
    <div class="tcard" style="padding:14px;margin-top:14px"><div id="portfolioActivePositionsTable"></div></div>
    <div id="portfolioModuleError" class="mini-note" style="margin-top:10px;color:var(--red)"></div>
  `;

  const recEl = root.querySelector("#portfolioRecommendationsTable");
  const posEl = root.querySelector("#portfolioActivePositionsTable");
  const analyticsEl = root.querySelector("#portfolioAnalyticsCards");
  const historyEl = root.querySelector("#portfolioHistoryTimeline");
  const errorEl = root.querySelector("#portfolioModuleError");

  store.subscribe((state) => {
    errorEl.textContent = state.error || "";
    renderPortfolioAnalyticsCards({ targetEl: analyticsEl, analytics: state.analytics });
    renderHistoryTimeline({ targetEl: historyEl, history: state.history });
    renderRecommendationsTable({
      targetEl: recEl,
      recommendations: state.recommendations,
      onCreate: async () => {
        const payload = promptRecommendation();
        if (!payload) return;
        await portfolioApi.createRecommendation(payload);
        await refreshAll();
      },
      onEdit: async (id) => {
        const row = state.recommendations.find((r) => String(r.id) === String(id));
        if (!row) return;
        const payload = promptRecommendation(row);
        if (!payload) return;
        await portfolioApi.updateRecommendation(id, payload);
        await refreshAll();
      },
      onDelete: async (id) => {
        if (!confirm("Delete this recommendation?")) return;
        await portfolioApi.deleteRecommendation(id);
        await refreshAll();
      },
    });
    renderActivePositionsTable({
      targetEl: posEl,
      positions: state.positions,
      onCreate: async () => {
        const payload = promptPosition();
        if (!payload) return;
        await portfolioApi.createPosition(payload);
        await refreshAll();
      },
      onEdit: async (id) => {
        const row = state.positions.find((p) => String(p.id) === String(id));
        if (!row) return;
        const payload = promptPosition(row);
        if (!payload) return;
        await portfolioApi.updatePosition(id, payload);
        await refreshAll();
      },
      onDelete: async (id) => {
        if (!confirm("Delete this position?")) return;
        await portfolioApi.deletePosition(id);
        await refreshAll();
      },
    });
  });

  refreshAll();
}

window.initPortfolioModule = function initPortfolioModule() {
  const root = document.getElementById("portfolioModuleRoot");
  if (!root) return;
  mount(root);
};
