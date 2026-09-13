"""Forward matching Suhao cost-table messages from a Dingtalk event stream."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable


DEFAULT_TERMS = (
    "重开成本表",
    "成本表重新开",
    "重新开表",
    "成本重新开",
    "成本表",
    "调价",
    "降价",
    "缺货",
    "库存",
    "提货",
    "货源",
)


def _get(event: dict[str, Any], key: str, default: Any = "") -> Any:
    if key in event:
        return event[key]
    aliases = {"conversationId": "conversation_id", "senderId": "sender_open_dingtalk_id",
               "messageId": "message_id", "text": "content", "createTime": "create_time"}
    if aliases.get(key) in event:
        return event[aliases[key]]
    for nested_key in ("data", "event", "payload", "message"):
        nested = event.get(nested_key)
        if isinstance(nested, dict):
            value = _get(nested, key, None)
            if value is not None:
                return value
    return default


def should_forward(
    event: dict[str, Any], *, group_id: str = "", sender_id: str = "",
    sources: Iterable[tuple[str, str]] | None = None,
    terms: Iterable[str] = DEFAULT_TERMS,
    allow_reopen: bool = True,
) -> bool:
    pair = (_get(event, "conversationId"), _get(event, "senderId"))
    allowed = set(sources or ((group_id, sender_id),))
    if pair not in allowed:
        return False
    text = str(_get(event, "text", "") or "")
    if not allow_reopen and any(marker in text for marker in ("重开", "重新开", "成本表")):
        return False
    return bool(text) and any(term in text for term in terms)


def build_payload(event: dict[str, Any]) -> dict[str, Any]:
    sender = str(_get(event, "sender", "") or "").strip()
    timestamp = _get(event, "createTime", "")
    text = str(_get(event, "text", "") or "").strip()
    reopen = sender == "示例供货员" and any(
        marker in text for marker in ("重开成本表", "成本表重新开", "重新开表", "成本重新开")
    )
    title = "价格调整｜重开成本表" if reopen else "价格调整"
    source_group = _get(event, "conversationName", "") or os.environ.get("SOURCE_GROUP_NAME", "货源监控群")
    report = "\n\n".join(
        [
            f"# {title}",
            f"来源群：{source_group}",
            f"发送人：{sender}",
            f"时间：{timestamp}",
            "",
            "原消息：",
            text,
            "",
            "同一源消息只发送一次。",
        ]
    )
    return {
        "msgtype": "markdown",
        "markdown": {"title": title, "text": report},
        "at": {"isAtAll": False},
    }


def send_payload(webhook: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        webhook,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.loads(response.read().decode("utf-8"))
    if result.get("errcode") != 0:
        raise RuntimeError(f"DingTalk send failed: errcode={result.get('errcode')}")
    return result


def process_lines(
    lines: Iterable[str],
    *,
    webhook: str,
    sources: Iterable[tuple[str, str]],
    state_path: Path,
    terms: Iterable[str] = DEFAULT_TERMS,
    allow_reopen: bool = True,
    on_event=None,
    sender=None,
) -> int:
    sources = tuple(sources)
    sent = set()
    if state_path.exists():
        try:
            sent = set(json.loads(state_path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            sent = set()
    count = 0
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        message_id = str(_get(event, "messageId", "") or "")
        if (message_id and on_event is not None and
                (_get(event, 'conversationId'), _get(event, 'senderId')) in sources):
            try:
                on_event(event)
            except Exception:
                # Journal failure must not break existing authorized forwarding.
                pass
        if not message_id or message_id in sent:
            continue
        if not should_forward(event, sources=sources, terms=terms, allow_reopen=allow_reopen):
            continue
        if sender is None:
            send_payload(webhook, build_payload(event))
        else:
            sender(event, build_payload(event))
        sent.add(message_id)
        state_path.write_text(json.dumps(sorted(sent), ensure_ascii=False), encoding="utf-8")
        count += 1
    return count


def main() -> int:
    webhook = os.environ.get("DINGTALK_WEBHOOK", "")
    if not webhook:
        print("DINGTALK_WEBHOOK unavailable", file=sys.stderr)
        return 2
    configured_sources = (
        (
            os.environ.get("SOURCE_GROUP_ID", ""),
            os.environ.get("SOURCE_SENDER_ID", ""),
        ),
    )
    configured_sources = tuple(pair for pair in configured_sources if all(pair))
    sources = configured_sources
    if not sources:
        config = Path(__file__).resolve().parent / 'monitor_sources.json'
        if not config.is_file():
            return 2
        sources = tuple((row['GroupId'], row['SenderId']) for row in json.loads(config.read_text(encoding='utf-8-sig')))
    process_lines(
        sys.stdin,
        webhook=webhook,
        sources=sources,
        state_path=Path(os.environ.get("SOURCE_STATE_PATH", "suhao_cost_monitor_state.json")),
        terms=tuple(filter(None, os.environ.get("SOURCE_TERMS", "").split(","))) or DEFAULT_TERMS,
        allow_reopen=os.environ.get("ALLOW_REOPEN", "1") == "1",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
