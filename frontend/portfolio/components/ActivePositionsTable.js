/**
 * ActivePositionsTable.js — Active holdings table with live P&L calculation.
 *
 * Renders a table of active portfolio_positions rows. For each row, calculates:
 *   invested = qty × entry_price
 *   value    = qty × current_price  (current_price refreshed by Flask via LTP API)
 *   pnl      = value - invested
 *   pnl_pct  = pnl / invested × 100
 *
 * Props:
 *   targetEl  — DOM element to render into
 *   positions — array of active position rows (current_price already updated by API)
 *   onCreate  — async callback for the "+ Add Position" button
 *   onEdit(id)   — async callback for "Edit" button (receives row UUID)
 *   onDelete(id) — async callback for "Delete" button (receives row UUID)
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
 * Render the active positions table into targetEl.
 * P&L is colour-coded: green for profit, red for loss.
 * Shows empty-state message when no active positions exist.
 */
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
