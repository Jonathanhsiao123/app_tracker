"""Notifications (spec 10). No network: httpx.MockTransport stands in for ntfy.sh."""

from datetime import UTC, datetime

import httpx
import pytest

from jobmon import notify
from jobmon.models import Analysis, Posting

TOPIC = "jobmon-test-topic"


@pytest.fixture(autouse=True)
def topic(monkeypatch):
    monkeypatch.setenv("JOBMON_NTFY_TOPIC", TOPIC)


def client_capturing(captured, status=200):
    def handler(request):
        captured.append(request)
        return httpx.Response(status)

    return httpx.Client(transport=httpx.MockTransport(handler))


def make_posting(company="Acme", title="Software Engineer, New Grad"):
    now = datetime.now(UTC)
    return Posting(
        global_id=f"greenhouse:{company.lower()}:1", source="greenhouse", company=company,
        title=title, locations=["NYC", "SF"], description_html="", description_text="",
        apply_url="https://boards.greenhouse.io/acme/jobs/1",
        first_seen_at=now, last_seen_at=now, remote_type="hybrid",
    )


def make_analysis(global_id="greenhouse:acme:1", **kw):
    base = {
        "global_id": global_id, "role_class": "new_grad", "function": ["swe"],
        "eligibility": "unknown", "eligibility_evidence": [], "keywords": {}, "gaps": [],
        "recommended_variant": "generalist",
        "variant_scores": {"generalist": 0.7, "mle": 0.4},
    }
    base.update(kw)
    return Analysis(**base)


# --- refusing to run silently ------------------------------------------------------

def test_unset_topic_raises_rather_than_no_opping(monkeypatch):
    """A monitor that quietly stops notifying is worse than one that crashes."""
    monkeypatch.delenv("JOBMON_NTFY_TOPIC", raising=False)
    monkeypatch.setattr(notify, "NTFY_TOPIC", None)
    with pytest.raises(RuntimeError, match="refusing to silently drop"):
        notify.send_alert(httpx.Client(), make_posting(), make_analysis())


# --- the alert payload (spec 10) ---------------------------------------------------

def test_format_alert_carries_every_required_field():
    analysis = make_analysis(
        grad_window="must graduate between December 2026 and August 2027",
        eligibility="likely_ok",
        eligibility_evidence=["We welcome F-1 students on CPT or OPT."],
        keywords={"languages": ["python"], "infra": ["aws"]},
        gaps=["kubernetes"],
    )
    text = notify.format_alert(make_posting(), analysis)
    assert "Acme" in text and "Software Engineer, New Grad" in text
    assert "NYC, SF" in text and "hybrid" in text
    assert "December 2026" in text
    assert "likely_ok" in text
    assert "python" in text and "aws" in text
    assert "kubernetes" in text
    assert "generalist" in text


def test_evidence_sentence_is_quoted_verbatim():
    """The evidence is the whole reason the label is trustworthy (spec 6)."""
    sentence = "We are unable to sponsor or take over sponsorship of an employment visa."
    text = notify.format_alert(make_posting(), make_analysis(eligibility_evidence=[sentence]))
    assert f'"{sentence}"' in text


def test_apply_url_is_last_and_bare():
    """Last and bare so it is tappable on a phone lock screen."""
    text = notify.format_alert(make_posting(), make_analysis())
    assert text.splitlines()[-1] == "https://boards.greenhouse.io/acme/jobs/1"


def test_degree_gated_is_shown_not_hidden():
    text = notify.format_alert(make_posting(), make_analysis(degree_gated=True))
    assert "degree-gated" in text


def test_missing_location_does_not_render_an_empty_line():
    posting = make_posting()
    posting.locations = []
    posting.remote_type = None
    assert "location not stated" in notify.format_alert(posting, make_analysis())


# --- transport ---------------------------------------------------------------------

def test_send_alert_posts_to_the_topic_with_headers():
    captured = []
    ok = notify.send_alert(client_capturing(captured), make_posting(), make_analysis())
    assert ok is True
    request = captured[0]
    assert request.url.path == f"/{TOPIC}"
    assert request.headers["X-Title"] == "Jobmon Opening Workflow"
    assert request.headers["X-Click"] == "https://boards.greenhouse.io/acme/jobs/1"


