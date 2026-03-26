function fmtMoney(v) {
  return `₹${Number(v || 0).toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;
}

export function renderActivePositionsTable({ targetEl, positions, onCreate, onEdit, onDelete }) {
  targetEl.innerHTML = `
    <div class="tbar" style="padding:0 0 10px 0;border:none">
      <div class="tcount">Active Positions (Live P/L)</div>
      <button class="btn btn-g" data-action="new-pos">+ Add Position</button>
    </div>
    ${
      positions.length
        ? `<div class="tscroll"><table>
            <thead>
              <tr><th>Symbol</th><th>Qty</th><th>Entry</th><th>Current</th><th>Invested</th><th>Value</th><th>P/L</th><th>Actions</th></tr>
            </thead>
            <tbody>
              ${positions
                .map((p) => {
                  const invested = Number(p.qty || 0) * Number(p.entry_price || 0);
                  const value = Number(p.qty || 0) * Number(p.current_price || p.entry_price || 0);
                  const pnl = value - invested;
                  const pnlPct = invested ? (pnl / invested) * 100 : 0;
                  const pnlColor = pnl >= 0 ? "var(--green)" : "var(--red)";
                  return `<tr>
                    <td><strong>${p.symbol || "-"}</strong></td>
                    <td>${Number(p.qty || 0).toLocaleString("en-IN")}</td>
                    <td>${fmtMoney(p.entry_price)}</td>
                    <td>${fmtMoney(p.current_price || p.entry_price)}</td>
                    <td>${fmtMoney(invested)}</td>
                    <td>${fmtMoney(value)}</td>
                    <td style="color:${pnlColor}">${fmtMoney(pnl)} (${pnlPct > 0 ? "+" : ""}${pnlPct.toFixed(2)}%)</td>
                    <td>
                      <button class="btn btn-g" data-action="edit-pos" data-id="${p.id}">Edit</button>
                      <button class="btn btn-r" data-action="delete-pos" data-id="${p.id}">Delete</button>
                    </td>
                  </tr>`;
                })
                .join("")}
            </tbody>
          </table></div>`
        : '<div class="empty"><div class="empty-ico">📭</div><div>No active positions yet.</div></div>'
    }
  `;

  targetEl.querySelector('[data-action="new-pos"]')?.addEventListener("click", onCreate);
  targetEl.querySelectorAll('[data-action="edit-pos"]').forEach((btn) => {
    btn.addEventListener("click", () => onEdit(btn.dataset.id));
  });
  targetEl.querySelectorAll('[data-action="delete-pos"]').forEach((btn) => {
    btn.addEventListener("click", () => onDelete(btn.dataset.id));
  });
}
