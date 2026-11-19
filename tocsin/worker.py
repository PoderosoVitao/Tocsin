from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine
from taskiq import AsyncBroker, Context, TaskiqDepends, TaskiqEvents, TaskiqScheduler, TaskiqState
from taskiq.kicker import AsyncKicker
from taskiq.schedule_sources import LabelScheduleSource
from taskiq_redis import RedisStreamBroker

from tocsin.alerts import DeliveryKey, claim_due_deliveries, deliver
from tocsin.channels import Sender
from tocsin.channels.telegram import TelegramSender
from tocsin.config import Settings, get_settings
from tocsin.db import Sessionmaker, create_engine, create_sessionmaker
from tocsin.events import alert_updated, publish
from tocsin.heartbeat import HEARTBEAT_INTERVAL_SECONDS, heartbeat
from tocsin.llm import LLM, LLMConfigError, build_llm
from tocsin.models import Event
from tocsin.summaries import summarize
from tocsin.sweep import run_sweep

log = logging.getLogger(__name__)

# httpx logs every request URL at INFO, and Telegram's Bot API puts the bot
# token in the URL. Warnings and errors still come through.
logging.getLogger("httpx").setLevel(logging.WARNING)

DELIVER = "tocsin.deliver"
RELAY_DELIVERIES = "tocsin.relay_deliveries"
RELAY_INTERVAL_SECONDS = 5
SWEEP = "tocsin.sweep"
SWEEP_INTERVAL_SECONDS = 5
HEARTBEAT = "tocsin.heartbeat"
SUMMARIZE = "tocsin.summarize"


def build_senders(http: httpx.AsyncClient, settings: Settings) -> dict[str, Sender]:
    return {"telegram": TelegramSender(http, settings.telegram_api_url)}


# What a worker process holds open for its tasks: one database pool, one HTTP
# client and one Redis client, shared by every task the process runs.
@dataclass
class Resources:
    settings: Settings
    engine: AsyncEngine
    sessionmaker: Sessionmaker
    http: httpx.AsyncClient
    senders: dict[str, Sender]
    redis: Redis
    # None when failure summaries are not set up.
    llm: LLM | None

    @classmethod
    def open(cls, settings: Settings) -> Resources:
        engine = create_engine(settings.database_url)
        http = httpx.AsyncClient()
        try:
            llm = build_llm(settings, http)
        except LLMConfigError as exc:
            log.error("failure summaries are off: %s", exc)
            llm = None
        return cls(
            settings,
            engine,
            create_sessionmaker(engine),
            http,
            build_senders(http, settings),
            Redis.from_url(settings.redis_url),
            llm,
        )

    async def close(self) -> None:
        if self.llm is not None:
            await self.llm.close()
        await self.redis.aclose()
        await self.http.aclose()
        await self.engine.dispose()


# Queues delivery jobs by task name, so code that only enqueues (the API) does
# not need the task functions themselves.
class TaskiqDispatcher:
    def __init__(self, broker: AsyncBroker) -> None:
        self.broker = broker

    async def deliver(self, keys: Sequence[DeliveryKey]) -> None:
        kicker: AsyncKicker[..., str] = AsyncKicker(DELIVER, self.broker, {})
        for key in keys:
            await kicker.kiq(key.event_id, str(key.channel_id))

    async def summarize(self, event_ids: Sequence[int]) -> None:
        kicker: AsyncKicker[..., str] = AsyncKicker(SUMMARIZE, self.broker, {})
        for event_id in event_ids:
            await kicker.kiq(event_id)


def register_tasks(broker: AsyncBroker, settings: Settings) -> None:
    @broker.on_event(TaskiqEvents.WORKER_STARTUP)
    async def open_resources(state: TaskiqState) -> None:
        state.resources = Resources.open(settings)

    @broker.on_event(TaskiqEvents.WORKER_SHUTDOWN)
    async def close_resources(state: TaskiqState) -> None:
        await state.resources.close()

    @broker.task(task_name=DELIVER)
    async def deliver_task(
        event_id: int, channel_id: str, context: Context = TaskiqDepends()
    ) -> str:
        resources: Resources = context.state.resources
        key = DeliveryKey(event_id, uuid.UUID(channel_id))
        status = await deliver(
            resources.sessionmaker,
            resources.senders,
            resources.settings,
            key,
            datetime.now(UTC),
        )
        # "busy" means another worker is on it and will report; anything else
        # changed or confirmed the delivery's state.
        if status != "busy":
            async with resources.sessionmaker() as session:
                check_id = await session.scalar(select(Event.check_id).where(Event.id == event_id))
            if check_id is not None:
                await publish(resources.redis, alert_updated(check_id))
        return status

    # Adds a line on the likely cause to the alert for a failed run. Anything
    # that goes wrong leaves the alert as it was, so nothing is retried.
    @broker.task(task_name=SUMMARIZE)
    async def summarize_task(event_id: int, context: Context = TaskiqDepends()) -> str:
        resources: Resources = context.state.resources
        try:
            return await summarize(
                resources.sessionmaker,
                resources.redis,
                resources.llm,
                resources.senders,
                resources.settings,
                event_id,
                datetime.now(UTC),
            )
        except Exception:
            log.exception("could not summarise event %s", event_id)
            return "failed"

    # Queues retries that are due and deliveries whose first enqueue was lost.
    @broker.task(task_name=RELAY_DELIVERIES, schedule=[{"interval": RELAY_INTERVAL_SECONDS}])
    async def relay_deliveries(context: Context = TaskiqDepends()) -> int:
        resources: Resources = context.state.resources
        async with resources.sessionmaker() as session, session.begin():
            keys = await claim_due_deliveries(session, datetime.now(UTC))
        await TaskiqDispatcher(context.broker).deliver(keys)
        return len(keys)

    @broker.task(task_name=SWEEP, schedule=[{"interval": SWEEP_INTERVAL_SECONDS}])
    async def sweep_overdue(context: Context = TaskiqDepends()) -> int:
        resources: Resources = context.state.resources
        return await run_sweep(
            resources.sessionmaker, TaskiqDispatcher(context.broker), resources.redis
        )

    @broker.task(task_name=HEARTBEAT, schedule=[{"interval": HEARTBEAT_INTERVAL_SECONDS}])
    async def send_heartbeat(context: Context = TaskiqDepends()) -> list[str] | None:
        resources: Resources = context.state.resources
        return await heartbeat(
            resources.sessionmaker,
            resources.redis,
            resources.http,
            resources.settings,
            datetime.now(UTC),
        )


# Redis streams with a consumer group: a job a worker took but never
# acknowledged (because the worker died) is handed to another worker, which
# plain Redis lists can't do.
def create_broker(settings: Settings) -> AsyncBroker:
    broker = RedisStreamBroker(settings.redis_url, queue_name="tocsin:tasks", maxlen=10_000)
    register_tasks(broker, settings)
    return broker


# The objects the taskiq CLI loads: `taskiq worker tocsin.worker:broker` and
# `taskiq scheduler tocsin.worker:scheduler` (wrapped by `tocsin worker` and
# `tocsin scheduler`).
broker = create_broker(get_settings())
scheduler = TaskiqScheduler(broker, [LabelScheduleSource(broker)])