def test_likely_ok_gets_a_higher_priority():
    captured = []
    notify.send_alert(client_capturing(captured), make_posting(),
                      make_analysis(eligibility="likely_ok"))
    assert captured[0].headers["X-Priority"] == "4"


def test_delivery_failure_returns_false_rather_than_raising():
    """False lets the runner leave `alerted_at` unset so the posting is retried."""
    assert notify.send_alert(client_capturing([], status=500), make_posting(),
                             make_analysis()) is False


def test_configured_email_is_sent_with_the_same_alert_body(monkeypatch):
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            sent.update(host=host, port=port, timeout=timeout)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def ehlo(self):
            pass

        def starttls(self):
            pass

        def login(self, username, password):
            sent.update(username=username, password=password)

        def send_message(self, message):
            sent["message"] = message

    monkeypatch.setenv("JOBMON_SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("JOBMON_SMTP_PORT", "587")
    monkeypatch.setenv("JOBMON_SMTP_USER", "user")
    monkeypatch.setenv("JOBMON_SMTP_PASSWORD", "password")
    monkeypatch.setenv("JOBMON_EMAIL_FROM", "monitor@example.test")
    monkeypatch.setenv("JOBMON_EMAIL_TO", "me@example.test")
    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)

    posting = make_posting()
    analysis = make_analysis()
    assert notify._send_email("subject", notify.format_alert(posting, analysis)) is True
    assert sent["message"]["From"] == "monitor@example.test"
    assert sent["message"]["To"] == "me@example.test"
    assert "boards.greenhouse.io/acme/jobs/1" in sent["message"].get_content()


# --- digest ------------------------------------------------------------------------

def test_digest_batches_all_items_with_required_subject_and_html_links():
    items = [(make_posting(company=f"C{i}"), make_analysis(f"g:{i}")) for i in range(22)]
    captured = []
    assert notify.send_digest(client_capturing(captured), items, "digest") is True
    body = captured[0].content.decode()
    assert "C21" in body
    assert captured[0].headers["X-Title"] == "Jobmon Opening Workflow"


def test_empty_digest_sends_hourly_heartbeat():
    captured = []
    assert notify.send_digest(client_capturing(captured), [], "digest") is True
    assert len(captured) == 1
    assert "No new eligible openings" in captured[0].content.decode()


def test_digest_puts_sponsorship_first_and_degree_gated_last():
    gated = (make_posting(company="Gated"), make_analysis("g:1", degree_gated=True))
    sponsors = (make_posting(company="Sponsors"), make_analysis("g:2", eligibility="likely_ok"))
    plain = (make_posting(company="Plain"), make_analysis("g:3"))
    captured = []
    notify.send_digest(client_capturing(captured), [gated, plain, sponsors], "digest")
    body = captured[0].content.decode()
    assert body.index("Sponsors") < body.index("Plain") < body.index("Gated")


# --- pipeline alert (build step 6.4) -----------------------------------------------

def test_pipeline_alert_is_visibly_distinct_from_a_job_alert():
    captured = []
    notify.send_pipeline_alert(client_capturing(captured), "greenhouse returned 0 postings")
    assert captured[0].headers["X-Title"].startswith("!!")
    assert captured[0].headers["X-Priority"] == "5"


# --- weekly summary ----------------------------------------------------------------

def test_weekly_summary_calls_out_silent_adapters():
    from jobmon.models import AdapterHealth

    health = [
        AdapterHealth("ats_public", datetime.now(UTC), True, 0, 0, 10),
        AdapterHealth("github_repos", datetime.now(UTC), True, 40, 2, 10),
    ]
    captured = []
    notify.send_weekly_summary(client_capturing(captured), {"new": 12}, health)
    body = captured[0].content.decode()
    assert "SILENT" in body and "ats_public" in body
    assert "github_repos" not in body.split("SILENT")[1]
