"""Tests for when the cache decides to refresh itself.

This is the single most consequential piece of logic in the library: it governs
every request that reaches AniDB, and AniDB bans clients that ask too often. Too
eager and a large collection gets its owner banned; too lazy and the cache never
updates. It had no tests at all.

The rules, as implemented:

* Never refresh anything twice in one day.
* Never re-roll the dice more than once in 20 hours, so a daily cron job gets one
  roll per run rather than one per attribute access.
* Past that, roll a die whose odds grow with the age of the data -- 2% in the
  second week, then about half again each week after.
* Anime adds up to 30% on top, scaled by how soon after AniDB's own last update
  the data was fetched.

Time is controlled by *setting the stored timestamps*, not by freezing the clock.
That is the honest way round: these are functions of what is in the database, and
the database is an input the tests own. Freezing time would test the same thing
less directly and pull in a dependency to do it.
"""

import datetime

import pytest

from tests import factories

UTC = datetime.UTC
DAY = datetime.timedelta(days=1)
WEEK = datetime.timedelta(weeks=1)
HOUR = datetime.timedelta(hours=1)


def ago(**kwargs):
    return datetime.datetime.now(UTC) - datetime.timedelta(**kwargs)


@pytest.fixture
def never_refreshes(monkeypatch):
    """Make the dice always lose, so 'did it decide to refresh' is deterministic."""
    import anidb_client.animeobjs

    monkeypatch.setattr(anidb_client.animeobjs.random, "randint", lambda a, b: 101)


@pytest.fixture
def always_refreshes(monkeypatch):
    """Make the dice always win."""
    import anidb_client.animeobjs

    monkeypatch.setattr(anidb_client.animeobjs.random, "randint", lambda a, b: 0)


def seed(session, **kwargs):
    session.add(factories.make_anime(aid=6187, **kwargs))
    session.commit()


class TestTheDailyFloor:
    def test_data_fetched_today_is_never_refreshed(self, anidb, session, link, always_refreshes):
        """The hard floor. Even with the dice rigged to win, nothing is sent.

        This is what stops an application that touches a thousand files in a loop
        from sending a thousand requests.
        """
        seed(session, updated=ago(hours=2), last_update_dice=ago(days=30))
        anidb.Anime(6187).update_if_old()

        assert link.requests == []

    def test_data_from_just_under_a_day_ago_is_not_refreshed(self, anidb, session, link, always_refreshes):
        seed(session, updated=ago(hours=23), last_update_dice=ago(days=30))
        anidb.Anime(6187).update_if_old()

        assert link.requests == []


class TestTheDiceCooldown:
    def test_the_dice_are_not_re_rolled_within_20_hours(self, anidb, session, link, always_refreshes):
        """Rolling on every attribute access would make the odds meaningless.

        20 hours is chosen so a daily cron job still gets a roll each run.
        """
        seed(session, updated=ago(weeks=5), last_update_dice=ago(hours=19))
        anidb.Anime(6187).update_if_old()

        assert link.requests == []

    def test_the_dice_are_re_rolled_after_20_hours(self, anidb, session, link, always_refreshes):
        """block=True on purpose.

        update_if_old() defaults to block=False, which dispatches the fetch on a
        thread it does not join -- so asserting straight afterwards is a race, and
        one that only lost under coverage's slower execution. Blocking makes the
        assertion deterministic rather than usually-true.
        """
        from tests.objectlayer import FakeResponse

        link.on("ANIME", FakeResponse("230", datalines=[{"aid": "6187", "year": "2009"}]))
        seed(session, updated=ago(weeks=5), last_update_dice=ago(hours=21))
        anidb.Anime(6187).update_if_old(block=True)

        assert link.commands() == ["ANIME"]

    def test_rolling_the_dice_records_that_it_happened(self, anidb, session, link, never_refreshes):
        """The timestamp has to be written even when the roll loses.

        Otherwise the cooldown never starts and every access rolls again.
        """
        seed(session, updated=ago(weeks=5), last_update_dice=ago(days=2))
        anime = anidb.Anime(6187)
        anime.update_if_old()

        assert link.requests == []
        with anidb.get_session() as check:
            from anidb_client.db import AnimeTable

            stored = check.query(AnimeTable).one()
            assert datetime.datetime.now(UTC) - stored.last_update_dice.replace(tzinfo=UTC) < HOUR


