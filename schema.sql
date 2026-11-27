-- Schéma Supabase pour "Notes-Cesag"
-- À exécuter dans Supabase > SQL Editor (en une fois).

create extension if not exists pgcrypto;

-- Profil du chef de classe (nom affiché dans les e-mails).
create table if not exists public.profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  full_name text not null default '',
  email text not null default '',
  created_at timestamptz not null default now()
);

-- Classes créées par un chef de classe.
create table if not exists public.classes (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  name text not null,
  created_at timestamptz not null default now()
);

-- Étudiants d'une classe.
create table if not exists public.students (
  id uuid primary key default gen_random_uuid(),
  class_id uuid not null references public.classes(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  nom text not null,
  prenom text not null,
  email text not null,
  created_at timestamptz not null default now()
);

-- Historique des envois. Les notes ne sont JAMAIS stockées ici.
create table if not exists public.send_history (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  class_id uuid not null references public.classes(id) on delete cascade,
  class_name text not null,
  matiere text not null,
  eval_type text not null check (eval_type in ('Test', 'Partiel')),
  sent_count integer not null check (sent_count >= 0),
  sent_at timestamptz not null default now()
);

create index if not exists students_class_id_idx on public.students(class_id);
create index if not exists students_user_id_idx on public.students(user_id);
create index if not exists classes_user_id_idx on public.classes(user_id);
create index if not exists send_history_user_id_idx on public.send_history(user_id);
create index if not exists send_history_sent_at_idx on public.send_history(sent_at);

-- Row Level Security : chaque utilisateur ne voit que ses données.
alter table public.profiles enable row level security;
alter table public.classes enable row level security;
alter table public.students enable row level security;
alter table public.send_history enable row level security;

-- Profiles
drop policy if exists "profiles_select_own" on public.profiles;
create policy "profiles_select_own" on public.profiles
  for select using (auth.uid() = id);
drop policy if exists "profiles_insert_own" on public.profiles;
create policy "profiles_insert_own" on public.profiles
  for insert with check (auth.uid() = id);
drop policy if exists "profiles_update_own" on public.profiles;
create policy "profiles_update_own" on public.profiles
  for update using (auth.uid() = id) with check (auth.uid() = id);

-- Classes
drop policy if exists "classes_select_own" on public.classes;
create policy "classes_select_own" on public.classes
  for select using (auth.uid() = user_id);
drop policy if exists "classes_insert_own" on public.classes;
create policy "classes_insert_own" on public.classes
  for insert with check (auth.uid() = user_id);
drop policy if exists "classes_update_own" on public.classes;
create policy "classes_update_own" on public.classes
  for update using (auth.uid() = user_id) with check (auth.uid() = user_id);
drop policy if exists "classes_delete_own" on public.classes;
create policy "classes_delete_own" on public.classes
  for delete using (auth.uid() = user_id);

-- Students
drop policy if exists "students_select_own" on public.students;
create policy "students_select_own" on public.students
  for select using (auth.uid() = user_id);
drop policy if exists "students_insert_own" on public.students;
create policy "students_insert_own" on public.students
  for insert with check (auth.uid() = user_id);
drop policy if exists "students_update_own" on public.students;
create policy "students_update_own" on public.students
  for update using (auth.uid() = user_id) with check (auth.uid() = user_id);
drop policy if exists "students_delete_own" on public.students;
create policy "students_delete_own" on public.students
  for delete using (auth.uid() = user_id);

-- Send history
drop policy if exists "history_select_own" on public.send_history;
create policy "history_select_own" on public.send_history
  for select using (auth.uid() = user_id);
drop policy if exists "history_insert_own" on public.send_history;
create policy "history_insert_own" on public.send_history
  for insert with check (auth.uid() = user_id);
drop policy if exists "history_delete_own" on public.send_history;
create policy "history_delete_own" on public.send_history
  for delete using (auth.uid() = user_id);

-- Crée automatiquement un profil à l'inscription.
create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer set search_path = ''
as $$
begin
  insert into public.profiles (id, full_name, email)
  values (
    new.id,
    coalesce(new.raw_user_meta_data ->> 'full_name', ''),
    coalesce(new.email, '')
  );
  return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute procedure public.handle_new_user();
