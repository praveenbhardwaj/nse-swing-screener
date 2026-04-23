-- =============================================================================
-- Add 'reason' column to active_trades and closed_trades
-- Migration: 20260423_add_reason_column
-- =============================================================================
-- Stores the screener rationale (why the stock was selected) so that
-- closed trades can be analyzed to understand what differentiates
-- successful vs failed trades.
-- =============================================================================

alter table public.active_trades
  add column if not exists reason text;

alter table public.closed_trades
  add column if not exists reason text;
