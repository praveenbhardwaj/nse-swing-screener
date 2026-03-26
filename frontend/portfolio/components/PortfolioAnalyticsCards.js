function fmtMoney(v) {
  return `₹${Number(v || 0).toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;
}

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
