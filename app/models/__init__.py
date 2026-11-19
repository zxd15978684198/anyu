"""ORM 模型包。

`alembic/env.py` 通过 `from app.models import Base` 拿 metadata，因此这里必须导出 `Base`。

按技术文档 §3 的分组逐批补模型（已建 10 张表全部有 ORM）：

- A 身份组织：rooms / beds / devices / elders / personas ✚ staff_accounts / guardian_accounts ✅
- B 关系绑定：elder_staff_assignments / elder_guardians / staff_tokens ✅（迭代 2）
- C 业务：voiceprints / medication_* / health_records / risk_events / work_orders* ✅（迭代 7）
- D 内容日志：media_library / media_history ✅（迭代 7）/ daily_reports / notifications /
  decision_logs / audit_logs / conversation_events ✅（迭代 10）

注意：后续 C/D 组建表时同样「先加模型类，再 autogenerate」，避免同类漂移。
"""

from app.models.audit import AuditLog
from app.models.base import NAMING_CONVENTION, Base
from app.models.conversation import ConversationThread
from app.models.conversation_event import ConversationEvent
from app.models.decision_log import DecisionLog
from app.models.guardian import GuardianAccount
from app.models.health import HealthCheckupSession, HealthRecord
from app.models.knowledge import KnowledgeChunk, KnowledgeDocument
from app.models.media import MediaHistory, MediaLibraryItem
from app.models.medication import MedicationAttempt, MedicationEvent, MedicationPlan
from app.models.memory import MemoryChunk
from app.models.notification import Notification
from app.models.organization import Bed, Device, Elder, Persona, Room
from app.models.relation import ElderGuardian, ElderStaffAssignment
from app.models.report import DailyReport
from app.models.staff import StaffAccount, StaffToken
from app.models.voiceprint import Voiceprint
from app.models.work_order import RiskEvent, WorkOrder, WorkOrderTransition

__all__ = [
    "NAMING_CONVENTION",
    "AuditLog",
    "Base",
    "Bed",
    "ConversationEvent",
    "ConversationThread",
    "DailyReport",
    "DecisionLog",
    "Device",
    "Elder",
    "ElderGuardian",
    "ElderStaffAssignment",
    "GuardianAccount",
    "HealthCheckupSession",
    "HealthRecord",
    "KnowledgeChunk",
    "KnowledgeDocument",
    "MediaHistory",
    "MediaLibraryItem",
    "MedicationAttempt",
    "MedicationEvent",
    "MedicationPlan",
    "MemoryChunk",
    "Notification",
    "Persona",
    "RiskEvent",
    "Room",
    "StaffAccount",
    "StaffToken",
    "Voiceprint",
    "WorkOrder",
    "WorkOrderTransition",
]
