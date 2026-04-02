-- =============================================================================
-- Portfolio Module Schema
-- Migration: 20260326_001_portfolio_module
-- =============================================================================
-- Creates three tables for the portfolio management module:
--   1. recommendations  — analyst/screener buy signals tracked for reference
--   2. portfolio_positions — actual holdings entered manually by the user
--   3. portfolio_events    — append-only audit log for all position changes
--
-- These tables are separate from the 'trades' table which is managed by the
-- Flask screener. This module allows manual portfolio tracking alongside the
-- automated screener trade lifecycle.
-- =============================================================================

create extension if not exists "pgcrypto"; -- enables gen_random_uuid()

-- -----------------------------------------------------------------------------
-- Table: recommendations
-- Purpose: Store buy/strong_buy signals for tracking (manual or from screener).
--          Separate from 'trades' — recommendations are advisory; trades are
--          actual positions being tracked for T1/SL outcomes.
-- -----------------------------------------------------------------------------
create table if not exists public.recommendations (
  id uuid primary key default gen_random_uuid(),
  symbol text not null,
  rating text not null check (rating in ('buy', 'strong_buy')),
  target_price numeric(12, 2) not null default 0,
  stop_loss numeric(12, 2) not null default 0,
  status text not null default 'active',
  rationale text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

-- -----------------------------------------------------------------------------
-- Table: portfolio_positions
-- Purpose: Track actual holdings entered by the user. Unlike 'trades', these
--          are manually entered and support fractional quantities. The
--          current_price field is refreshed live via the /portfolio/positions
--          route (calls _fetch_ltp_batch internally).
--          closed_at is populated automatically when status changes to 'closed'.
-- -----------------------------------------------------------------------------
create table if not exists public.portfolio_positions (
  id uuid primary key default gen_random_uuid(),
  symbol text not null,
  qty numeric(14, 4) not null default 0,
  entry_price numeric(12, 2) not null default 0,
  current_price numeric(12, 2) not null default 0,
  status text not null default 'active' check (status in ('active', 'closed')),
  opened_at timestamptz not null default now(),
  closed_at timestamptz,
  notes text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

-- -----------------------------------------------------------------------------
-- Table: portfolio_events
-- Purpose: Append-only audit log. Every create/update/close/delete on a
--          portfolio_position writes a row here. The payload_json stores a
--          snapshot of the changed fields. Used by the History Timeline UI.
--          position_id FK is SET NULL on position delete so history is preserved.
-- -----------------------------------------------------------------------------
create table if not exists public.portfolio_events (
  id uuid primary key default gen_random_uuid(),
  position_id uuid references public.portfolio_positions(id) on delete set null,
  symbol text,
  event_type text not null check (event_type in ('create', 'update', 'delete', 'close')),
  payload_json jsonb not null default '{}'::jsonb,
  message text,
  created_at timestamptz not null default now()
);

-- -----------------------------------------------------------------------------
-- Indexes: optimise the common access patterns used by Flask routes
--   - symbol: lookups by ticker (most routes filter on this)
--   - status:  filter active vs closed records
--   - created_at/opened_at DESC: default sort order for list endpoints
-- -----------------------------------------------------------------------------
create index if not exists idx_recommendations_symbol on public.recommendations(symbol);
create index if not exists idx_recommendations_status on public.recommendations(status);
create index if not exists idx_recommendations_created_at on public.recommendations(created_at desc);

create index if not exists idx_portfolio_positions_symbol on public.portfolio_positions(symbol);
create index if not exists idx_portfolio_positions_status on public.portfolio_positions(status);
create index if not exists idx_portfolio_positions_opened_at on public.portfolio_positions(opened_at desc);

create index if not exists idx_portfolio_events_position_id on public.portfolio_events(position_id);
create index if not exists idx_portfolio_events_symbol on public.portfolio_events(symbol);
create index if not exists idx_portfolio_events_created_at on public.portfolio_events(created_at desc);