class TestTheOddsGrowWithAge:
    @pytest.mark.parametrize(
        ("age_weeks", "expected"),
        [(1, 2), (2, 3), (3, 5), (4, 8), (5, 12)],
    )
    def test_the_base_probability_by_age(self, anidb, session, monkeypatch, age_weeks, expected):
        """2% after one week, then ceil(x1.5) for each further week.

        The sequence is 2, 3, 5, 8, 12, 18, ... and the age in whole weeks selects
        the term. Derived from the rule rather than recorded from a run, so a
        change to the rule fails here instead of being rubber-stamped.
        """
        rolls = []

        import anidb_client.animeobjs

        def spy(low, high):
            rolls.append(("roll", low, high))
            return 101

        monkeypatch.setattr(anidb_client.animeobjs.random, "randint", spy)

        # anidb_updated far in the past, so Anime's own bonus contributes nothing.
        seed(
            session,
            updated=ago(weeks=age_weeks),
            last_update_dice=ago(days=2),
            anidb_updated=datetime.datetime.now() - datetime.timedelta(weeks=52),
        )
        anime = anidb.Anime(6187)
        assert anime._probability_of_refresh() == expected

    def test_the_probability_is_capped_at_100(self, anidb, session):
        seed(
            session,
            updated=ago(weeks=52),
            last_update_dice=ago(days=2),
            anidb_updated=datetime.datetime.now() - datetime.timedelta(weeks=104),
        )
        assert anidb.Anime(6187)._probability_of_refresh() == 100


class TestAnimeSpecificBonus:
    def test_data_fetched_right_after_anidb_updated_it_gets_the_full_bonus(self, anidb, session):
        """If AniDB had just changed the record when we fetched it, it is likely
        to change again soon -- so 30% is added on top."""
        now = datetime.datetime.now()
        seed(session, updated=ago(weeks=1), last_update_dice=ago(days=2), anidb_updated=now)

        assert anidb.Anime(6187)._extra_refresh_probability() == 30

    @pytest.mark.parametrize(
        ("weeks_between", "expected"),
        [(0, 30), (1, 20), (2, 10), (3, 0), (10, 0)],
    )
    def test_the_bonus_decays_by_ten_points_a_week(self, anidb, session, weeks_between, expected):
        now = datetime.datetime.now()
        seed(
            session,
            updated=datetime.datetime.now(UTC),
            last_update_dice=ago(days=2),
            anidb_updated=now - datetime.timedelta(weeks=weeks_between),
        )
        assert anidb.Anime(6187)._extra_refresh_probability() == expected

    def test_the_bonus_never_goes_negative(self, anidb, session):
        seed(
            session,
            updated=datetime.datetime.now(UTC),
            last_update_dice=ago(days=2),
            anidb_updated=datetime.datetime.now() - datetime.timedelta(weeks=100),
        )
        assert anidb.Anime(6187)._extra_refresh_probability() == 0


class TestNoCachedData:
    def test_an_object_with_no_cached_row_fetches_immediately(self, anidb, link, never_refreshes):
        """No data means no dice: there is nothing to be fresh or stale.

        The dice are rigged to lose, and it fetches anyway -- that is the point.
        """
        from tests.objectlayer import FakeResponse

        link.on("ANIME", FakeResponse("230", datalines=[{"aid": "6187", "year": "2009"}]))
        anidb.Anime(6187).update_if_old()

        assert link.commands() == ["ANIME"]

    def test_a_fetch_that_finds_nothing_marks_the_object_illegal(self, anidb, link, never_refreshes):
        """And does so without hanging -- the failure mode fixed in the last MR."""
        anime = anidb.Anime(6187)
        with pytest.raises(anidb.errors.IllegalAnimeObject):
            anime.update_if_old()
