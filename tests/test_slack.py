import json
from decimal import Decimal
from urllib.parse import urlencode

from slack_bolt import App, BoltRequest
from slack_bolt.authorization import AuthorizeResult

from finsql import slack_app


def fake_authorize(**_):
    # Offline: never call Slack's auth.test from tests
    return AuthorizeResult(enterprise_id=None, team_id="T1", bot_token="xoxb-test", bot_user_id="UBOT", bot_id="B1")


def make_app():
    app = App(signing_secret="secret", authorize=fake_authorize, request_verification_enabled=False,
              process_before_response=True)
    slack_app.register(app)
    return app


def command(text, user="U1", channel="C1"):
    body = urlencode({"command": "/finsql", "text": text, "user_id": user, "channel_id": channel,
                      "team_id": "T1", "response_url": "https://hooks.slack.com/x", "trigger_id": "1"})
    return BoltRequest(body=body, headers={"content-type": ["application/x-www-form-urlencoded"]})


def test_slash_command_acks_immediately_and_runs_agent_lazily(monkeypatch):
    calls = []
    monkeypatch.setattr(slack_app, "handle_question", lambda *a, **k: calls.append(a))
    resp = make_app().dispatch(command("net revenue last quarter"))
    assert resp.status == 200 and "Working on" in resp.body


def test_empty_command_shows_usage():
    resp = make_app().dispatch(command(""))
    assert "Ask me about finance data" in resp.body


def test_access_list_enforced(monkeypatch):
    monkeypatch.setattr(slack_app.config, "ALLOWED_SLACK_USERS", {"U_CFO"})
    resp = make_app().dispatch(command("revenue", user="U_INTERN"))
    assert "not on the FinSQL access list" in resp.body


def test_slack_retries_are_dropped():
    req = command("revenue")
    req.headers["x-slack-retry-num"] = ["1"]
    resp = make_app().dispatch(req)
    assert resp.status == 200 and resp.body == ""


def test_blocks_and_csv():
    result = {"status": "answered", "report": "*Revenue is up.*", "attempts": 2, "rows": [("EMEA", Decimal("10.5"))],
              "columns": ["region", "net_revenue"], "truncated": False,
              "trace": [{"step": "validate", "ok": True, "tables": ["fct_invoices"]}]}
    blocks = slack_app.build_blocks(result, 4.2)
    meta = blocks[1]["elements"][0]["text"]
    assert "2 attempt(s)" in meta and "1 rows" in meta and "fct_invoices" in meta
    assert slack_app.to_csv(result["columns"], result["rows"]).splitlines() == ["region,net_revenue", "EMEA,10.5"]
    json.dumps(blocks)  # must be serialisable for the Slack API
