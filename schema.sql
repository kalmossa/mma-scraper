-- ============================================================================
-- mma-scraper - PostgreSQL schema
--
-- The minimum the scrapers need: fighters, events, event_fights.
-- Idempotent: safe to run several times.
--
--   psql -d mma_scraper -f schema.sql
-- ============================================================================

-- ----------------------------------------------------------------------------
-- Name normalization (used by the anti-duplicate unique index)
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION norm_name(text) RETURNS text
LANGUAGE sql IMMUTABLE STRICT AS $$
    SELECT lower(
        regexp_replace(
            regexp_replace($1, '[^a-zA-Z0-9 ]', '', 'g'),
            '\s+', ' ', 'g'
        )
    )
$$;

-- ----------------------------------------------------------------------------
-- fighters
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fighters (
    id                              SERIAL PRIMARY KEY,

    -- external identifiers (aligned on id after INSERT, see scrape_batch.py)
    ufc_id                          VARCHAR(80)  UNIQUE,
    fightmatrix_id                  VARCHAR(80)  UNIQUE,
    mma_com_id                      VARCHAR(80)  UNIQUE,

    -- identity
    name                            VARCHAR(150) NOT NULL,
    nickname                        VARCHAR(150),
    gender                          VARCHAR(1)   NOT NULL DEFAULT 'M' CHECK (gender IN ('M', 'F')),
    date_of_birth                   DATE,
    age                             INTEGER,
    nationality                     VARCHAR(60),
    photo_url                       TEXT,
    photo_thumbnail_url             TEXT,

    -- physical / style
    weight_class_current            VARCHAR(50)  NOT NULL DEFAULT 'Unknown',
    weight_class_origin             VARCHAR(50),
    height_inches                   VARCHAR(15),
    reach_inches                    VARCHAR(15),
    stance                          VARCHAR(30),
    first_sport                     VARCHAR(100),

    -- record (career / UFC / everything else)
    record_total_wins               INTEGER      NOT NULL DEFAULT 0 CHECK (record_total_wins >= 0),
    record_total_losses             INTEGER      NOT NULL DEFAULT 0 CHECK (record_total_losses >= 0),
    record_total_draws              INTEGER      DEFAULT 0,
    record_total_nc                 INTEGER      DEFAULT 0,
    record_ufc_wins                 INTEGER      DEFAULT 0,
    record_ufc_losses               INTEGER      DEFAULT 0,
    record_ufc_draws                INTEGER      DEFAULT 0,
    record_other_wins               INTEGER      DEFAULT 0,
    record_other_losses             INTEGER      DEFAULT 0,
    record_other_draws              INTEGER      DEFAULT 0,

    -- win / loss methods
    wins_by_ko_tko                  INTEGER      DEFAULT 0,
    wins_by_submission              INTEGER      DEFAULT 0,
    wins_by_decision                INTEGER      DEFAULT 0,
    losses_by_ko_tko                INTEGER      DEFAULT 0,
    losses_by_submission            INTEGER      DEFAULT 0,
    losses_by_decision              INTEGER      DEFAULT 0,
    split_decision_wins             INTEGER      DEFAULT 0,
    split_decision_losses           INTEGER      DEFAULT 0,

    -- career timeline / status
    career_debut_date               DATE,
    last_fight_date                 DATE,
    days_inactive                   INTEGER,
    is_active                       BOOLEAN      DEFAULT TRUE,
    is_ufc_champion                 BOOLEAN      DEFAULT FALSE,
    ufc_title_match_win             INTEGER      DEFAULT 0,
    total_fights                    INTEGER      DEFAULT 0,
    win_percentage_int              INTEGER,
    finish_rate_int                 INTEGER,
    current_streak                  VARCHAR(15)  DEFAULT '0',
    last_5_results                  VARCHAR(30),
    current_league                  VARCHAR(50),

    -- FightMatrix reference metrics
    fightmatrix_rating_points       NUMERIC,
    fightmatrix_big_league_record   VARCHAR(30),
    fightmatrix_540_metric          NUMERIC,
    fightmatrix_quality_perf_pct    NUMERIC,

    -- official UFC rankings (0 = champion)
    ufc_official_rank               INTEGER,
    ufc_p4p_rank                    INTEGER,

    -- provenance / quality
    data_source                     TEXT,            -- scraped URLs, ' | ' separated
    data_quality_score              INTEGER      DEFAULT 0 CHECK (data_quality_score BETWEEN 0 AND 100),
    is_verified                     BOOLEAN      DEFAULT FALSE,
    notes                           TEXT,
    fight_history                   TEXT,            -- JSON: {"source", "count", "fights": [...]}
    last_scraped_at                 TIMESTAMP,
    created_at                      TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    updated_at                      TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,

    -- anti-duplicate key: the scrapers rely on this constraint name
    CONSTRAINT uq_fighter_name_dob UNIQUE (name, date_of_birth)
);

