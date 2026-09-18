from __future__ import annotations

from sqlalchemy import select

from app.models.notification import DeviceToken, Notification
from app.repositories.base_repository import BaseRepository


class NotificationRepository(BaseRepository[Notification]):
    model = Notification

    async def list_for_user(
        self, user_id, limit: int, offset: int
    ) -> list[Notification]:
        result = await self.session.execute(
            select(Notification)
            .where(Notification.recipient_id == user_id)
            .order_by(Notification.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())

    async def count_unread(self, user_id) -> int:
        result = await self.session.execute(
            select(Notification).where(
                Notification.recipient_id == user_id, Notification.is_read.is_(False)
            )
        )
        return len(list(result.scalars().all()))


class DeviceTokenRepository(BaseRepository[DeviceToken]):
    model = DeviceToken

    async def get_by_token(self, token: str) -> DeviceToken | None:
        result = await self.session.execute(
            select(DeviceToken).where(DeviceToken.token == token)
        )
        return result.scalar_one_or_none()

    async def list_for_user(self, user_id) -> list[DeviceToken]:
        result = await self.session.execute(
            select(DeviceToken).where(
                DeviceToken.user_id == user_id, DeviceToken.is_active.is_(True)
            )
        )
        return list(result.scalars().all())
