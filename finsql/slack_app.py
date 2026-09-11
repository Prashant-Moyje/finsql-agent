"""Slack front end: the /finsql slash command and @FinSQL mentions.

Slack needs an HTTP 200 within 3 seconds; an agent run takes 5-30s. Bolt's lazy
listeners solve this on Lambda: `ack` answers immediately, then Bolt invokes
the same function again asynchronously to run the agent and post the report.

Every question is written to the audit log (CloudWatch on Lambda) with the
Slack user, the SQL that ran and the outcome.
"""
import csv
import io
import json
import logging
import re
import time

from . import config

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("finsql")

STATUS_ICON = {"answered": ":white_check_mark:", "clarify": ":thinking_face:",
               "refused": ":no_entry:", "failed": ":warning:"}
USAGE = "Ask me about finance data, e.g. `/finsql What was net revenue by region last quarter?`"


def is_allowed(user: str, channel: str) -> bool:
    return ((not config.ALLOWED_SLACK_USERS or user in config.ALLOWED_SLACK_USERS)
            and (not config.ALLOWED_SLACK_CHANNELS or channel in config.ALLOWED_SLACK_CHANNELS))


def to_csv(columns: list[str], rows: list[tuple]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(columns)
    w.writerows(["" if v is None else v for v in r] for r in rows)
    return buf.getvalue()


def build_blocks(result: dict, elapsed_s: float) -> list[dict]:
    status = result.get("status", "failed")
    meta = [f"{STATUS_ICON.get(status, '')} {status}", f"{elapsed_s:.1f}s", f"{result.get('attempts', 0)} attempt(s)"]
    if result.get("rows") is not None and status == "answered":
        meta.append(f"{len(result['rows'])} rows" + (" (truncated)" if result.get("truncated") else ""))
    tables = next((t.get("tables") for t in result.get("trace", []) if t["step"] == "validate" and t.get("ok")), None)
    if tables:
        meta.append("tables: " + ", ".join(tables))
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": (result.get("report") or "(no report)")[:2900]}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": " · ".join(meta)}]},
    ]


def audit(user: str, channel: str, question: str, result: dict, elapsed_s: float) -> None:
    log.info(json.dumps({
        "event": "finsql_question", "user": user, "channel": channel, "question": question,
        "status": result.get("status"), "attempts": result.get("attempts"), "sql": result.get("checked_sql"),
        "rows": len(result.get("rows") or []), "errors": [h["error"] for h in result.get("history", [])],
        "seconds": round(elapsed_s, 2),
    }))


def handle_question(question: str, user: str, channel: str, client, thread_ts: str | None = None) -> None:
    from .graph import answer  # heavy import; keep the ack path fast

    t0 = time.perf_counter()
    result = answer(question, user=user)
    elapsed = time.perf_counter() - t0
    audit(user, channel, question, result, elapsed)

    msg = client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=result.get("report", "")[:3000],
                                  blocks=build_blocks(result, elapsed))
    ts = thread_ts or msg["ts"]
    if result.get("checked_sql"):  # transparency: finance can see (and forward) exactly what ran
        client.chat_postMessage(channel=channel, thread_ts=ts, text=f"SQL used:\n```{result['checked_sql']}```")
    if result.get("rows"):
        client.files_upload_v2(channel=channel, thread_ts=ts, filename="finsql_result.csv",
                               title=question[:80], content=to_csv(result["columns"], result["rows"]))


def register(app) -> None:
    def ack_command(ack, body):
        text = (body.get("text") or "").strip()
        if not text:
            return ack(USAGE)
        if not is_allowed(body["user_id"], body["channel_id"]):
            return ack("Sorry, you're not on the FinSQL access list. Ask the data team for access.")
        ack(f":mag: Working on: _{text}_")

    def run_command(body, client, respond):
        text = (body.get("text") or "").strip()
        if not text or not is_allowed(body["user_id"], body["channel_id"]):
            return
        try:
            handle_question(text, body["user_id"], body["channel_id"], client)
        except Exception as e:  # never leave the user hanging after the ack
            log.exception("finsql command failed")
            if "not_in_channel" in str(e) or "channel_not_found" in str(e):
                respond("Please invite me to this channel first: `/invite @FinSQL`")
            else:
                respond(":warning: Something went wrong answering that; the error has been logged.")

    def ack_event(ack):
        ack()

    def run_mention(event, client):
        text = re.sub(r"<@[^>]+>", "", event.get("text", "")).strip()
        channel, thread = event["channel"], event.get("thread_ts") or event["ts"]
        if not text:
            return client.chat_postMessage(channel=channel, thread_ts=thread, text=USAGE)
        if not is_allowed(event.get("user", ""), channel):
            return client.chat_postMessage(channel=channel, thread_ts=thread,
                                           text="Sorry, you're not on the FinSQL access list.")
        try:
            handle_question(text, event.get("user", ""), channel, client, thread_ts=thread)
        except Exception:
            log.exception("finsql mention failed")
            client.chat_postMessage(channel=channel, thread_ts=thread,
                                    text=":warning: Something went wrong; the error has been logged.")

    def skip_slack_retries(req, resp, next):
        # A Lambda cold start can exceed 3s, making Slack retry the same event.
        # The first delivery is already being handled, so drop the retry.
        if req.headers.get("x-slack-retry-num"):
            from slack_bolt import BoltResponse
            return BoltResponse(status=200, body="")
        next()

    app.use(skip_slack_retries)
    app.command("/finsql")(ack=ack_command, lazy=[run_command])
    app.event("app_mention")(ack=ack_event, lazy=[run_mention])


def create_app(**kwargs):
    from slack_bolt import App

    app = App(token=config.SLACK_BOT_TOKEN, signing_secret=config.SLACK_SIGNING_SECRET,
              process_before_response=True, **kwargs)
    register(app)
    return app


_lambda_handler = None


def handler(event, context):
    """AWS Lambda entry point (Function URL -> here)."""
    global _lambda_handler
    if _lambda_handler is None:
        from slack_bolt.adapter.aws_lambda import SlackRequestHandler

        _lambda_handler = SlackRequestHandler(create_app())
    return _lambda_handler.handle(event, context)


if __name__ == "__main__":
    # Local development over Socket Mode: no public URL or deployment needed.
    from slack_bolt import App
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    local = App(token=config.SLACK_BOT_TOKEN)
    register(local)
    print("FinSQL is connected to Slack over Socket Mode. Try /finsql in your workspace.")
    SocketModeHandler(local, config.SLACK_APP_TOKEN).start()
