from datetime import datetime, timedelta, timezone

import pytest

from ofertas.dedupe import is_duplicate, should_resend
from ofertas.models import RawOffer
from ofertas.storage import Storage

T0 = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


def offer(price=100.0, sid="1", title="Air Fryer Mondial 4L", url="https://x.com/p/1", source="promobit"):
    return RawOffer(source=source, source_id=sid, title=title, price=price, url=url, store="Amazon")


@pytest.fixture
def db():
    s = Storage(":memory:")
    yield s
    s.close()


@pytest.mark.parametrize(
    "new,last,expected",
    [(100, None, True), (100, 100, False), (96, 100, False), (95, 100, True), (80, 100, True)],
)
def test_should_resend(new, last, expected):
    assert should_resend(new, last, 0.05) is expected


def test_not_duplicate_when_never_sent(db):
    o = db.upsert_offer(offer(), T0)
    assert is_duplicate(db, o, 72, 0.05, T0) == (False, None)


def test_duplicate_within_window(db):
    o = db.upsert_offer(offer(), T0)
    db.mark_sent(o, 1, T0)
    dup, why = is_duplicate(db, o, 72, 0.05, T0 + timedelta(hours=10))
    assert dup and "já enviada" in why


def test_not_duplicate_after_window(db):
    o = db.upsert_offer(offer(), T0)
    db.mark_sent(o, 1, T0)
    assert is_duplicate(db, o, 72, 0.05, T0 + timedelta(hours=73))[0] is False


def test_resend_when_price_dropped_enough(db):
    o = db.upsert_offer(offer(price=100), T0)
    db.mark_sent(o, 1, T0)
    cheaper = db.upsert_offer(offer(price=90), T0 + timedelta(hours=1))
    assert is_duplicate(db, cheaper, 72, 0.05, T0 + timedelta(hours=1))[0] is False


def test_same_product_from_other_source_is_duplicate(db):
    a = db.upsert_offer(offer(title="SSD Kingston NV3 1TB"), T0)
    db.mark_sent(a, 1, T0)
    b = db.upsert_offer(
        offer(source="gatry", sid="g1", title="Kingston SSD NV3 1TB", url="https://gatry.com/p/g1"), T0
    )
    assert is_duplicate(db, b, 72, 0.05, T0 + timedelta(hours=2))[0] is True


def test_upsert_tracks_new_and_price_change(db):
    first = db.upsert_offer(offer(price=100), T0)
    same = db.upsert_offer(offer(price=100), T0 + timedelta(minutes=20))
    changed = db.upsert_offer(offer(price=89.9), T0 + timedelta(minutes=40))
    assert (first.is_new, first.price_changed) == (True, True)
    assert (same.is_new, same.price_changed) == (False, False)
    assert (changed.is_new, changed.price_changed) == (False, True)
    assert first.id == same.id == changed.id


def test_price_history_not_flooded_by_repeated_runs(db):
    for i in range(10):
        db.upsert_offer(offer(price=100), T0 + timedelta(minutes=20 * i))
    db.upsert_offer(offer(price=95), T0 + timedelta(hours=4))
    db.upsert_offer(offer(price=95), T0 + timedelta(hours=30))
    prices = db.price_history(db.upsert_offer(offer(price=95), T0).product_key, T0 - timedelta(days=1))
    assert prices == [100, 95, 95]
