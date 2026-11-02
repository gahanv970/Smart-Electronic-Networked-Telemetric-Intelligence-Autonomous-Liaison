"""
Notification Intelligence & Alert Center Service for S.E.N.T.I.A.L.
Centralizes, aggregates, ranks, and manages alerts from WhatsApp Desktop
and Gmail inbox notifications with contact priority locks, sorting, filters,
dismissals, and prompt-injection defenses.
"""

from dataclasses import asdict, dataclass
from datetime import datetime
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import APP_DIR
from gmail_service import EmailSummary
from whatsapp_service import WhatsAppMessage

logger = logging.getLogger(__name__)

ALERTS_FILE = APP_DIR / "alert_center.json"

URGENCY_RANK = {
    "critical": 4,
    "high": 3,
    "normal": 2,
    "medium": 2,
    "low": 1,
}


@dataclass
class AlertNotification:
    id: str
    source: str  # "WhatsApp" | "Gmail"
    sender: str
    title: str
    body_preview: str
    timestamp: str
    urgency: str = "low"  # "critical" | "high" | "normal" | "low"
    category: str = "General"  # "Work", "Family", "VIP", "General", "Email"
    is_read: bool = False
    is_dismissed: bool = False
    summary: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class AlertNotificationService:
    """
    Centralized Alert Notification Service.
    Aggregates notifications from WhatsApp and Gmail into a unified, prioritized Alert Center.
    Enforces user contact priority locks, read/unread states, dismissals, and deduplication.
    """

    def __init__(self):
        self._alerts: Dict[str, AlertNotification] = {}
        self._load_alerts()

    def _load_alerts(self) -> None:
        if not ALERTS_FILE.exists():
            self._alerts = {}
            return

        try:
            with open(ALERTS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    self._alerts = {
                        item["id"]: AlertNotification(
                            id=item.get("id", ""),
                            source=item.get("source", "General"),
                            sender=item.get("sender", "Unknown"),
                            title=item.get("title", ""),
                            body_preview=item.get("body_preview", ""),
                            timestamp=item.get("timestamp", ""),
                            urgency=item.get("urgency", "low").lower(),
                            category=item.get("category", "General"),
                            is_read=bool(item.get("is_read", False)),
                            is_dismissed=bool(item.get("is_dismissed", False)),
                            summary=item.get("summary", ""),
                        )
                        for item in data
                        if "id" in item
                    }
        except Exception as e:
            logger.error(f"Error loading alert center notifications: {e}")
            self._alerts = {}

    def _save_alerts(self) -> None:
        try:
            ALERTS_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(ALERTS_FILE, "w", encoding="utf-8") as f:
                json.dump([a.to_dict() for a in self._alerts.values()], f, indent=4)
        except Exception as e:
            logger.error(f"Error saving alert center notifications: {e}")

    # ------------------------------------------------------------------ Aggregation Methods

    def add_whatsapp_alert(
        self,
        msg: WhatsAppMessage,
        category: str = "General",
        contact_priority: Optional[str] = None,
    ) -> AlertNotification:
        """
        Aggregates analyzed WhatsApp message into Alert Center.
        Respects user contact priority lock so user CRITICAL/HIGH settings are authoritative.
        """
        alert_id = f"alert_{msg.id}"

        # Deduplication check
        if alert_id in self._alerts:
            return self._alerts[alert_id]

        resolved_urgency = (msg.urgency or "low").lower()

        # Enforce priority lock
        if contact_priority:
            cp_upper = contact_priority.upper()
            if cp_upper == "CRITICAL":
                resolved_urgency = "critical"
            elif cp_upper == "HIGH" and URGENCY_RANK.get(resolved_urgency, 1) < 3:
                resolved_urgency = "high"

        alert = AlertNotification(
            id=alert_id,
            source="WhatsApp",
            sender=msg.sender,
            title=f"WhatsApp Message from {msg.sender}",
            body_preview=msg.message_text[:300],
            timestamp=msg.timestamp,
            urgency=resolved_urgency,
            category=category,
            is_read=False,
            is_dismissed=False,
            summary=msg.summary,
        )

        self._alerts[alert_id] = alert
        self._save_alerts()
        logger.info(f"Added WhatsApp alert '{alert_id}' (Urgency: {resolved_urgency.upper()})")
        return alert

    def add_gmail_alert(
        self,
        summary_obj: EmailSummary,
        urgency: str = "low",
        ai_summary: str = "",
    ) -> AlertNotification:
        """
        Aggregates Gmail notification into Alert Center.
        Deduplicates repeated email notifications based on email ID.
        """
        alert_id = f"alert_gmail_{summary_obj.id}"

        if alert_id in self._alerts:
            return self._alerts[alert_id]

        clean_urgency = (urgency or "low").lower()

        alert = AlertNotification(
            id=alert_id,
            source="Gmail",
            sender=summary_obj.sender,
            title=summary_obj.subject or "Email Notification",
            body_preview=summary_obj.snippet[:300],
            timestamp=summary_obj.date,
            urgency=clean_urgency,
            category="Email",
            is_read=False,
            is_dismissed=False,
            summary=ai_summary or summary_obj.snippet,
        )

        self._alerts[alert_id] = alert
        self._save_alerts()
        logger.info(f"Added Gmail alert '{alert_id}' (Urgency: {clean_urgency.upper()})")
        return alert

    # ------------------------------------------------------------------ Retrieval, Sorting & Filters

    def get_alerts(
        self,
        source_filter: Optional[str] = None,
        category_filter: Optional[str] = None,
        urgency_filter: Optional[str] = None,
        unread_only: bool = False,
        search_query: Optional[str] = None,
        include_dismissed: bool = False,
    ) -> List[AlertNotification]:
        """
        Retrieves, filters, and ranks notifications.
        RANKING RULE:
        1. Higher urgency/priority first (Critical > High > Normal/Medium > Low)
        2. Timestamp descending (newest first)
        """
        res = []
        for alert in self._alerts.values():
            if not include_dismissed and alert.is_dismissed:
                continue

            if unread_only and alert.is_read:
                continue

            if source_filter and source_filter.lower() != "all":
                if alert.source.lower() != source_filter.lower():
                    continue

            if category_filter and category_filter.lower() != "all":
                if alert.category.lower() != category_filter.lower():
                    continue

            if urgency_filter and urgency_filter.lower() != "all":
                if alert.urgency.lower() != urgency_filter.lower():
                    continue

            if search_query:
                q = search_query.lower()
                matches = (
                    q in alert.sender.lower()
                    or q in alert.title.lower()
                    or q in alert.body_preview.lower()
                    or q in alert.summary.lower()
                )
                if not matches:
                    continue

            res.append(alert)

        # Sort: urgency rank desc, then timestamp string desc
        res.sort(
            key=lambda a: (
                URGENCY_RANK.get(a.urgency.lower(), 1),
                a.timestamp,
            ),
            reverse=True,
        )
        return res

    # ------------------------------------------------------------------ Control Operations

    def mark_as_read(self, alert_id: str) -> bool:
        if alert_id in self._alerts:
            self._alerts[alert_id].is_read = True
            self._save_alerts()
            return True
        return False

    def mark_all_as_read(self) -> int:
        count = 0
        for alert in self._alerts.values():
            if not alert.is_read:
                alert.is_read = True
                count += 1
        if count > 0:
            self._save_alerts()
        return count

    def dismiss_alert(self, alert_id: str) -> bool:
        if alert_id in self._alerts:
            self._alerts[alert_id].is_dismissed = True
            self._save_alerts()
            return True
        return False

    def clear_history(self) -> None:
        self._alerts.clear()
        if ALERTS_FILE.exists():
            try:
                ALERTS_FILE.unlink()
            except Exception as e:
                logger.warning(f"Could not delete alert center file: {e}")
        logger.info("Alert Center notification history cleared.")

    def get_unread_count(self) -> int:
        return sum(1 for a in self._alerts.values() if not a.is_read and not a.is_dismissed)

    def get_critical_count(self) -> int:
        return sum(
            1
            for a in self._alerts.values()
            if not a.is_dismissed and a.urgency.lower() in ("critical", "high") and not a.is_read
        )
