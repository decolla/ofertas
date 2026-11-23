import json
from datetime import date

import pytest

from ofertas.criteria import with_verdict
from ofertas.models import Decision, RawOffer, Verdict
from ofertas.schema import validate
from ofertas.storage import Storage
from ofertas.triage import ITEM_SCHEMA, Triage, TriageError, build_prompt, parse_result
from tests.conftest import fixture_text

DAY = date(2026, 11, 23)


def stored(db, n):
    return [
        db.upsert_offer(RawOffer(source="promobit", source_id=str(i), title=f"Produto {i}",
                                 price=100.0 + i, url=f"https://x/{i}", store="Amazon",
                                 list_price=300.0, extra={"tags": ["APP"]}))
        for i in range(1, n + 1)
    ]


class FakeCLI:
    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def ok_result(ids, score=85):
    return {"subtype": "success", "is_error": False, "total_cost_usd": 0.001,
            "structured_output": {"items": [
                {"id": str(i), "category": "cozinha", "name_norm": f"P{i}", "condition": "novo",
                 "fake": False, "flags": [], "score": score, "reason": "ok"} for i in ids]}}


# -- schema / parsing ------------------------------------------------------
def test_parse_result_keeps_valid_drops_invalid_and_unknown():
    data = json.loads(fixture_text("claude_result.json"))
    v = parse_result(data, {"1", "2", "3", "4"}, "haiku")
    assert set(v) == {"1", "2", "3"}
    assert v["1"].score == 88 and v["1"].category == "cozinha"
    assert v["2"].fake and "preco_de_inflado" in v["2"].flags


def test_parse_result_falls_back_to_result_text():
    data = {"subtype": "success", "is_error": False,
            "result": json.dumps(ok_result([1])["structured_output"])}
    assert parse_result(data, {"1"}, "haiku")["1"].score == 85


@pytest.mark.parametrize("data", [
    {"subtype": "error_max_turns", "is_error": True},
    {"subtype": "success", "is_error": False, "result": "não sei"},
])
def test_parse_result_errors(data):
    with pytest.raises(TriageError):
        parse_result(data, {"1"}, "haiku")


def test_schema_validator_catches_types_and_extra_fields():
    item = {"id": "1", "category": "casa", "name_norm": "x", "condition": "novo", "fake": "no",
            "flags": [], "score": 50, "reason": "r", "extra": 1}
    errs = validate(item, ITEM_SCHEMA)
    assert any("fake" in e for e in errs) and any("extra" in e for e in errs)
    assert validate({**item, "fake": False, "score": True}, ITEM_SCHEMA)  # bool is not int


def test_prompt_contains_only_collected_data():
    db = Storage(":memory:")
    p = build_prompt(stored(db, 1))
    payload = json.loads(p.split("\n", 1)[1])
    assert payload[0]["id"] == "1" and payload[0]["declared_discount_pct"] == 66
    assert payload[0]["tags"] == ["APP"]


# -- batching & budget -------------------------------------------------------
def test_batches_and_counts_calls():
    db = Storage(":memory:")
    offers = stored(db, 5)
    cli = FakeCLI([ok_result([1, 2]), ok_result([3, 4]), ok_result([5])])
    t = Triage(cli, db, daily_limit=10, batch_size=2, today=lambda: DAY)
    assert set(t.judge(offers)) == {1, 2, 3, 4, 5}
    assert len(cli.prompts) == 3 and db.claude_calls(DAY) == 3


def test_daily_limit_stops_and_alerts_once():
    db = Storage(":memory:")
    offers = stored(db, 6)
    alerts = []
    cli = FakeCLI([ok_result([1, 2]), ok_result([3, 4])])
    t = Triage(cli, db, daily_limit=2, batch_size=2, today=lambda: DAY, on_limit=alerts.append)
    assert set(t.judge(offers)) == {1, 2, 3, 4}
    t.judge(offers[4:])
    assert len(alerts) == 1 and "Limite diário de 2" in alerts[0]
    assert db.claude_calls(DAY) == 2


def test_failed_call_counts_but_does_not_crash():
    db = Storage(":memory:")
    cli = FakeCLI([TriageError("timeout"), ok_result([3])])
    t = Triage(cli, db, daily_limit=10, batch_size=2, today=lambda: DAY)
    assert set(t.judge(stored(db, 3))) == {3}
    assert db.claude_calls(DAY) == 2


def test_verdict_cache_roundtrip():
    db = Storage(":memory:")
    (o,) = stored(db, 1)
    v = Verdict(score=90, fake=False, flags=["cupom_escondido"], category="casa",
                name_norm="X", condition="novo", reason="r")
    db.save_verdict(o.id, o.raw.price, v)
    assert db.get_verdict(o.id, o.raw.price) == v
    assert db.get_verdict(o.id, o.raw.price - 10) is None  # new price => new verdict


# -- criterion with verdict --------------------------------------------------
OK = Decision(ok=True, reasons=["desconto declarado 50%"], insufficient_history=True)


def V(**kw):
    base = dict(score=85, fake=False, flags=[], category="cozinha", name_norm="X",
                condition="novo", reason="r")
    return Verdict(**{**base, **kw})


@pytest.mark.parametrize("verdict,ok,why", [
    (V(), True, "nota 85"),
    (V(condition="desconhecido"), True, "nota 85"),
    (V(score=79), False, "nota 79 < 80"),
    (V(fake=True, flags=["preco_de_inflado"]), False, "desconto falso (preco_de_inflado)"),
    (V(condition="recondicionado"), False, "produto recondicionado"),
    (V(category="outros"), False, "categoria desativada: outros"),
])
def test_with_verdict(verdict, ok, why):
    d = with_verdict(OK, verdict, 80, ["cozinha", "casa"])
    assert d.ok is ok and why in d.reasons


def test_without_verdict_is_pending():
    d = with_verdict(OK, None, 80, None)
    assert not d.ok and d.pending