-- Second anti-duplicate net: same normalized name + same date of birth
CREATE UNIQUE INDEX IF NOT EXISTS uq_fighter_name_norm_dob
    ON fighters (norm_name(COALESCE(name, '')), date_of_birth)
    WHERE date_of_birth IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_fighters_name          ON fighters (name);
CREATE INDEX IF NOT EXISTS idx_fighters_weight_class  ON fighters (weight_class_current);
CREATE INDEX IF NOT EXISTS idx_fighters_nationality   ON fighters (nationality);
CREATE INDEX IF NOT EXISTS idx_fighters_last_fight    ON fighters (last_fight_date DESC);
CREATE INDEX IF NOT EXISTS idx_fighters_active        ON fighters (is_active) WHERE is_active = TRUE;
CREATE INDEX IF NOT EXISTS idx_fighters_champion      ON fighters (is_ufc_champion) WHERE is_ufc_champion = TRUE;
CREATE INDEX IF NOT EXISTS idx_fighters_league        ON fighters (current_league) WHERE current_league IS NOT NULL;

-- ----------------------------------------------------------------------------
-- Triggers on fighters
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION update_updated_at() RETURNS TRIGGER AS $$
BEGIN NEW.updated_at = CURRENT_TIMESTAMP; RETURN NEW; END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION update_fighter_age() RETURNS TRIGGER AS $$
BEGIN
    IF NEW.date_of_birth IS NOT NULL THEN
        NEW.age := EXTRACT(YEAR FROM AGE(CURRENT_DATE, NEW.date_of_birth))::INT;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION update_days_inactive() RETURNS TRIGGER AS $$
BEGIN
    IF NEW.last_fight_date IS NOT NULL THEN
        NEW.days_inactive := CURRENT_DATE - NEW.last_fight_date;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION update_total_fights() RETURNS TRIGGER AS $$
BEGIN
    NEW.total_fights := COALESCE(NEW.record_total_wins, 0)
                      + COALESCE(NEW.record_total_losses, 0)
                      + COALESCE(NEW.record_total_draws, 0);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_fighters_updated_at   ON fighters;
DROP TRIGGER IF EXISTS trg_fighters_age          ON fighters;
DROP TRIGGER IF EXISTS trg_fighters_inactivite   ON fighters;
DROP TRIGGER IF EXISTS trg_fighters_total_fights ON fighters;

CREATE TRIGGER trg_fighters_updated_at
    BEFORE UPDATE ON fighters
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER trg_fighters_age
    BEFORE INSERT OR UPDATE OF date_of_birth ON fighters
    FOR EACH ROW EXECUTE FUNCTION update_fighter_age();

CREATE TRIGGER trg_fighters_inactivite
    BEFORE INSERT OR UPDATE OF last_fight_date ON fighters
    FOR EACH ROW EXECUTE FUNCTION update_days_inactive();

CREATE TRIGGER trg_fighters_total_fights
    BEFORE INSERT OR UPDATE OF record_total_wins, record_total_losses, record_total_draws ON fighters
    FOR EACH ROW EXECUTE FUNCTION update_total_fights();

-- ----------------------------------------------------------------------------
-- events
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS events (
    id            SERIAL PRIMARY KEY,
    name          TEXT        NOT NULL,
    slug          TEXT,
    promotion     TEXT,
    date          DATE,
    venue         TEXT,
    city          TEXT,
    country       TEXT,
    tapology_url  TEXT UNIQUE,      -- also holds the espn-{id} slug for ESPN events
    status        TEXT        NOT NULL DEFAULT 'completed',   -- completed | upcoming
    awards        JSONB       NOT NULL DEFAULT '[]'::jsonb,
    poster_url    TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_events_date      ON events (date DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_events_promotion ON events (promotion);
CREATE INDEX IF NOT EXISTS idx_events_status    ON events (status);

-- ----------------------------------------------------------------------------
-- event_fights (one row per bout)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS event_fights (
    id              SERIAL PRIMARY KEY,
    event_id        INTEGER     NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    card_section    TEXT        NOT NULL DEFAULT 'Main Card',
    bout_order      INTEGER     NOT NULL DEFAULT 0,
    fighter1_id     INTEGER     REFERENCES fighters(id),
    fighter2_id     INTEGER     REFERENCES fighters(id),
    fighter1_name   TEXT        NOT NULL,
    fighter2_name   TEXT        NOT NULL,
    weight_class    TEXT,
    weight_lbs      SMALLINT,
    rounds          TEXT,
    is_title_fight  BOOLEAN     NOT NULL DEFAULT FALSE,
    winner_id       INTEGER     REFERENCES fighters(id),
    winner_name     TEXT,
    method          TEXT,
    method_detail   TEXT,
    round_num       INTEGER,
    time_str        TEXT,
    status          TEXT        NOT NULL DEFAULT 'completed',
    event_date      DATE,       -- denormalized copy of events.date
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_event_fights_event_id ON event_fights (event_id);
CREATE INDEX IF NOT EXISTS idx_event_fights_f1       ON event_fights (fighter1_id);
CREATE INDEX IF NOT EXISTS idx_event_fights_f2       ON event_fights (fighter2_id);
