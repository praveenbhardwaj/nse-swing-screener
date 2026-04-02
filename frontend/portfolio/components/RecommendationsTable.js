/**
 * RecommendationsTable.js — Buy/Strong Buy recommendations UI component.
 *
 * Renders a table of analyst recommendations with CRUD action buttons.
 * Uses innerHTML for full re-render on each state change (simple but effective
 * for the small dataset this component handles).
 *
 * Props:
 *   targetEl        — DOM element to render into
 *   recommendations — array of recommendation rows from Supabase
 *   onCreate        — async callback for the "+ Add Recommendation" button
 *   onEdit(id)      — async callback when "Edit" is clicked (receives row UUID)
 *   onDelete(id)    — async callback when "Delete" is clicked (receives row UUID)
 */

/**
 * Format a number as Indian Rupees (e.g. ₹1,23,456.78).
 * @param {number|string} v - Numeric value to format.
 * @returns {string} Formatted currency string.
 */
function fmtMoney(v) {
  return `₹${Number(v || 0).toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;
}

/**
 * Render a coloured pill badge for the recommendation rating.
 * @param {string} rating - 'buy', 'strong_buy', or other.
 * @returns {string} HTML string for the badge element.
 */
function ratingBadge(rating) {
  const val = String(rating || "").toLowerCase();
  if (val === "strong_buy") return '<span class="pill pill-strong">STRONG BUY</span>';
  if (val === "buy") return '<span class="pill pill-buy">BUY</span>';
  return `<span class="pill">${(rating || "N/A").toUpperCase()}</span>`;
}

/**
 * Render the recommendations table into targetEl.
 * Shows an empty-state message when the recommendations array is empty.
 * Event listeners are re-attached on every render (innerHTML wipes old ones).
 */
export function renderRecommendationsTable({
  targetEl,
  recommendations,
  onCreate,
  onEdit,
  onDelete,
}) {
  targetEl.innerHTML = `
    <div class="tbar" style="padding:0 0 10px 0;border:none">
      <div class="tcount">Buy / Strong Buy Recommendations</div>
      <button class="btn btn-g" data-action="new-rec">+ Add Recommendation</button>
    </div>
    ${
      recommendations.length
        ? `<div class="tscroll"><table>
            <thead>
              <tr><th>Symbol</th><th>Rating</th><th>Target</th><th>S/L</th><th>Status</th><th>Reason</th><th>Actions</th></tr>
            </thead>
            <tbody>
              ${recommendations
                .map(
                  (r) => `<tr>
                    <td><strong>${r.symbol || "-"}</strong></td>
                    <td>${ratingBadge(r.rating)}</td>
                    <td>${fmtMoney(r.target_price)}</td>
                    <td>${fmtMoney(r.stop_loss)}</td>
                    <td>${(r.status || "active").toUpperCase()}</td>
                    <td>${r.rationale || "-"}</td>
                    <td>
                      <button class="btn btn-g" data-action="edit-rec" data-id="${r.id}">Edit</button>
                      <button class="btn btn-r" data-action="delete-rec" data-id="${r.id}">Delete</button>
                    </td>
                  </tr>`
                )
                .join("")}
            </tbody>
          </table></div>`
        : '<div class="empty"><div class="empty-ico">📭</div><div>No recommendations yet.</div></div>'
    }
  `;

  targetEl.querySelector('[data-action="new-rec"]')?.addEventListener("click", onCreate);
  targetEl.querySelectorAll('[data-action="edit-rec"]').forEach((btn) => {
    btn.addEventListener("click", () => onEdit(btn.dataset.id));
  });
  targetEl.querySelectorAll('[data-action="delete-rec"]').forEach((btn) => {
    btn.addEventListener("click", () => onDelete(btn.dataset.id));
  });
}
