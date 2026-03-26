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
