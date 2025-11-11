# models/__init__.py
from .cover import Cover  # 确保 Base.metadata.create_all 时能创建 covers 表

import os as _os

if _os.getenv("FLAG_ENABLE_PUBLIC_GROUPS", "").strip().lower() in {"1", "true", "yes", "on"}:
    from .public_group import (  # noqa: F401
        PublicGroup,
        PublicGroupMember,
        PublicGroupRewardClaim,
        PublicGroupEvent,
        PublicGroupActivity,
        PublicGroupActivityLog,
        PublicGroupBookmark,
        PublicGroupActivityWebhook,
        PublicGroupActivityConversionLog,
        PublicGroupActivityStatus,
        PublicGroupStatus,
    )