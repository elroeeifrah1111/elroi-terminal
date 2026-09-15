-- ============================================================
-- Elroi Terminal — Supabase schema (free tier)
-- Run ONCE in the Supabase dashboard: SQL Editor -> paste -> Run.
-- ============================================================

create extension if not exists "pgcrypto";

-- ---------------- alerts ----------------
create table if not exists alerts (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null,
  name text,
  symbol text,
  market text,
  rule jsonb,
  frequency text default 'once',
  expires_at timestamptz,
  active boolean default true,
  created_at timestamptz default now(),
  updated_at timestamptz default now(),
  last_triggered_at timestamptz,
  last_bar bigint,
  trigger_count int default 0
);
create index if not exists alerts_user_id_idx on alerts (user_id);

-- ---------------- drawings ----------------
create table if not exists drawings (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null,
  symbol text not null,
  timeframe text not null,
  drawings jsonb,
  updated_at timestamptz default now(),
  unique (user_id, symbol, timeframe)
);
create index if not exists drawings_user_id_idx on drawings (user_id);

-- ---------------- chart layouts ----------------
create table if not exists chart_layouts (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null,
  name text not null,
  layout jsonb,
  is_default boolean default false,
  updated_at timestamptz default now()
);
create index if not exists chart_layouts_user_id_idx on chart_layouts (user_id);

-- ---------------- watchlists ----------------
create table if not exists watchlists (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null,
  name text not null,
  symbols jsonb,
  updated_at timestamptz default now()
);
create index if not exists watchlists_user_id_idx on watchlists (user_id);

-- ---------------- custom (AI) indicators ----------------
create table if not exists custom_indicators (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null,
  name text not null,
  description text,
  code text,
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);
create index if not exists custom_indicators_user_id_idx on custom_indicators (user_id);

-- ---------------- updated_at auto-bump ----------------
create or replace function bump_updated_at()
returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

drop trigger if exists trg_alerts_updated on alerts;
create trigger trg_alerts_updated before update on alerts
  for each row execute function bump_updated_at();

drop trigger if exists trg_drawings_updated on drawings;
create trigger trg_drawings_updated before update on drawings
  for each row execute function bump_updated_at();

drop trigger if exists trg_layouts_updated on chart_layouts;
create trigger trg_layouts_updated before update on chart_layouts
  for each row execute function bump_updated_at();

drop trigger if exists trg_watchlists_updated on watchlists;
create trigger trg_watchlists_updated before update on watchlists
  for each row execute function bump_updated_at();

drop trigger if exists trg_indicators_updated on custom_indicators;
create trigger trg_indicators_updated before update on custom_indicators
  for each row execute function bump_updated_at();

-- ---------------- Row Level Security ----------------
-- The server uses the SERVICE key (bypasses RLS). These policies protect
-- the tables if the anon key is ever used directly: a signed-in user can
-- only touch rows whose user_id matches their own auth.uid().

alter table alerts enable row level security;
alter table drawings enable row level security;
alter table chart_layouts enable row level security;
alter table watchlists enable row level security;
alter table custom_indicators enable row level security;

drop policy if exists own_rows on alerts;
create policy own_rows on alerts
  for all to authenticated
  using (user_id = auth.uid())
  with check (user_id = auth.uid());

drop policy if exists own_rows on drawings;
create policy own_rows on drawings
  for all to authenticated
  using (user_id = auth.uid())
  with check (user_id = auth.uid());

drop policy if exists own_rows on chart_layouts;
create policy own_rows on chart_layouts
  for all to authenticated
  using (user_id = auth.uid())
  with check (user_id = auth.uid());

drop policy if exists own_rows on watchlists;
create policy own_rows on watchlists
  for all to authenticated
  using (user_id = auth.uid())
  with check (user_id = auth.uid());

drop policy if exists own_rows on custom_indicators;
create policy own_rows on custom_indicators
  for all to authenticated
  using (user_id = auth.uid())
  with check (user_id = auth.uid());
