-- appmux v1 baseline: the nine tables of the registry (docs/product/v1-data-model.md, section 4).
--
-- Names, types and constraints are final for v1. A later change is a new numbered migration,
-- never an edit of this file. Standard PostgreSQL only. The file holds no BEGIN or COMMIT because
-- the loader applies every migration in one transaction.
--
-- The lists the code owns are not checked here: platform_settings.key, permission_requests.kind,
-- audit_log.action, app_resources.type and app_resources.permission.
--
-- The three composite keys of app_config come last, in the ALTER TABLE, because they refer to
-- releases and app_resources, which do not exist when app_config is created (spike 9: PostgreSQL
-- refused them inside CREATE TABLE). They are DEFERRABLE INITIALLY DEFERRED so that renaming a slug
-- can rewrite app_config, releases and app_resources in any order and be checked once, at commit.

CREATE TABLE platform_settings (
  key        text PRIMARY KEY,                       -- checked against the code's list of keys, not by SQL
  value      jsonb NOT NULL,                         -- the shape depends on the key; validated in code
  updated_by text NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE apps (
  slug                text PRIMARY KEY CHECK (slug ~ '^[a-z0-9][a-z0-9-]{0,30}$'),   -- renamable; the foreign keys to it are ON UPDATE CASCADE
  display_name        text NOT NULL CHECK (btrim(display_name) <> ''),
  owner               text NOT NULL CHECK (owner = lower(owner)),
  source_path         text NOT NULL CHECK (source_path LIKE '/Workspace/%'),
  memory_budget_mb    integer NOT NULL CHECK (memory_budget_mb BETWEEN 64 AND 16384),
  obo_delegated       boolean NOT NULL DEFAULT false,     -- set only by approving a delegation request, or directly by a platform admin
  shared_sp_delegated boolean NOT NULL DEFAULT false,
  egress              jsonb NOT NULL DEFAULT '[]',        -- [{"action", "cidr", "port", "comment"}], in order
  version             bigint NOT NULL DEFAULT 1,
  created_by          text NOT NULL,
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_by          text NOT NULL,
  updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE permission_requests (
  request_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  app_slug     text REFERENCES apps (slug) ON UPDATE CASCADE ON DELETE CASCADE,        -- null only while a request to register waits
  kind         text NOT NULL CHECK (kind ~ '^[a-z][a-z_]{0,31}$'),   -- the list of kinds is in code
  payload      jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(payload) = 'object'),
  reason       text NOT NULL CHECK (btrim(reason) <> '' AND char_length(reason) <= 500),
  requested_by text NOT NULL,
  requested_at timestamptz NOT NULL DEFAULT now(),
  status       text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'denied', 'withdrawn')),
  closed_by    text,         -- the platform admin who answered, or the requester who withdrew
  closed_at    timestamptz,
  closed_note  text,
  CHECK (app_slug IS NOT NULL OR payload ? 'slug'),                  -- a request to register names its slug in payload
  CHECK ((status = 'pending') = (closed_at IS NULL)),
  CHECK ((closed_at IS NULL) = (closed_by IS NULL))
);
CREATE UNIQUE INDEX one_pending_request ON permission_requests (coalesce(app_slug, payload ->> 'slug'), kind) WHERE status = 'pending';

CREATE TABLE app_config (
  app_slug                 text PRIMARY KEY REFERENCES apps (slug) ON UPDATE CASCADE ON DELETE CASCADE,
  desired_state            text NOT NULL DEFAULT 'stopped' CHECK (desired_state IN ('running', 'stopped')),
  target_release_no        integer,
  health_path              text NOT NULL DEFAULT '/' CHECK (health_path LIKE '/%'),
  health_deadline_s        integer NOT NULL DEFAULT 60 CHECK (health_deadline_s BETWEEN 5 AND 600),
  own_sp_client_id_key     text,
  own_sp_client_secret_key text,
  version                  bigint NOT NULL DEFAULT 1,
  updated_by               text NOT NULL,
  updated_at               timestamptz NOT NULL DEFAULT now(),
  CHECK ((own_sp_client_id_key IS NULL) = (own_sp_client_secret_key IS NULL))
);

CREATE TABLE app_grants (
  app_slug       text NOT NULL REFERENCES apps (slug) ON UPDATE CASCADE ON DELETE CASCADE,
  principal_type text NOT NULL CHECK (principal_type IN ('user', 'group', 'everyone')),
  principal      text NOT NULL,
  permission     text NOT NULL CHECK (permission IN ('CAN_USE', 'CAN_MANAGE')),
  granted_by     text NOT NULL,
  granted_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (app_slug, principal_type, principal),
  CHECK ((principal_type = 'everyone') = (principal = '*')),
  CHECK (principal_type <> 'user' OR principal = lower(principal))
);
CREATE INDEX app_grants_by_principal ON app_grants (principal_type, principal);  -- the launcher

