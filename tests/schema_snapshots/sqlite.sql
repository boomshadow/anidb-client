CREATE TABLE anime (
    pk INTEGER NOT NULL,
    aid INTEGER NOT NULL,
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
    ann_id INTEGER,
    allcinema_id INTEGER,
    animenfo_id VARCHAR(64),
    anidb_updated DATETIME NOT NULL,
    special_count INTEGER NOT NULL,
    credit_count INTEGER NOT NULL,
    other_count INTEGER NOT NULL,
    trailer_count INTEGER NOT NULL,
    parody_count INTEGER NOT NULL,
    updated DATETIME NOT NULL,
    last_update_dice DATETIME NOT NULL,
    PRIMARY KEY (pk),
    UNIQUE (aid)
);

CREATE TABLE episode (
    pk INTEGER NOT NULL,
    aid INTEGER NOT NULL,
    eid INTEGER NOT NULL,
    length INTEGER NOT NULL,
    rating FLOAT,
    votes INTEGER NOT NULL,
    epno VARCHAR(8) NOT NULL,
    title_eng VARCHAR(512),
    title_romaji VARCHAR(512),
    title_kanji VARCHAR(512),
    aired DATE,
    type VARCHAR(7) NOT NULL,
    updated DATETIME NOT NULL,
    last_update_dice DATETIME NOT NULL,
    PRIMARY KEY (pk)
);

CREATE INDEX ix_episode_aid ON episode (aid);

CREATE UNIQUE INDEX ix_episode_eid ON episode (eid);

CREATE TABLE file (
    pk INTEGER NOT NULL,
    path VARCHAR(512),
    size INTEGER,
    ed2khash VARCHAR(64),
    mtime DATETIME,
    aid INTEGER NOT NULL,
    gid INTEGER,
    eid INTEGER NOT NULL,
    fid INTEGER,
    is_deprecated BOOLEAN,
    is_generic BOOLEAN NOT NULL,
    part INTEGER,
    crc_ok BOOLEAN,
    file_version INTEGER,
    censored BOOLEAN,
    length_in_seconds INTEGER,
    description VARCHAR(512),
    aired_date DATE,
    mylist_state VARCHAR(7),
    mylist_filestate VARCHAR(29),
    mylist_viewed BOOLEAN,
    mylist_viewdate DATETIME,
    mylist_storage VARCHAR(128),
    mylist_source VARCHAR(128),
    mylist_other VARCHAR(128),
    lid INTEGER,
    updated DATETIME,
    last_update_dice DATETIME NOT NULL,
    PRIMARY KEY (pk)
);

CREATE INDEX ix_file_aid ON file (aid);

CREATE INDEX ix_file_eid ON file (eid);

CREATE INDEX ix_file_fid ON file (fid);

CREATE TABLE "group" (
    pk INTEGER NOT NULL,
    gid INTEGER,
    rating INTEGER,
    votes INTEGER,
    acount INTEGER,
    fcount INTEGER,
    name VARCHAR(248) NOT NULL,
    short VARCHAR(64) NOT NULL,
    irc_channel VARCHAR(32),
    irc_server VARCHAR(32),
    url VARCHAR(248),
    picname VARCHAR(32),
    founded DATETIME,
    disbanded DATETIME,
    dateflag INTEGER,
    last_release DATETIME,
    last_activity DATETIME,
    updated DATETIME,
    last_update_dice DATETIME NOT NULL,
    PRIMARY KEY (pk)
);

CREATE INDEX ix_group_gid ON "group" (gid);

CREATE INDEX ix_group_short ON "group" (short);

CREATE TABLE anime_relation (
    pk INTEGER NOT NULL,
    anime_pk INTEGER NOT NULL,
    related_aid INTEGER NOT NULL,
    relation_type VARCHAR(19) NOT NULL,
    PRIMARY KEY (pk),
    FOREIGN KEY(anime_pk) REFERENCES anime (pk)
);

CREATE TABLE group_relation (
    pk INTEGER NOT NULL,
    group_pk INTEGER NOT NULL,
    related_gid INTEGER NOT NULL,
    relation_type VARCHAR(14) NOT NULL,
    PRIMARY KEY (pk),
    FOREIGN KEY(group_pk) REFERENCES "group" (pk)
);
