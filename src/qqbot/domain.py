"""Validated OneBot event and current-turn state. IDs are authority, display names are labels."""

from dataclasses import dataclass, field
import time


@dataclass
class Message:
    user: str
    group: str | None
    text: str
    message_id: str | None = None
    reply_id: str | None = None
    bot_id: str | None = None
    reply_only: bool = False

    @property
    def key(self):
        return (self.group or "private", self.user)


def parse_event(event, allowed_users, allowed_groups):
    if event.get("post_type") != "message":
        return None
    user = str(event.get("user_id", ""))
    if not user or user == str(event.get("self_id")):
        return None
    kind = event.get("message_type")
    if kind not in ("private", "group"):
        return None
    group = str(event["group_id"]) if kind == "group" and event.get("group_id") else None
    if kind == "group" and not group:
        return None
    if allowed_users and user not in allowed_users:
        return None
    if group and allowed_groups and group not in allowed_groups:
        return None
    segments = event.get("message")
    # Deployment explicitly requests array format; ignore unstructured CQ codes.
    if not isinstance(segments, list):
        return None
    text = "".join(
        str(x.get("data", {}).get("text", "")) for x in segments if x.get("type") == "text"
    ).strip()
    mention = any(
        x.get("type") == "at" and str(x.get("data", {}).get("qq")) == str(event.get("self_id"))
        for x in segments
    )
    prefix = text == "/ai" or text.startswith("/ai ") or text.startswith("/ai\n")
    reply_id = next(
        (str(x.get("data", {}).get("id", "")) for x in segments if x.get("type") == "reply"), None
    )
    if reply_id and not reply_id.lstrip("-").isdigit():
        reply_id = None
    if group and not (mention or prefix or reply_id):
        return None
    if prefix:
        text = text[3:].strip()
    message_id = str(event.get("message_id", ""))
    if not message_id.lstrip("-").isdigit():
        message_id = None
    return (
        Message(
            user,
            group,
            text,
            message_id,
            reply_id,
            str(event.get("self_id")),
            bool(group and reply_id and not (mention or prefix)),
        )
        if text
        else None
    )


@dataclass
class Turn:
    """Owned by the single queue worker; never shared between concurrent turns."""

    message: Message | None = None
    success: bool = False
    panel: bool = False
    with_memory: bool = False
    display_name: str = ""
    quoted_speaker: str = "unknown"
    timings: list = field(default_factory=list)
    recall_id: str = ""
    recall_ms: float = 0
    reset_short: bool = False
    queue_ms: float = 0
    started: float = field(default_factory=time.monotonic)