CREATE TABLE releases (
  app_slug        text NOT NULL REFERENCES apps (slug) ON UPDATE CASCADE ON DELETE CASCADE,
  release_no      integer NOT NULL CHECK (release_no > 0),
  snapshot_digest text CHECK (snapshot_digest ~ '^sha256:[0-9a-f]{64}$'),
  detected        jsonb NOT NULL,                      -- set once, at creation
  config          jsonb NOT NULL DEFAULT '{}',         -- set once, at creation
  wheels          jsonb NOT NULL DEFAULT '[]',         -- set once, by the install
  status          text NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'building', 'starting', 'healthy', 'active', 'failed', 'superseded')),
  status_reason   text,
  build_log_tail  text,
  status_at       timestamptz NOT NULL DEFAULT now(),
  label           text CHECK (label IS NULL OR char_length(label) BETWEEN 1 AND 80),
  tags            text[] NOT NULL DEFAULT '{}',        -- free labels; no effect on retention
  locked_by       text,                                -- a manual lock; the active release needs none
  locked_at       timestamptz,
  created_by      text NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  deleted_by      text,
  deleted_at      timestamptz,
  PRIMARY KEY (app_slug, release_no),
  CHECK ((deleted_at IS NULL) = (snapshot_digest IS NOT NULL)),   -- a live release has a snapshot; a deleted one has none
  CHECK ((deleted_at IS NULL) = (deleted_by IS NULL)),
  CHECK ((locked_at IS NULL) = (locked_by IS NULL)),
  CHECK (deleted_at IS NULL OR locked_at IS NULL)                  -- a release is unlocked before it is deleted
);
CREATE UNIQUE INDEX one_active_release ON releases (app_slug) WHERE status = 'active';   -- exactly one live release

CREATE TABLE app_resources (
  app_slug     text NOT NULL REFERENCES apps (slug) ON UPDATE CASCADE ON DELETE CASCADE,
  key          text NOT NULL CHECK (key ~ '^[a-z0-9][a-z0-9._-]{0,62}$'),
  type         text NOT NULL,                          -- a type of the catalogue in code
  target       text NOT NULL CHECK (btrim(target) <> ''),
  permission   text NOT NULL,                          -- a level of the type, validated in code
  access_as    text CHECK (access_as IN ('obo', 'own_sp', 'shared_sp')),
  check_result text NOT NULL CHECK (check_result IN ('ok', 'unverified')),
  attached_by  text NOT NULL,
  attached_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (app_slug, key),
  CHECK ((type = 'secret') = (access_as IS NULL)),                  -- a secret is read by appmux, not used as a credential
  CHECK (type <> 'secret' OR target ~ '^[^/]+/[^/]+$'),             -- scope/key
  CHECK (NOT (access_as = 'shared_sp' AND check_result <> 'ok'))    -- shared_sp only after a verified check
);

CREATE TABLE metrics (
  app_slug               text NOT NULL,                   -- no foreign key: '_platform' is not an app
  minute                 timestamptz NOT NULL,
  requests               integer NOT NULL DEFAULT 0,
  r2xx                   integer NOT NULL DEFAULT 0,
  r3xx                   integer NOT NULL DEFAULT 0,
  r4xx                   integer NOT NULL DEFAULT 0,
  r5xx                   integer NOT NULL DEFAULT 0,
  upstream_errors        integer NOT NULL DEFAULT 0,
  limited                integer NOT NULL DEFAULT 0,
  lat_hist               integer[] NOT NULL CHECK (cardinality(lat_hist) = 11),
  bytes_in               bigint NOT NULL DEFAULT 0,
  bytes_out              bigint NOT NULL DEFAULT 0,
  in_flight_max          integer NOT NULL DEFAULT 0,
  ws_opened              integer NOT NULL DEFAULT 0,
  ws_open                integer NOT NULL DEFAULT 0,
  rss_mb_max             integer NOT NULL DEFAULT 0,
  restarts               integer NOT NULL DEFAULT 0,
  egress_bytes_out       bigint NOT NULL DEFAULT 0,
  egress_bytes_in        bigint NOT NULL DEFAULT 0,
  egress_dropped         integer NOT NULL DEFAULT 0,
  egress_dropped_by_rule jsonb NOT NULL DEFAULT '{}',
  mem_used_mb            integer,                          -- the next eight: '_platform' rows only
  mem_limit_mb           integer,
  children_running       integer,
  children_registered    integer,
  admission_rejections   integer,
  registry_errors        integer,
  registry_ms_max        integer,
  edge_rss_mb            integer,
  PRIMARY KEY (app_slug, minute),
  CHECK ((app_slug = '_platform') = (mem_used_mb IS NOT NULL))
);

CREATE TABLE audit_log (
  id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ts       timestamptz NOT NULL DEFAULT now(),
  actor    text NOT NULL,
  action   text NOT NULL,                           -- one of the names above, checked in code
  app_slug text,                                    -- no foreign key
  detail   jsonb NOT NULL DEFAULT '{}',
  reason   text
);
CREATE INDEX audit_by_app ON audit_log (app_slug, ts DESC);
CREATE INDEX audit_by_actor ON audit_log (actor, ts DESC);

ALTER TABLE app_config
  ADD FOREIGN KEY (app_slug, target_release_no) REFERENCES releases (app_slug, release_no) DEFERRABLE INITIALLY DEFERRED,
  ADD FOREIGN KEY (app_slug, own_sp_client_id_key)     REFERENCES app_resources (app_slug, key) DEFERRABLE INITIALLY DEFERRED,
  ADD FOREIGN KEY (app_slug, own_sp_client_secret_key) REFERENCES app_resources (app_slug, key) DEFERRABLE INITIALLY DEFERRED;
