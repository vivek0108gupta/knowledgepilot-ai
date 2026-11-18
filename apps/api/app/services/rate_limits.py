"""Request rate limits, enforced in PostgreSQL so every API process shares one count.

Algorithm: a sliding-window counter. Requests are counted in fixed windows; a request is
allowed if

    count in the previous window x (the part of it still inside the sliding window)
    + count in the current window (including this request)

is at most the limit. It smooths the burst a plain fixed window allows at each boundary,
and needs one row per window.

Atomicity: the current window's counter is incremented with a single
`INSERT ... ON CONFLICT DO UPDATE ... RETURNING`, which PostgreSQL serialises on the row, so
every request sees a distinct count and concurrent requests cannot all slip under the
limit. A request over the limit undoes its own increment, so refused requests do not count.

Subjects are only ever a verified user ID, or the ID of a workspace the caller was just
authorised for. Nothing in a request names a subject.
"""

import logging
import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.models.usage import RateLimitBucket

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Policy:
    # "<operation>:<subject kind>", for example "chat:user".
    name: str
    limit: int
    window_seconds: int


def chat_per_user(settings: Settings) -> Policy:
    return Policy("chat:user", settings.rate_limit_chat_per_minute, 60)


def chat_per_workspace(settings: Settings) -> Policy:
    return Policy("chat:workspace", settings.rate_limit_chat_per_workspace_per_minute, 60)


def search_per_user(settings: Settings) -> Policy:
    return Policy("search:user", settings.rate_limit_search_per_minute, 60)


def upload_per_user(settings: Settings) -> Policy:
    return Policy("upload:user", settings.rate_limit_upload_per_hour, 3600)


def rate_limited(policy: Policy, retry_after: int) -> AppError:
    scope = "this workspace" if policy.name.endswith(":workspace") else "you"
    return AppError(
        429,
        "rate_limited",
        f"Too many requests from {scope}. Try again in {retry_after} seconds.",
        retry_after_seconds=retry_after,
    )


def usage_unavailable() -> AppError:
    # Fail closed: a request whose limits cannot be checked is not run.
    return AppError(
        503,
        "usage_unavailable",
        "Usage limits could not be checked, so the request was not run. Try again shortly.",
    )


def windows(now: datetime, window_seconds: int) -> tuple[datetime, datetime, float]:
    """The current and previous window starts, and how far into the current window `now` is
    (0 to 1)."""
    epoch = now.timestamp()
    start = math.floor(epoch / window_seconds) * window_seconds
    current = datetime.fromtimestamp(start, UTC)
    return current, current - timedelta(seconds=window_seconds), (epoch - start) / window_seconds


def retry_after(previous: int, current: int, elapsed: float, policy: Policy) -> int:
    """Seconds until a request would be allowed, assuming no other requests meanwhile."""
    window = policy.window_seconds
    if current >= policy.limit or previous == 0:
        # Not before this window ends, when its count becomes the weighted previous one.
        return max(1, math.ceil(window * (1 - elapsed)))
    # The previous window's weight falls linearly; find when the total drops below the limit.
    needed = (previous * (1 - elapsed) + current + 1 - policy.limit) / previous
    return max(1, min(window, math.ceil(needed * window)))


async def hit(
    session: AsyncSession,
    policy: Policy,
    subject_id: uuid.UUID,
    *,
    now: datetime | None = None,
    settings: Settings | None = None,
) -> None:
    """Count one request against `policy` for `subject_id`, or refuse it with 429.

    Commits its own short transaction, so call it before any other work in the request.
    """
    settings = settings or get_settings()
    if not settings.rate_limits_enabled:
        return
    now = now or datetime.now(UTC)
    current, previous, elapsed = windows(now, policy.window_seconds)
    key = (
        RateLimitBucket.policy == policy.name,
        RateLimitBucket.subject_id == subject_id,
    )
    try:
        count = await session.scalar(
            insert(RateLimitBucket)
            .values(policy=policy.name, subject_id=subject_id, window_start=current, count=1)
            .on_conflict_do_update(
                index_elements=["policy", "subject_id", "window_start"],
                set_={"count": RateLimitBucket.count + 1},
            )
            .returning(RateLimitBucket.count)
        )
        before = await session.scalar(
            select(RateLimitBucket.count).where(*key, RateLimitBucket.window_start == previous)
        )
        # Windows older than the previous one can never count again.
        await session.execute(
            delete(RateLimitBucket).where(*key, RateLimitBucket.window_start < previous)
        )
        assert count is not None
        weighted = (before or 0) * (1 - elapsed) + count
        allowed = weighted <= policy.limit
        if not allowed:
            await session.execute(
                update(RateLimitBucket)
                .where(*key, RateLimitBucket.window_start == current)
                .values(count=RateLimitBucket.count - 1)
            )
        await session.commit()
    except SQLAlchemyError as error:
        await session.rollback()
        logger.error("Rate limit check failed: %s", type(error).__name__)
        raise usage_unavailable() from error
    if not allowed:
        wait = retry_after(before or 0, count - 1, elapsed, policy)
        logger.info("Rate limit %s reached", policy.name)
        raise rate_limited(policy, wait)
