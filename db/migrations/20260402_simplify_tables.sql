-- =============================================================================
-- Simplify Schema: 2 Tables Only
-- Migration: 20260402_simplify_tables
-- =============================================================================
-- Replaces the old 4-table schema with two clean tables:
--   1. active_trades — open positions from screener BUY/STRONG BUY signals
--   2. closed_trades — resolved positions (target hit or SL hit) with P&L
--
-- Migrates existing data from 'trades' table before dropping it.
-- =============================================================================

create extension if not exists "pgcrypto";

-- -----------------------------------------------------------------------------
-- Step 1: Create new tables
-- -----------------------------------------------------------------------------

create table if not exists public.active_trades (
  id            uuid          primary key default gen_random_uuid(),
  added_date    date          not null default current_date,
  added_time    time          not null default current_time,
  symbol        text          not null,
  name          text,
  signal        text          not null check (signal in ('BUY', 'STRONG BUY')),
  score         integer       default 0,
  entry_price   numeric(12,2) not null default 0,
  entry_price_range text,
  target        numeric(12,2) not null default 0,
  stop_loss     numeric(12,2) not null default 0,
  status        text          not null default 'open'
);

create table if not exists public.closed_trades (
  id            uuid          primary key default gen_random_uuid(),
  added_date    date          not null,
  added_time    time          not null,
  symbol        text          not null,
  name          text,
  signal        text          not null,
  score         integer       default 0,
  entry_price   numeric(12,2) not null default 0,
  entry_price_range text,
  target        numeric(12,2) not null default 0,
  stop_loss     numeric(12,2) not null default 0,
  status        text          not null,
  pnl           numeric(12,2) default 0,
  pnl_pct       numeric(8,2)  default 0,
  outcome_date  date,
  days_held     integer       default 0
);

-- -----------------------------------------------------------------------------
-- Step 2: Migrate data from old 'trades' table (if it exists)
-- -----------------------------------------------------------------------------

-- Copy open trades → active_trades
insert into public.active_trades (
  added_date, added_time, symbol, name, signal, score,
  entry_price, entry_price_range, target, stop_loss, status
)
select
  coalesce(cast(scanned_at as date), current_date),
  coalesce(cast(scanned_at as time), current_time),
  symbol,
  name,
  signal,
  coalesce(score, 0),
  coalesce(entry_price, 0),
  case
    when buy_range_low is not null and buy_range_high is not null
    then buy_range_low::text || '-' || buy_range_high::text
    else null
  end,
  coalesce(target1, 0),
  coalesce(stop_loss, 0),
  'open'
from public.trades
where status = 'open'
  and signal in ('BUY', 'STRONG BUY');

-- Copy closed trades (target_hit / sl_hit) → closed_trades
insert into public.closed_trades (
  added_date, added_time, symbol, name, signal, score,
  entry_price, entry_price_range, target, stop_loss, status,
  pnl, pnl_pct, outcome_date, days_held
)
select
  coalesce(cast(scanned_at as date), current_date),
  coalesce(cast(scanned_at as time), current_time),
  symbol,
  name,
  signal,
  coalesce(score, 0),
  coalesce(entry_price, 0),
  case
    when buy_range_low is not null and buy_range_high is not null
    then buy_range_low::text || '-' || buy_range_high::text
    else null
  end,
  coalesce(target1, 0),
  coalesce(stop_loss, 0),
  status,
  -- Calculate P&L: outcome_price - entry_price
  case
    when outcome_price is not null and entry_price is not null
    then round(outcome_price - entry_price, 2)
    else 0
  end,
  -- Calculate P&L %: ((outcome_price - entry_price) / entry_price) * 100
  case
    when outcome_price is not null and entry_price is not null and entry_price > 0
    then round(((outcome_price - entry_price) / entry_price) * 100, 2)
    else 0
  end,
  cast(outcome_date as date),
  coalesce(days_to_outcome, 0)
from public.trades
where status in ('target_hit', 'sl_hit');

-- -----------------------------------------------------------------------------
-- Step 3: Drop old tables
-- -----------------------------------------------------------------------------

drop table if exists public.portfolio_events cascade;
drop table if exists public.portfolio_positions cascade;
drop table if exists public.recommendations cascade;
drop table if exists public.trades cascade;

-- -----------------------------------------------------------------------------
-- Step 4: Create indexes
-- -----------------------------------------------------------------------------

create index if not exists idx_active_trades_symbol on public.active_trades(symbol);
create index if not exists idx_active_trades_added_date on public.active_trades(added_date desc);
create index if not exists idx_active_trades_status on public.active_trades(status);

create index if not exists idx_closed_trades_symbol on public.closed_trades(symbol);
create index if not exists idx_closed_trades_added_date on public.closed_trades(added_date desc);
create index if not exists idx_closed_trades_status on public.closed_trades(status);
create index if not exists idx_closed_trades_outcome_date on public.closed_trades(outcome_date desc);
