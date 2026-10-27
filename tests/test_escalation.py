from survey_agent import escalation


def test_bot_trap_never_selected():
    assert escalation.is_bot_trap_option("ROBOT") is True
    assert escalation.is_bot_trap_option("select ROBOT here") is True
    assert escalation.is_bot_trap_option("Male") is False


def test_ask_once_dedupe(tmp_path):
    dd = tmp_path / "data"
    dd.mkdir()
    # first ask escalates
    ans, esc = escalation.ask_or_remember("Favorite color?", ["Red", "Blue"], "site", dd)
    assert ans is None and esc is True
    # second ask does not re-notify (pending exists) but still escalated
    ans2, esc2 = escalation.ask_or_remember("Favorite color?", ["Red", "Blue"], "site", dd)
    assert ans2 is None and esc2 is True
    assert len(escalation.pending(dd)) == 1


def test_bot_trap_question_not_escalated(tmp_path):
    dd = tmp_path / "data"
    dd.mkdir()
    ans, esc = escalation.ask_or_remember("Are you a robot?", ["ROBOT", "No"], "site", dd)
    assert ans is None and esc is False
    assert escalation.pending(dd) == []
