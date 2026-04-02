/**
 * PortfolioAnalyticsCards.js — KPI summary card grid for the portfolio module.
 *
 * Renders six summary cards from the /portfolio/analytics endpoint:
 *   Active Positions  — count of open positions
 *   Total Invested    — sum of (qty × entry_price) for active positions
 *   Current Value     — sum of (qty × current_price) for active positions
 *   Unrealized P/L    — Current Value - Total Invested (green/red coloured)
 *   Realized P/L      — sum of closed position gains/losses (green/red coloured)
 *   Win Rate          — % of closed positions that were profitable
 *
 * Props:
 *   targetEl  — DOM element to render into
 *   analytics — analytics object from GET /portfolio/analytics, or null
 */

/**
 * Format a number as Indian Rupees.
 * @param {number|string} v - Numeric value.
 * @returns {string} ₹-prefixed formatted string.
 */
function fmtMoney(v) {
  return `₹${Number(v || 0).toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;
}

/**
 * Render the analytics KPI card grid into targetEl.
 * Defaults all values to 0 / 0% when analytics is null (before first fetch).
 * P&L values are coloured green (≥0) or red (<0).
 */
export function renderPortfolioAnalyticsCards({ targetEl, analytics }) {
  const a = analytics || {};
  const unrealizedColor = Number(a.unrealized_pnl || 0) >= 0 ? "var(--green)" : "var(--red)";
  const realizedColor = Number(a.realized_pnl || 0) >= 0 ? "var(--green)" : "var(--red)";
  targetEl.innerHTML = `
    <div class="kpi-grid">
      <div class="kpi-card"><div class="kpi-lbl">Active Positions</div><div class="kpi-val">${a.active_positions || 0}</div></div>
      <div class="kpi-card"><div class="kpi-lbl">Total Invested</div><div class="kpi-val">${fmtMoney(a.total_invested)}</div></div>
      <div class="kpi-card"><div class="kpi-lbl">Current Value</div><div class="kpi-val">${fmtMoney(a.current_value)}</div></div>
      <div class="kpi-card"><div class="kpi-lbl">Unrealized P/L</div><div class="kpi-val" style="color:${unrealizedColor}">${fmtMoney(a.unrealized_pnl)}</div></div>
      <div class="kpi-card"><div class="kpi-lbl">Realized P/L</div><div class="kpi-val" style="color:${realizedColor}">${fmtMoney(a.realized_pnl)}</div></div>
      <div class="kpi-card"><div class="kpi-lbl">Win Rate</div><div class="kpi-val">${Number(a.win_rate || 0).toFixed(1)}%</div></div>
    </div>
  `;
}
