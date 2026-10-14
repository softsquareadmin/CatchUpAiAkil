-- Interview Assistant: one table that mirrors the app's small text files. Run once in Supabase > SQL Editor.
begin;
create table if not exists public.ia_files (
    path text primary key,                 -- e.g. sessions/20261014-101500-ab12/ledger.jsonl, packs/cps_interview_v2__0-6-1.yaml
    content text not null,
    size_bytes integer generated always as (octet_length(content)) stored,
    updated_at timestamptz not null default now()
);
create index if not exists ia_files_prefix on public.ia_files (path text_pattern_ops);
alter table public.ia_files enable row level security;
revoke all on public.ia_files from anon, authenticated;
grant select, insert, update on public.ia_files to service_role;   -- no delete: nothing is ever removed
notify pgrst, 'reload schema';
commit;
