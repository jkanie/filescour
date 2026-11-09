-- filescour schema. Safe to run more than once.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS roots (
    id       serial PRIMARY KEY,
    path     text NOT NULL UNIQUE,
    -- Read the text of files whose attributes cannot be seen (a Windows
    -- drive seen from WSL). Online-only files are never read.
    extract_unknown boolean NOT NULL DEFAULT false,
    added_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS files (
    id             bigserial PRIMARY KEY,
    root_id        integer NOT NULL REFERENCES roots (id) ON DELETE CASCADE,
    path           text NOT NULL UNIQUE,
    name           text NOT NULL,
    ext            text NOT NULL,
    size           bigint NOT NULL CHECK (size >= 0),
    mtime          timestamptz NOT NULL,
    content        text,
    content_source text CHECK (content_source IN ('file', 'pdf')),
    truncated      boolean NOT NULL DEFAULT false,
    -- Where the data is: on this disk, online only, or not known (see cloud.py).
    cloud          text NOT NULL DEFAULT 'local'
                   CHECK (cloud IN ('local', 'online', 'unknown')),
    -- Windows file attributes from the folder listing, NULL off Windows.
    attrs          bigint,
    indexed_at     timestamptz,
    -- Word search covers the first 500,000 characters, which keeps every
    -- tsvector under PostgreSQL's 1 MB limit.
    tsv            tsvector GENERATED ALWAYS AS
                   (to_tsvector('simple', left(coalesce(content, ''), 500000))) STORED
);

-- An entry of a zip file has its own row, with the zip's row as its parent
-- and a path such as /home/me/backup.zip!/notes/may.txt (see archive.py).
-- Deleting the zip's row deletes its entries. ADD COLUMN IF NOT EXISTS
-- brings a database made before this column up to date.
ALTER TABLE files ADD COLUMN IF NOT EXISTS
    parent_id bigint REFERENCES files (id) ON DELETE CASCADE;

CREATE INDEX IF NOT EXISTS files_root_idx ON files (root_id);
CREATE INDEX IF NOT EXISTS files_parent_idx ON files (parent_id)
    WHERE parent_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS files_cloud_idx ON files (cloud) WHERE cloud <> 'local';
CREATE INDEX IF NOT EXISTS files_name_trgm ON files USING gin (name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS files_path_trgm ON files USING gin (path gin_trgm_ops);
CREATE INDEX IF NOT EXISTS files_content_trgm ON files USING gin (content gin_trgm_ops);
CREATE INDEX IF NOT EXISTS files_tsv_idx ON files USING gin (tsv);

CREATE TABLE IF NOT EXISTS jobs (
    id         bigserial PRIMARY KEY,
    kind       text NOT NULL CHECK (kind IN ('extract')),
    file_id    bigint NOT NULL REFERENCES files (id) ON DELETE CASCADE,
    state      text NOT NULL DEFAULT 'queued'
               CHECK (state IN ('queued', 'running', 'done', 'failed', 'skipped')),
    attempts   integer NOT NULL DEFAULT 0,
    error      text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS jobs_queued_idx ON jobs (id) WHERE state = 'queued';
CREATE INDEX IF NOT EXISTS jobs_file_idx ON jobs (file_id);

-- The down-load journal (see fetch.py). A row records a file's attributes
-- before anything changes it, and it closes only once the file is confirmed
-- back in the state the row asks for.
CREATE TABLE IF NOT EXISTS fetches (
    id            bigserial PRIMARY KEY,
    file_id       bigint REFERENCES files (id) ON DELETE SET NULL,
    path          text NOT NULL,
    size          bigint NOT NULL,
    mode          text NOT NULL CHECK (mode IN ('temporary', 'keep')),
    state         text NOT NULL DEFAULT 'queued'
                  CHECK (state IN ('queued', 'downloading', 'restoring',
                                   'done', 'skipped')),
    earlier_attrs bigint,
    note          text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS fetches_open_idx ON fetches (id)
    WHERE state IN ('queued', 'downloading', 'restoring');
