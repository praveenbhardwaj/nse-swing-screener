-- Portfolio module schema: recommendations, positions, events.

create extension if not exists "pgcrypto";

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

create table if not exists public.portfolio_events (
  id uuid primary key default gen_random_uuid(),
  position_id uuid references public.portfolio_positions(id) on delete set null,
  symbol text,
  event_type text not null check (event_type in ('create', 'update', 'delete', 'close')),
  payload_json jsonb not null default '{}'::jsonb,
  message text,
  created_at timestamptz not null default now()
);

create index if not exists idx_recommendations_symbol on public.recommendations(symbol);
create index if not exists idx_recommendations_status on public.recommendations(status);
create index if not exists idx_recommendations_created_at on public.recommendations(created_at desc);

create index if not exists idx_portfolio_positions_symbol on public.portfolio_positions(symbol);
create index if not exists idx_portfolio_positions_status on public.portfolio_positions(status);
create index if not exists idx_portfolio_positions_opened_at on public.portfolio_positions(opened_at desc);

create index if not exists idx_portfolio_events_position_id on public.portfolio_events(position_id);
create index if not exists idx_portfolio_events_symbol on public.portfolio_events(symbol);
create index if not exists idx_portfolio_events_created_at on public.portfolio_events(created_at desc);
