/**
 * HistoryTimeline.js — Portfolio event audit log timeline component.
 *
 * Renders a table of portfolio_events rows in reverse-chronological order.
 * Each event records a create/update/close/delete action on a position,
 * including the symbol, event type, and a human-readable message.
 *
 * Props:
 *   targetEl — DOM element to render into
 *   history  — array of portfolio_events rows from GET /portfolio/history
 *
 * Timestamps are formatted using the browser's locale (en-IN).
 * Shows an empty-state message when no history has been recorded.
 */
export function renderHistoryTimeline({ targetEl, history }) {
  const items = history || [];
  targetEl.innerHTML = items.length
    ? `<div class="tscroll"><table>
        <thead><tr><th>Time</th><th>Type</th><th>Symbol</th><th>Details</th></tr></thead>
        <tbody>
          ${items
            .map(
              (h) => `<tr>
                <td>${h.created_at ? new Date(h.created_at).toLocaleString("en-IN") : "-"}</td>
                <td>${(h.event_type || "-").toUpperCase()}</td>
                <td>${h.symbol || "-"}</td>
                <td>${h.message || "-"}</td>
              </tr>`
            )
            .join("")}
        </tbody>
      </table></div>`
    : '<div class="empty"><div class="empty-ico">📜</div><div>No history recorded yet.</div></div>';
}
