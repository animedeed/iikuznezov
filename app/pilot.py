"""Run the integrated worker for one configured test profile."""

from argparse import ArgumentParser
from datetime import datetime
from json import loads
from logging import basicConfig, getLogger
from os import environ
from time import sleep
from urllib.parse import urlsplit

import httpx

from app.adapters.model import YandexAIStudioModelPort
from app.calendar.caldav_adapter import CalDavCalendar
from app.calendar.ports import CalendarPermanentError, CalendarTemporaryError
from app.config import Settings
from app.contracts import Contract, Profile, Ref, Route, TelegramActor
from app.core.approvals import ApprovalService
from app.core.mail_polling import MailPolling
from app.core.pilot import PilotWorker
from app.core.store import Store
from app.mail.config import SmtpSettings
from app.mail.imap import ImapSettings
from app.mail.routing import MailRoutingContext
from app.mail.smtp import SmtpEmailTransport, recipient_fingerprint
from app.telegram.bot import TelegramAPI, TelegramError
from app.telegram.channel import TelegramChannel
from app.telegram.inbox import UpdateInbox
from app.workflow.plan import build_plan
from app.workflow.tasks import SqliteTaskStore
from app.workflow.transport import WorkflowTransport


class PilotProfile(Contract):
    profile: Profile
    mail_channel: Ref
    telegram_user_id: int
    calendar_name: Ref
    mail_targets: dict[Route, Ref] = {}
    auto_forward_quotes: bool = False
    protected_intervals: tuple[tuple[datetime, datetime], ...] = ()


def main() -> None:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("profile_config", help="Private JSON file with profile and routing configuration")
    args = parser.parse_args()
    basicConfig(level="INFO", format="%(levelname)s %(name)s %(message)s")
    # HTTP request logs include the Telegram bot URL.
    getLogger("httpx").setLevel("WARNING")
    try:
        with open(args.profile_config, encoding="utf-8") as source:
            config = PilotProfile.model_validate(loads(source.read()))
        settings = Settings.from_env()
        mail = SmtpSettings.from_env()
        required = ("TELEGRAM_BOT_TOKEN", "PILOT_CALDAV_URL", "PILOT_CALDAV_USER", "PILOT_CALDAV_PASSWORD")
        supplied = [environ.get(key, "").strip() for key in required]
        if not all(supplied) or not mail or not settings.model_folder_id or not settings.model_api_key:
            raise ValueError("Set pilot Telegram, CalDAV, SMTP/IMAP and Yandex AI Studio environment variables")
        token, calendar_url, calendar_user, calendar_password = supplied
        parsed_url = urlsplit(calendar_url)
        if parsed_url.scheme != "https" or not parsed_url.hostname or parsed_url.username:
            raise ValueError("PILOT_CALDAV_URL must be an HTTPS URL without embedded credentials")
        if config.telegram_user_id <= 0:
            raise ValueError("Use a positive Telegram ID")
        if any(
            start.utcoffset() is None or end.utcoffset() is None or start >= end
            for start, end in config.protected_intervals
        ):
            raise ValueError("Protected intervals must have time zones and increasing start/end times")
        fingerprints = {ref: recipient_fingerprint(address) for ref, address in mail.recipients.items()}
        if set(config.mail_targets.values()) - fingerprints.keys():
            raise ValueError("All mail_targets must exist in MAIL_RECIPIENTS")

        with httpx.Client(timeout=35) as client:
            api = TelegramAPI(token, client)
            bot = api.call("getMe")
            if api.call("getWebhookInfo")["url"]:
                raise ValueError("Use a dedicated bot without an active webhook")
            _run(config, settings, mail, fingerprints, api, bot, client, calendar_url, calendar_user, calendar_password)
    except (OSError, PermissionError, TelegramError) as error:
        parser.exit(1, f"{error}\n")
    except ValueError as error:
        # Pydantic errors can contain private configuration values.
        summary = str(error) if type(error) is ValueError else "Invalid pilot profile configuration"
        parser.exit(1, f"{summary}\n")
    except KeyboardInterrupt:
        return


def _run(config, settings, mail, fingerprints, api, bot, client, calendar_url, calendar_user, calendar_password):
    profile = config.profile
    profile_id = profile.profile_id
    store = Store(settings.database_path)
    store.initialize()
    store.seed_profiles(((profile, config.mail_channel, config.telegram_user_id),))
    approvals = ApprovalService(store)
    tasks = SqliteTaskStore(store)
    calendar = CalDavCalendar(
        url=calendar_url, username=calendar_user, password=calendar_password,
        calendars={profile_id: config.calendar_name}, timezones={profile_id: profile.timezone},
    )
    workflow = WorkflowTransport(calendar, tasks, protected={profile_id: config.protected_intervals})

    def profiles_for(user_id):
        with store._connection() as db:
            authorized = db.execute(
                "SELECT 1 FROM bindings WHERE kind='telegram' AND active=1 AND profile_id=? AND channel_id=?",
                (profile_id, str(user_id)),
            ).fetchone()
        return {profile_id: profile_id} if authorized else {}

    def chat_for(requested_profile: str, actor: TelegramActor):
        if requested_profile != profile_id:
            return None
        user_id = actor.telegram_user_id
        if profiles_for(user_id):
            return user_id
        with store._connection() as db:
            acting_initiator = db.execute(
                """SELECT 1 FROM approvals WHERE profile_id=? AND approver_telegram_id=?
                AND approver_ref='meeting-initiator' AND state IN ('pending_approval','approved')""",
                (profile_id, user_id),
            ).fetchone()
        return user_id if acting_initiator else None

    def load_plan(requested_profile, day, period):
        if requested_profile != profile_id:
            raise LookupError("Unknown profile")
        try:
            return build_plan(profile_id, day, period, store=store, calendar=calendar, tasks=tasks)
        except (CalendarTemporaryError, CalendarPermanentError) as error:
            raise LookupError("Calendar is unavailable") from error

    channel = TelegramChannel(
        approvals, api, bot_id=bot["id"], bot_username=bot["username"],
        context_db=store.path, chat_for=chat_for, profiles_for=profiles_for, build_plan=load_plan,
        week_ends_sunday=True,
    )
    inbox = UpdateInbox(store.path, api, bot["id"])
    model = YandexAIStudioModelPort(
        settings.model_api_key, settings.model_folder_id, model=settings.model_name, client=client,
    )
    routing = MailRoutingContext(
        profile, targets=config.mail_targets, automatic_routes=frozenset({Route.FORWARD_SALES}),
        automation_enabled=config.auto_forward_quotes,
    )
    mail_polling = MailPolling(
        store, ImapSettings(config.mail_channel, mail.username, mail.password), routing,
        recipient_fingerprints=fingerprints, model=model,
    )
    worker = PilotWorker(
        store, profile_id, channel, workflow,
        smtp=SmtpEmailTransport(mail, store=store, require_recipient_fingerprint=True),
        mail=mail_polling, model=model, recipient_fingerprints=fingerprints, bot_username=bot["username"],
    )
    while True:
        worker.tick()
        inbox.poll_once(worker.handle_update)
        sleep(1)


if __name__ == "__main__":
    main()
