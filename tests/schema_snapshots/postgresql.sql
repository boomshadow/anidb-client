CREATE TYPE anime_relation_type_enum AS ENUM ('sequel', 'prequel', 'same setting', 'alternative setting', 'alternative version', 'music video', 'character', 'side story', 'parent story', 'summary', 'full story', 'other');

CREATE TYPE episode_type_enum AS ENUM ('regular', 'special', 'credit', 'trailer', 'parody', 'other');

CREATE TYPE group_relation_type_enum AS ENUM ('participant in', 'parent of', 'merged from', 'now known as', 'other', 'includes', 'formerly', 'merged into', 'lost part', 'split from', 'child of');

CREATE TYPE mylist_filestate_enum AS ENUM ('normal/original', 'corrupted version/invalid crc', 'self edited', 'self ripped', 'on dvd', 'on vhs', 'on tv', 'in theaters', 'streamed', 'other');

CREATE TYPE mylist_state_enum AS ENUM ('unknown', 'on hdd', 'on cd', 'deleted');

CREATE TABLE anime (
    pk BIGSERIAL NOT NULL,
    aid BIGINT NOT NULL,
    year VARCHAR(16) NOT NULL,
    type VARCHAR(16) NOT NULL,
    nr_of_episodes INTEGER NOT NULL,
    highest_episode_number INTEGER NOT NULL,
    special_ep_count INTEGER NOT NULL,
    air_date DATE,
    end_date DATE,
    url VARCHAR(512),
    picname VARCHAR(128),
    rating FLOAT,
    vote_count INTEGER NOT NULL,
    temp_rating FLOAT,
    temp_vote_count INTEGER NOT NULL,
    average_review_rating FLOAT,
    review_count INTEGER NOT NULL,
    is_18_restricted BOOLEAN NOT NULL,
    ann_id BIGINT,
    allcinema_id BIGINT,
    animenfo_id VARCHAR(64),
    anidb_updated TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    special_count INTEGER NOT NULL,
    credit_count INTEGER NOT NULL,
    other_count INTEGER NOT NULL,
    trailer_count INTEGER NOT NULL,
    parody_count INTEGER NOT NULL,
    updated TIMESTAMP WITH TIME ZONE NOT NULL,
    last_update_dice TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (pk),
    UNIQUE (aid)
);

CREATE TABLE episode (
    pk BIGSERIAL NOT NULL,
    aid BIGINT NOT NULL,
    eid BIGINT NOT NULL,
    length INTEGER NOT NULL,
    rating FLOAT,
    votes INTEGER NOT NULL,
    epno VARCHAR(8) NOT NULL,
    title_eng VARCHAR(512),
    title_romaji VARCHAR(512),
    title_kanji VARCHAR(512),
    aired DATE,
    type episode_type_enum NOT NULL,
    updated TIMESTAMP WITH TIME ZONE NOT NULL,
    last_update_dice TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (pk)
);

CREATE INDEX ix_episode_aid ON episode (aid);

CREATE UNIQUE INDEX ix_episode_eid ON episode (eid);

CREATE TABLE file (
    pk BIGSERIAL NOT NULL,
    path VARCHAR(512),
    size BIGINT,
    ed2khash VARCHAR(64),
    mtime TIMESTAMP WITHOUT TIME ZONE,
    aid BIGINT NOT NULL,
    gid BIGINT,
    eid BIGINT NOT NULL,
    fid BIGINT,
    is_deprecated BOOLEAN,
    is_generic BOOLEAN NOT NULL,
    part INTEGER,
    crc_ok BOOLEAN,
    file_version INTEGER,
    censored BOOLEAN,
    length_in_seconds INTEGER,
    description VARCHAR(512),
    aired_date DATE,
    mylist_state mylist_state_enum,
    mylist_filestate mylist_filestate_enum,
    mylist_viewed BOOLEAN,
    mylist_viewdate TIMESTAMP WITHOUT TIME ZONE,
    mylist_storage VARCHAR(128),
    mylist_source VARCHAR(128),
    mylist_other VARCHAR(128),
    lid BIGINT,
    updated TIMESTAMP WITH TIME ZONE,
    last_update_dice TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (pk)
);

CREATE INDEX ix_file_aid ON file (aid);

CREATE INDEX ix_file_eid ON file (eid);

CREATE INDEX ix_file_fid ON file (fid);

CREATE TABLE "group" (
    pk BIGSERIAL NOT NULL,
    gid BIGINT,
    rating BIGINT,
    votes BIGINT,
    acount BIGINT,
    fcount BIGINT,
    name VARCHAR(248) NOT NULL,
    short VARCHAR(64) NOT NULL,
    irc_channel VARCHAR(32),
    irc_server VARCHAR(32),
    url VARCHAR(248),
    picname VARCHAR(32),
    founded TIMESTAMP WITHOUT TIME ZONE,
    disbanded TIMESTAMP WITHOUT TIME ZONE,
    dateflag INTEGER,
    last_release TIMESTAMP WITHOUT TIME ZONE,
    last_activity TIMESTAMP WITHOUT TIME ZONE,
    updated TIMESTAMP WITH TIME ZONE,
    last_update_dice TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (pk)
);

CREATE INDEX ix_group_gid ON "group" (gid);

CREATE INDEX ix_group_short ON "group" (short);

CREATE TABLE anime_relation (
    pk BIGSERIAL NOT NULL,
    anime_pk BIGINT NOT NULL,
    related_aid BIGINT NOT NULL,
    relation_type anime_relation_type_enum NOT NULL,
    PRIMARY KEY (pk),
    FOREIGN KEY(anime_pk) REFERENCES anime (pk)
);

CREATE TABLE group_relation (
    pk BIGSERIAL NOT NULL,
    group_pk BIGINT NOT NULL,
    related_gid BIGINT NOT NULL,
    relation_type group_relation_type_enum NOT NULL,
    PRIMARY KEY (pk),
    FOREIGN KEY(group_pk) REFERENCES "group" (pk)
);
