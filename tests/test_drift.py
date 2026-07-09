import time

from genie_autopilot.drift import Correction, parse_correction, score_proposals


def test_parse_means():
    assert parse_correction("liquid assets means liquid_cash_assets in wealth portfolios") == (
        "liquid assets",
        "liquid_cash_assets",
    )


def test_parse_refers_to():
    assert parse_correction("Liquid Assets refers to fact_wealth_portfolios.liquid_cash_assets") == (
        "liquid assets",
        "fact_wealth_portfolios.liquid_cash_assets",
    )


def test_parse_is_on():
    assert parse_correction("available balance is on fact_transactions.available_balance") == (
        "available balance",
        "fact_transactions.available_balance",
    )


def test_parse_rejects_noise():
    assert parse_correction("this answer is wrong") is None
    assert parse_correction("") is None


def _corr(term, entity, user, role="wealth_advisor", age_s=0):
    return Correction(term=term, entity=entity, user=user, role=role, ts=time.time() - age_s)


def test_two_users_beats_one_loud_user():
    now = time.time()
    two_users = score_proposals(
        [_corr("liquid assets", "liquid_cash_assets", "u1"), _corr("liquid assets", "liquid_cash_assets", "u2")],
        now=now,
    )[0]
    one_user = score_proposals(
        [
            _corr("gross margin", "net_margin", "u9"),
            _corr("gross margin", "net_margin", "u9"),
            _corr("gross margin", "net_margin", "u9"),
        ],
        now=now,
    )[0]
    assert two_users.distinct_users == 2
    assert one_user.distinct_users == 1
    assert two_users.confidence > one_user.confidence


def test_freshness_decay():
    now = time.time()
    fresh = score_proposals(
        [_corr("a", "b", "u1"), _corr("a", "b", "u2")], now=now
    )[0]
    stale = score_proposals(
        [_corr("a", "b", "u1", age_s=30 * 24 * 3600), _corr("a", "b", "u2", age_s=30 * 24 * 3600)],
        now=now,
    )[0]
    assert fresh.confidence > stale.confidence
