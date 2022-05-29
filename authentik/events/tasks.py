"""Event notification tasks"""
from django.db.models.query_utils import Q
from guardian.shortcuts import get_anonymous_user
from structlog.stdlib import get_logger

from authentik.core.models import User
from authentik.events.models import (
    Event,
    Notification,
    NotificationRule,
    NotificationTransport,
    NotificationTransportError,
)
from authentik.events.monitored_tasks import (
    MonitoredTask,
    TaskResult,
    TaskResultStatus,
    prefill_task,
)
from authentik.policies.engine import PolicyEngine
from authentik.policies.models import PolicyBinding, PolicyEngineMode
from authentik.root.celery import CELERY_APP

LOGGER = get_logger()


@CELERY_APP.task()
def event_notification_handler(event_uuid: str):
    """Start task for each trigger definition"""
    for trigger in NotificationRule.objects.all():
        event_trigger_handler.apply_async(args=[event_uuid, trigger.name], queue="authentik_events")


@CELERY_APP.task()
def event_trigger_handler(event_uuid: str, trigger_name: str):
    """Check if policies attached to NotificationRule match event"""
    events = Event.objects.filter(event_uuid=event_uuid)
    if not events.exists():
        LOGGER.warning("event doesn't exist yet or anymore", event_uuid=event_uuid)
        return
    event: Event = events.first()
    triggers: NotificationRule = NotificationRule.objects.filter(name=trigger_name)
    if not triggers.exists():
        return
    trigger = triggers.first()

    if "policy_uuid" in event.context:
        policy_uuid = event.context["policy_uuid"]
        if PolicyBinding.objects.filter(
            target__in=NotificationRule.objects.all().values_list("pbm_uuid", flat=True),
            policy=policy_uuid,
        ).exists():
            # If policy that caused this event to be created is attached
            # to *any* NotificationRule, we return early.
            # This is the most effective way to prevent infinite loops.
            LOGGER.debug("e(trigger): attempting to prevent infinite loop", trigger=trigger)
            return

    if not trigger.group:
        LOGGER.debug("e(trigger): trigger has no group", trigger=trigger)
        return

    LOGGER.debug("e(trigger): checking if trigger applies", trigger=trigger)
    try:
        user = User.objects.filter(pk=event.user.get("pk")).first() or get_anonymous_user()
    except User.DoesNotExist:
        LOGGER.warning("e(trigger): failed to get user", trigger=trigger)
        return
    policy_engine = PolicyEngine(trigger, user)
    policy_engine.mode = PolicyEngineMode.MODE_ANY
    policy_engine.empty_result = False
    policy_engine.use_cache = False
    policy_engine.request.context["event"] = event
    policy_engine.build()
    result = policy_engine.result
    if not result.passing:
        return

    LOGGER.debug("e(trigger): event trigger matched", trigger=trigger)
    # Create the notification objects
    for transport in trigger.transports.all():
        for user in trigger.group.users.all():
            LOGGER.debug("created notification")
            notification = Notification.objects.create(
                severity=trigger.severity, body=event.summary, event=event, user=user
            )
            notification_transport.apply_async(
                args=[notification.pk, transport.pk], queue="authentik_events"
            )
            if transport.send_once:
                break


@CELERY_APP.task(
    bind=True,
    autoretry_for=(NotificationTransportError,),
    retry_backoff=True,
    base=MonitoredTask,
)
def notification_transport(self: MonitoredTask, notification_pk: int, transport_pk: int):
    """Send notification over specified transport"""
    self.save_on_success = False
    try:
        notification: Notification = Notification.objects.filter(pk=notification_pk).first()
        if not notification:
            return
        transport = NotificationTransport.objects.filter(pk=transport_pk).first()
        if not transport:
            return
        transport.send(notification)
        self.set_status(TaskResult(TaskResultStatus.SUCCESSFUL))
    except NotificationTransportError as exc:
        self.set_status(TaskResult(TaskResultStatus.ERROR).with_error(exc))
        raise exc


@CELERY_APP.task()
def gdpr_cleanup(user_pk: int):
    """cleanup events from gdpr_compliance"""
    events = Event.objects.filter(user__pk=user_pk)
    LOGGER.debug("GDPR cleanup, removing events from user", events=events.count())
    events.delete()


@CELERY_APP.task(bind=True, base=MonitoredTask)
@prefill_task
def notification_cleanup(self: MonitoredTask):
    """Cleanup seen notifications and notifications whose event expired."""
    notifications = Notification.objects.filter(Q(event=None) | Q(seen=True))
    amount = notifications.count()
    for notification in notifications:
        notification.delete()
    LOGGER.debug("Expired notifications", amount=amount)
    self.set_status(TaskResult(TaskResultStatus.SUCCESSFUL, [f"Expired {amount} Notifications"]))
