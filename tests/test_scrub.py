from __future__ import annotations

from app.jobs import REDACTED, Scrubber


def test_stored_secret_values_are_replaced() -> None:
    s = Scrubber(["wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "hunter22"])
    out = s.scrub("secret=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY pw=hunter22 done")
    assert out == f"secret={REDACTED} pw={REDACTED} done"


def test_access_key_ids_are_replaced_even_when_not_stored() -> None:
    s = Scrubber([])
    out = s.scrub("using AKIAIOSFODNN7EXAMPLE and ASIAJEXAMPLE12345678 here")
    assert "AKIA" not in out and "ASIA" not in out
    assert out.count(REDACTED) == 2


def test_long_base64_runs_are_replaced_but_short_ones_kept() -> None:
    s = Scrubber([])
    long = "A" * 150 + "b/+=" * 40  # 310 chars of base64 alphabet
    short = "QUJDREVGR0g=" * 5  # 60 chars
    out = s.scrub(f"token: {long} id: {short}")
    assert out == f"token: {REDACTED} id: {short}"
    # A `KEY=value` prefix is base64 alphabet too and is absorbed into the run: over-redaction is the safe side.
    assert s.scrub(f"AWS_SESSION_TOKEN={long}") == REDACTED


def test_short_or_empty_secret_values_are_ignored() -> None:
    s = Scrubber(["", "ab", "a"])
    assert s.scrub("nothing changes ab a") == "nothing changes ab a"


def test_longest_secret_wins_when_nested() -> None:
    s = Scrubber(["abcd", "abcdefgh"])
    assert s.scrub("x abcdefgh y") == f"x {REDACTED} y"


def test_plain_text_untouched() -> None:
    line = "aws_instance.pse: Creation complete after 12s [id=i-0123456789abcdef0]"
    assert Scrubber(["something-else-entirely"]).scrub(line) == line
