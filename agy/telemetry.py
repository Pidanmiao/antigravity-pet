"""Read-only Antigravity token telemetry shared by the HUD bar and Token Meter.

Data sources, best first:

1. The local Antigravity language server (Connect JSON RPC).  The endpoint and
   its per-launch CSRF token are discovered from the user's own
   ``language_server`` process; nothing leaves 127.0.0.1.
2. ``hud_live.json`` written by the token-meter sidecar when it was updated
   within the last few seconds.
3. ``<appData>/brain/<conversation>/.system_generated/logs/transcript.jsonl``
   offline logs (no quota, approximate speed).

Everything in this module is plain Python so it can run on a worker thread and
be unit tested without Qt.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

LS_SERVICE = "exa.language_server_pb.LanguageServerService"
HUD_LIVE_MAX_AGE_SECONDS = 15.0
ACTIVE_HOOK_MAX_AGE_SECONDS = 7200.0
RUNNING_STATUS = "CASCADE_RUN_STATUS_RUNNING"


# --------------------------------------------------------------------------
# data model
# --------------------------------------------------------------------------


DEFAULT_CONTEXT_LIMIT = 524_288  # 512k: Antigravity's default context window


@dataclass
class StepUsage:
    model_id: str
    model_label: str
    input_tokens: int = 0
    cache_read_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    response_tokens: int = 0
    created_ms: int = 0
    ttft_ms: float | None = None
    decode_tps: float | None = None
    e2e_tps: float | None = None
    tool_calls: tuple[str, ...] = ()

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.output_tokens

    @property
    def context_tokens(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.response_tokens


@dataclass
class ConversationUsage:
    conversation_id: str
    title: str
    status: str = "CASCADE_RUN_STATUS_IDLE"
    last_modified_ms: int = 0
    is_subagent: bool = False
    steps: list[StepUsage] = field(default_factory=list)

    @property
    def running(self) -> bool:
        return RUNNING_STATUS in (self.status or "")


@dataclass
class QuotaWindow:
    kind: str  # "5h", "week" or "" (unknown window)
    name: str
    remaining_pct: float | None
    reset_time: str | None
    group: str = ""  # e.g. "Gemini Models" / "Claude and GPT models"


_FIVE_HOUR = re.compile(r"(?<!\d)5\s*h(?:ours?|rs?)?(?![a-z])|(?<!\d)5[-_ ]?hours?|five[-_ ]?hours?|PT5H|(?<!\d)18000s?(?!\d)|(?<!\d)5\s*小时", re.I)
_WEEK = re.compile(r"week|(?<!\d)7\s*d(?:ays?)?(?![a-z])|(?<!\d)7[-_ ]?days?|seven[-_ ]?days?|P7D|P1W|(?<!\d)604800s?(?!\d)|周|(?<!\d)7\s*天|七天", re.I)


def classify_window(*texts: str) -> str:
    joined = " ".join(t for t in texts if t)
    if _FIVE_HOUR.search(joined):
        return "5h"
    if _WEEK.search(joined):
        return "week"
    return ""


def parse_quota_buckets(raw: object) -> list[QuotaWindow]:
    out: list[QuotaWindow] = []
    for bucket in raw if isinstance(raw, list) else []:
        if not isinstance(bucket, dict) or bucket.get("disabled"):
            continue
        pct = bucket.get("remainingPct")
        out.append(
            QuotaWindow(
                kind=classify_window(str(bucket.get("window") or ""), str(bucket.get("name") or ""), str(bucket.get("description") or "")),
                name=str(bucket.get("name") or bucket.get("window") or ""),
                remaining_pct=float(pct) if isinstance(pct, (int, float)) else None,
                reset_time=bucket.get("resetTime") or None,
                group=str(bucket.get("group") or ""),
            )
        )
    return out


_THIRD_PARTY = ("claude", "gpt", "oss", "opus", "sonnet")


def _group_has_model(group: str, model_label: str) -> bool:
    """Does quota group ``group`` (its display name) cover ``model_label``?"""
    group, label = (group or "").lower(), (model_label or "").lower()
    if not group or not label:
        return False
    third_party = any(word in label for word in _THIRD_PARTY)
    if third_party:
        return any(word in group for word in ("claude", "gpt"))
    return "gemini" in group


@dataclass
class LiveSnapshot:
    source: str
    conversation_id: str = ""
    title: str = ""
    status: str = ""
    selected_model_label: str = ""
    last_step_model_label: str = ""
    latest_decode_tps: float | None = None
    latest_e2e_tps: float | None = None
    latest_ttft_ms: float | None = None
    avg_decode_tps: float | None = None
    quota_remaining_pct: float | None = None
    quota_reset_time: str | None = None
    cache_hit_pct: float = 0.0
    context_used_tokens: int = 0
    context_limit_tokens: int = DEFAULT_CONTEXT_LIMIT
    cumulative_total_tokens: int = 0
    conversations: list[tuple[str, str]] = field(default_factory=list)
    fetched_at: float = field(default_factory=time.time)
    quota_windows: list[QuotaWindow] = field(default_factory=list)

    def quota_for(self, kind: str) -> tuple[float | None, str | None]:
        """Remaining % and reset time of the 5-hour (``"5h"``) or weekly window.

        Uses the per-window buckets when Antigravity reports them (the tightest
        bucket of that kind wins).  Otherwise the single per-model quota is
        placed by how far away its reset is: within ~5 h it is the 5-hour
        window, further away it is the weekly one.
        """
        matches = [w for w in self.quota_windows if w.kind == kind and w.remaining_pct is not None]
        # Models are grouped (Gemini / Claude and GPT), each group with its own
        # windows: use the group of the model in use.
        own = [w for w in matches if _group_has_model(w.group, self.model_for_form)]
        matches = own or matches
        if matches:
            tightest = min(matches, key=lambda w: w.remaining_pct)
            return tightest.remaining_pct, tightest.reset_time
        if any(w.kind for w in self.quota_windows) or self.quota_remaining_pct is None:
            return None, None
        target = parse_iso_ms(self.quota_reset_time)
        hours = (target - time.time() * 1000) / 3_600_000 if target else 0.0
        single_kind = "week" if hours > 5.2 else "5h"
        return (self.quota_remaining_pct, self.quota_reset_time) if kind == single_kind else (None, None)

    @property
    def running(self) -> bool:
        return RUNNING_STATUS in (self.status or "") or self.status.upper() == "RUNNING"

    @property
    def context_pct(self) -> float:
        if self.context_limit_tokens <= 0:
            return 0.0
        return self.context_used_tokens / self.context_limit_tokens * 100.0

    @property
    def display_rate(self) -> float | None:
        for value in (self.latest_decode_tps, self.avg_decode_tps):
            if value is not None and value > 0:
                return value
        return None

    @property
    def model_for_form(self) -> str:
        """The model the user is actually talking to right now."""
        if self.running and self.last_step_model_label:
            return self.last_step_model_label
        return self.selected_model_label or self.last_step_model_label


@dataclass
class ModelRow:
    label: str
    steps: int = 0
    input_tokens: int = 0
    cache_read_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    response_tokens: int = 0
    avg_decode_tps: float | None = None
    peak_decode_tps: float | None = None
    avg_e2e_tps: float | None = None
    avg_ttft_ms: float | None = None
    share_pct: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.output_tokens

    @property
    def cache_hit_pct(self) -> float:
        prompt = self.input_tokens + self.cache_read_tokens
        return self.cache_read_tokens / prompt * 100.0 if prompt else 0.0


@dataclass
class MeterReport:
    source: str
    models: list[ModelRow]
    conversation_count: int
    active: ConversationUsage | None
    live: LiveSnapshot | None
    generated_at: float = field(default_factory=time.time)
    conversations: list[ConversationUsage] = field(default_factory=list)

    @property
    def grand_total(self) -> int:
        return sum(row.total_tokens for row in self.models)


# --------------------------------------------------------------------------
# small parsing helpers
# --------------------------------------------------------------------------


def to_int(value: object) -> int:
    try:
        number = float(value)  # LS sends int64 as strings
    except (TypeError, ValueError):
        return 0
    return int(round(number)) if number > 0 else 0


def parse_iso_ms(value: object) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    # Python < 3.11 cannot parse more than 6 fractional digits.
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int(moment.timestamp() * 1000)


def prettify_model(raw: str) -> str:
    if not raw:
        return "未知模型"
    if raw.startswith("MODEL_"):
        if raw.startswith("MODEL_PLACEHOLDER_"):
            return "Model " + raw[len("MODEL_PLACEHOLDER_"):]
        words = raw[len("MODEL_"):].split("_")
        return " ".join(w if any(c.isdigit() for c in w) and len(w) <= 4 else w.capitalize() for w in words)
    if re.fullmatch(r"[a-z0-9.\-]+", raw):
        parts = raw.split("-")
        return " ".join(p.upper() if p in {"gpt", "oss"} else p.capitalize() for p in parts)
    return raw


_UNKNOWN_MODEL_LABELS = {"", "未知模型", "未知模型（离线日志）", "未选择模型", "unknown model"}


def infer_context_limit(*labels: str) -> int:
    """Best guess for the active model when the language server omits the window."""
    target = ""
    for label in labels:
        if isinstance(label, str):
            cleaned = label.strip().lower()
            if cleaned and cleaned not in _UNKNOWN_MODEL_LABELS:
                target = cleaned
                break
    if "claude" in target:
        return 200_000
    if "gpt-oss" in target or "120b" in target:
        return 131_072
    return DEFAULT_CONTEXT_LIMIT


_PBTXT_VIEW_BLOCK = re.compile(r"last_user_view_time\s*:\s*\{([^}]*)\}")
_PBTXT_SECONDS = re.compile(r"\bseconds\s*:\s*(\d+)")
_PBTXT_NANOS = re.compile(r"\bnanos\s*:\s*(\d+)")
_PBTXT_TITLE = re.compile(r'(?:^|\s)title\s*:\s*"((?:[^"\\]|\\.)*)"')


def _decode_pbtxt_string(raw: str) -> str:
    """Decode proto text-format string escapes (octal UTF-8 bytes or C-escapes)."""
    buf = bytearray()
    i = 0
    n = len(raw)
    escapes = {"n": 10, "r": 13, "t": 9, '"': 34, "'": 39, "\\": 92}
    while i < n:
        ch = raw[i]
        if ch == "\\" and i + 1 < n:
            nxt = raw[i + 1]
            if nxt in escapes:
                buf.append(escapes[nxt])
                i += 2
                continue
            if "0" <= nxt <= "7":
                j = i + 1
                while j < min(n, i + 4) and "0" <= raw[j] <= "7":
                    j += 1
                buf.append(int(raw[i + 1:j], 8) & 0xFF)
                i = j
                continue
            if nxt in ("x", "X") and i + 3 < n:
                try:
                    buf.append(int(raw[i + 2:i + 4], 16))
                    i += 4
                    continue
                except ValueError:
                    pass
        buf.extend(ch.encode("utf-8", errors="replace"))
        i += 1
    return buf.decode("utf-8", errors="replace").strip()


def parse_annotation_pbtxt(raw: bytes | str) -> tuple[int, str]:
    """Extract (last_user_view_time_ms, title) from an Antigravity ``.pbtxt``."""
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw or "")
    view_ms = 0
    block_match = _PBTXT_VIEW_BLOCK.search(text)
    if block_match:
        block = block_match.group(1)
        sec_match = _PBTXT_SECONDS.search(block)
        if sec_match:
            nanos_match = _PBTXT_NANOS.search(block)
            nanos = int(nanos_match.group(1)) if nanos_match else 0
            view_ms = int(sec_match.group(1)) * 1000 + nanos // 1_000_000
    title_match = _PBTXT_TITLE.search(text)
    title = _decode_pbtxt_string(title_match.group(1)) if title_match else ""
    return view_ms, title


def _normalize_workspace(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    text = urllib.parse.unquote(value.strip())
    if text.lower().startswith("file:///"):
        text = text[8:]
    elif text.lower().startswith("file://"):
        text = text[7:]
    text = text.replace("\\", "/").rstrip("/")
    return text.lower()


def clean_title(text: str) -> str:
    match = re.search(r"<USER_REQUEST>([\s\S]*?)</USER_REQUEST>", text or "", re.I)
    body = re.sub(r"<[^>]+>", " ", match.group(1) if match else (text or ""))
    body = re.sub(r"\s+", " ", body).strip()
    return body[:48] + "…" if len(body) > 48 else body


def format_tokens(value: float) -> str:
    value = float(value or 0)
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return f"{value:.0f}"


def format_reset_countdown(iso: str | None, *, now_ms: int | None = None) -> str:
    target = parse_iso_ms(iso)
    if target is None:
        return ""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    minutes = (target - now_ms) / 60_000
    clock = datetime.fromtimestamp(target / 1000).strftime("%H:%M")
    if minutes <= 0:
        return "即将重置"
    if minutes >= 60:
        return f"{int(minutes // 60)}h {int(minutes % 60)}m 后重置 ({clock})"
    return f"{int(minutes + 0.999)}m 后重置 ({clock})"


# --------------------------------------------------------------------------
# step parsing
# --------------------------------------------------------------------------


def user_input_title(raw: object) -> str:
    if not isinstance(raw, dict) or raw.get("type") != "CORTEX_STEP_TYPE_USER_INPUT":
        return ""
    user_input = raw.get("userInput") or {}
    items = user_input.get("items") or []
    joined = " ".join(str(item.get("text", "")) for item in items if isinstance(item, dict))
    return clean_title(joined or str(user_input.get("userResponse") or ""))


def parse_rpc_steps(trajectory: dict[str, Any], label_map: dict[str, str] | None = None) -> tuple[list[StepUsage], str]:
    """Parse ``GetCascadeTrajectory().trajectory`` into usage steps and a title."""
    steps: list[StepUsage] = []
    title = ""
    for raw in trajectory.get("steps") or []:
        title = title or user_input_title(raw)
        step = parse_rpc_step(raw, label_map)
        if step is not None:
            steps.append(step)
    return steps, title


def parse_rpc_step(raw: object, label_map: dict[str, str] | None = None) -> StepUsage | None:
    """One raw trajectory step -> usage, or None when it carries no tokens."""
    label_map = label_map or {}
    if True:
        if not isinstance(raw, dict):
            return None
        meta = raw.get("metadata") or {}
        usage = meta.get("modelUsage")
        if not isinstance(usage, dict):
            return None
        input_tokens = to_int(usage.get("inputTokens"))
        cache_tokens = to_int(usage.get("cacheReadTokens") or usage.get("cacheReadInputTokens"))
        output_tokens = to_int(usage.get("outputTokens"))
        if input_tokens + cache_tokens + output_tokens == 0:
            return None
        thinking = to_int(usage.get("thinkingOutputTokens"))
        response = (
            to_int(usage.get("responseOutputTokens"))
            if "responseOutputTokens" in usage
            else max(0, output_tokens - thinking)
        )
        model_id = str(usage.get("model") or meta.get("generatorModel") or "UNKNOWN_MODEL")
        created = parse_iso_ms(meta.get("createdAt"))
        first = parse_iso_ms(meta.get("viewableAt"))
        finished = parse_iso_ms(meta.get("finishedGeneratingAt")) or parse_iso_ms(meta.get("completedAt"))
        e2e_ms = finished - created if created and finished and finished > created else None
        ttft = first - created if created and first and first >= created else None
        decode_ms = finished - first if first and finished and finished - first >= 80 else None
        e2e_tps = output_tokens / (e2e_ms / 1000) if output_tokens and e2e_ms and e2e_ms >= 80 else None
        decode_tps = output_tokens / (decode_ms / 1000) if output_tokens and decode_ms else e2e_tps
        tools = tuple(
            str(call.get("name"))
            for call in ((raw.get("plannerResponse") or {}).get("toolCalls") or [])
            if isinstance(call, dict) and call.get("name")
        )
        return (
            StepUsage(
                model_id=model_id,
                model_label=label_map.get(model_id) or prettify_model(model_id),
                input_tokens=input_tokens,
                cache_read_tokens=cache_tokens,
                output_tokens=output_tokens,
                thinking_tokens=thinking,
                response_tokens=response,
                created_ms=created or 0,
                ttft_ms=ttft,
                decode_tps=decode_tps,
                e2e_tps=e2e_tps,
                tool_calls=tools,
            )
        )


def parse_transcript_lines(lines: Iterable[str], *, default_model: str = "") -> tuple[list[StepUsage], str]:
    """Parse an offline ``transcript.jsonl``.

    Only some lines carry ``model``; it is carried forward and, for the leading
    unlabelled lines, backward from the first labelled one.  Speed is estimated
    from the gap to the next log line, so it is a rough end-to-end figure.
    """
    records: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            records.append(obj)
    title = ""
    for obj in records:
        if obj.get("type") == "USER_INPUT" and obj.get("content"):
            title = clean_title(str(obj["content"]))
            break
    first_model = next((str(o["model"]) for o in records if o.get("type") == "PLANNER_RESPONSE" and o.get("model")), default_model)
    current = first_model
    steps: list[StepUsage] = []
    for index, obj in enumerate(records):
        if obj.get("type") != "PLANNER_RESPONSE":
            continue
        if obj.get("model"):
            current = str(obj["model"])
        input_tokens = to_int(obj.get("input_tokens"))
        cache_tokens = to_int(obj.get("cache_read_tokens"))
        output_tokens = to_int(obj.get("output_tokens"))
        if input_tokens + cache_tokens + output_tokens == 0:
            continue
        thinking_tokens = to_int(obj.get("thinking_tokens") or obj.get("thinkingOutputTokens"))
        if "response_tokens" in obj:
            response_tokens = to_int(obj.get("response_tokens"))
        elif "responseOutputTokens" in obj:
            response_tokens = to_int(obj.get("responseOutputTokens"))
        else:
            response_tokens = max(0, output_tokens - thinking_tokens)
        created = parse_iso_ms(obj.get("created_at"))
        rate = None
        if created and index + 1 < len(records):
            nxt = parse_iso_ms(records[index + 1].get("created_at"))
            if nxt is not None:
                seconds = (nxt - created) / 1000
                if 0.5 <= seconds < 300 and output_tokens:
                    rate = output_tokens / seconds
        tools = tuple(
            str(call.get("name"))
            for call in (obj.get("tool_calls") or [])
            if isinstance(call, dict) and call.get("name")
        )
        steps.append(
            StepUsage(
                model_id=current or "UNKNOWN_MODEL",
                model_label=prettify_model(current) if current else "未知模型（离线日志）",
                input_tokens=input_tokens,
                cache_read_tokens=cache_tokens,
                output_tokens=output_tokens,
                thinking_tokens=thinking_tokens,
                response_tokens=response_tokens,
                created_ms=created or 0,
                decode_tps=rate,
                e2e_tps=rate,
                tool_calls=tools,
            )
        )
    return steps, title


# --------------------------------------------------------------------------
# aggregation
# --------------------------------------------------------------------------


def summarize_conversation(
    conversation: ConversationUsage,
    *,
    source: str,
    selected_model_label: str = "",
    quota: tuple[float | None, str | None] = (None, None),
    quota_windows: list[QuotaWindow] | None = None,
    conversations: list[tuple[str, str]] | None = None,
) -> LiveSnapshot:
    steps = conversation.steps
    input_sum = sum(s.input_tokens for s in steps)
    cache_sum = sum(s.cache_read_tokens for s in steps)
    output_sum = sum(s.output_tokens for s in steps)
    decode_out = 0.0
    decode_seconds = 0.0
    for step in steps:
        if step.decode_tps and step.output_tokens:
            decode_out += step.output_tokens
            decode_seconds += step.output_tokens / step.decode_tps
    latest = steps[-1] if steps else None
    prompt = input_sum + cache_sum
    last_label = latest.model_label if latest else ""
    limit_labels = (last_label, selected_model_label) if (conversation.running and last_label) else (selected_model_label, last_label)
    return LiveSnapshot(
        source=source,
        conversation_id=conversation.conversation_id,
        title=conversation.title,
        status=conversation.status,
        selected_model_label=selected_model_label or last_label,
        last_step_model_label=last_label,
        latest_decode_tps=latest.decode_tps if latest else None,
        latest_e2e_tps=latest.e2e_tps if latest else None,
        latest_ttft_ms=latest.ttft_ms if latest else None,
        avg_decode_tps=decode_out / decode_seconds if decode_seconds > 0 else None,
        quota_remaining_pct=quota[0],
        quota_reset_time=quota[1],
        quota_windows=list(quota_windows or []),
        cache_hit_pct=cache_sum / prompt * 100.0 if prompt else 0.0,
        context_used_tokens=latest.context_tokens if latest else 0,
        context_limit_tokens=infer_context_limit(*limit_labels),
        cumulative_total_tokens=input_sum + cache_sum + output_sum,
        conversations=list(conversations or []),
    )


def snapshot_from_hud_live(payload: dict[str, Any], *, pinned_id: str = "", source: str = "hud_live") -> LiveSnapshot | None:
    convs = payload.get("conversations") if isinstance(payload.get("conversations"), list) else []
    chosen = None
    if pinned_id:
        chosen = next((c for c in convs if isinstance(c, dict) and c.get("conversationId") == pinned_id), None)
    chosen = chosen or payload.get("active")
    if not isinstance(chosen, dict):
        return None

    def num(key: str) -> float | None:
        value = chosen.get(key)
        return float(value) if isinstance(value, (int, float)) else None

    status = str(chosen.get("status") or "")
    is_running = RUNNING_STATUS in status or status.upper() == "RUNNING"
    selected_label = str(chosen.get("selectedModelLabel") or payload.get("selectedModelLabel") or "")
    last_step_label = str(chosen.get("lastStepModelLabel") or "")
    limit_labels = (last_step_label, selected_label) if is_running else (selected_label, last_step_label)

    return LiveSnapshot(
        source=source,
        conversation_id=str(chosen.get("conversationId") or ""),
        title=str(chosen.get("title") or ""),
        status=status,
        selected_model_label=selected_label,
        last_step_model_label=last_step_label,
        latest_decode_tps=num("latestDecodeTokPerSec"),
        latest_e2e_tps=num("latestE2eTokPerSec"),
        latest_ttft_ms=num("latestTtftMs"),
        avg_decode_tps=num("avgDecodeTokPerSec"),
        quota_remaining_pct=num("quotaRemainingPct"),
        quota_reset_time=chosen.get("quotaResetTime"),
        cache_hit_pct=num("cacheHitRatePct") or 0.0,
        context_used_tokens=int(num("contextUsedTokens") or 0),
        # Older sidecars always sent a guessed 1M; only trust a reported window.
        context_limit_tokens=(
            int(num("contextLimitTokens") or 0)
            if chosen.get("contextLimitSource") == "ls" and num("contextLimitTokens")
            else infer_context_limit(*limit_labels)
        ),
        cumulative_total_tokens=int(num("cumulativeTotalTokens") or 0),
        quota_windows=parse_quota_buckets(payload.get("quotaBuckets")),
        conversations=[
            (str(c.get("conversationId")), str(c.get("title") or c.get("conversationId", "")[:8]))
            for c in convs
            if isinstance(c, dict) and c.get("conversationId")
        ],
    )


def build_model_rows(conversations: Iterable[ConversationUsage]) -> list[ModelRow]:
    rows: dict[str, ModelRow] = {}
    decode: dict[str, list[tuple[int, float]]] = {}
    e2e: dict[str, list[tuple[int, float]]] = {}
    ttft: dict[str, list[float]] = {}
    for conversation in conversations:
        for step in conversation.steps:
            row = rows.setdefault(step.model_label, ModelRow(label=step.model_label))
            row.steps += 1
            row.input_tokens += step.input_tokens
            row.cache_read_tokens += step.cache_read_tokens
            row.output_tokens += step.output_tokens
            row.thinking_tokens += step.thinking_tokens
            row.response_tokens += step.response_tokens
            if step.decode_tps and step.output_tokens:
                decode.setdefault(row.label, []).append((step.output_tokens, step.decode_tps))
            if step.e2e_tps and step.output_tokens:
                e2e.setdefault(row.label, []).append((step.output_tokens, step.e2e_tps))
            if step.ttft_ms is not None:
                ttft.setdefault(row.label, []).append(step.ttft_ms)

    def weighted(samples: list[tuple[int, float]]) -> float | None:
        tokens = sum(t for t, _ in samples)
        seconds = sum(t / r for t, r in samples if r > 0)
        return tokens / seconds if seconds > 0 else None

    grand = sum(row.total_tokens for row in rows.values()) or 1
    for label, row in rows.items():
        row.avg_decode_tps = weighted(decode.get(label, []))
        row.peak_decode_tps = max((r for _, r in decode.get(label, [])), default=None)
        row.avg_e2e_tps = weighted(e2e.get(label, []))
        samples = ttft.get(label, [])
        row.avg_ttft_ms = sum(samples) / len(samples) if samples else None
        row.share_pct = row.total_tokens / grand * 100.0
    return sorted(rows.values(), key=lambda r: r.total_tokens, reverse=True)


# --------------------------------------------------------------------------
# language server discovery
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class LsEndpoint:
    port: int
    csrf_token: str = field(repr=False)


def _csrf_from_cmdline(cmdline: list[str]) -> str | None:
    for index, arg in enumerate(cmdline):
        if arg.startswith("--csrf_token"):
            if "=" in arg:
                return arg.split("=", 1)[1]
            if index + 1 < len(cmdline):
                return cmdline[index + 1]
    return None


class LanguageServerClient:
    def __init__(self, *, timeout: float = 4.0) -> None:
        self.timeout = timeout
        self.endpoint: LsEndpoint | None = None
        self._next_discovery_at = 0.0

    def rpc(self, method: str, body: dict[str, Any] | None = None, *, timeout: float | None = None) -> dict[str, Any]:
        endpoint = self.endpoint or self.discover()
        if endpoint is None:
            raise ConnectionError("Antigravity 语言服务未运行")
        try:
            return self._post(endpoint, method, body or {}, timeout or self.timeout)
        except (OSError, ValueError, urllib.error.URLError) as error:
            self.endpoint = None
            raise ConnectionError(str(error)) from error

    @staticmethod
    def _post(endpoint: LsEndpoint, method: str, body: dict[str, Any], timeout: float) -> dict[str, Any]:
        request = urllib.request.Request(
            f"http://127.0.0.1:{endpoint.port}/{LS_SERVICE}/{method}",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "x-codeium-csrf-token": endpoint.csrf_token},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def discover(self) -> LsEndpoint | None:
        now = time.monotonic()
        if now < self._next_discovery_at:
            return None
        self._next_discovery_at = now + 10.0
        try:
            import psutil
        except ImportError:
            return None
        for proc in psutil.process_iter(["name"]):
            try:
                if "language_server" not in (proc.info.get("name") or "").lower():
                    continue
                token = _csrf_from_cmdline(proc.cmdline() or [])
                if not token:
                    continue
                ports = sorted(
                    {c.laddr.port for c in proc.net_connections(kind="tcp") if c.status == psutil.CONN_LISTEN}
                )
            except (psutil.Error, OSError):
                continue
            for port in ports:
                candidate = LsEndpoint(port, token)
                try:
                    reply = self._post(candidate, "GetUserStatus", {}, 1.5)
                except (OSError, ValueError, urllib.error.URLError):
                    continue
                if isinstance(reply, dict) and "userStatus" in reply:
                    self.endpoint = candidate
                    self._next_discovery_at = 0.0
                    return candidate
        return None


# --------------------------------------------------------------------------
# collector
# --------------------------------------------------------------------------


def default_app_data_root() -> Path:
    return Path.home() / ".gemini" / "antigravity"


@dataclass
class _FocusCandidate:
    cid: str
    title: str
    running: bool
    modified_ms: int
    view_ms: int
    input_ms: int
    project_id: str = ""
    workspaces: set[str] = field(default_factory=set)
    has_project_meta: bool = False


class TelemetryCollector:
    """Combines all sources; call from a worker thread."""

    def __init__(
        self,
        app_data_root: Path | None = None,
        *,
        ls_client: LanguageServerClient | None = None,
        use_language_server: bool = True,
    ) -> None:
        self.root = Path(app_data_root) if app_data_root else default_app_data_root()
        self.ls = ls_client or LanguageServerClient()
        self.use_language_server = use_language_server
        self._labels: dict[str, str] = {}
        self._quotas: dict[str, tuple[float | None, str | None]] = {}
        self._selected_model = ""
        self._user_status_at = 0.0
        self._trajectory_cache: dict[str, tuple[str, ConversationUsage]] = {}
        # cid -> (raw step count fetched, parsed step per raw index, title)
        self._raw_steps: dict[str, tuple[int, list[StepUsage | None], str]] = {}
        self.full_fetches = 0
        self.incremental_fetches = 0
        self._transcript_cache: dict[str, tuple[float, ConversationUsage]] = {}
        self._annotation_cache: dict[str, tuple[int, int, str]] = {}
        self._active_session_cache: tuple[int, dict[str, Any] | None] = (-1, None)

    @property
    def hud_live_path(self) -> Path:
        return self.root / "sidecar_data" / "token-meter" / "dashboard" / "data" / "hud_live.json"

    @property
    def pet_live_path(self) -> Path:
        return self.root / "sidecar_data" / "agy-pet" / "pet-bridge" / "data" / "pet_live.json"

    @property
    def active_session_path(self) -> Path:
        return self.root / "sidecar_data" / "agy-pet" / "active_session.json"

    @property
    def annotations_dir(self) -> Path:
        return self.root / "annotations"

    @property
    def projects_dir(self) -> Path:
        return self.root.parent / "config" / "projects"

    @property
    def brain_dir(self) -> Path:
        return self.root / "brain"

    # ----- active-session focus helpers ------------------------------------

    def _read_annotation(self, cid: str) -> tuple[int, str]:
        if not cid:
            return 0, ""
        path = self.annotations_dir / f"{cid}.pbtxt"
        try:
            mtime_ns = path.stat().st_mtime_ns
        except OSError:
            return 0, ""
        cached = self._annotation_cache.get(cid)
        if cached and cached[0] == mtime_ns:
            return cached[1], cached[2]
        try:
            view_ms, title = parse_annotation_pbtxt(path.read_bytes())
        except OSError:
            return 0, ""
        self._annotation_cache[cid] = (mtime_ns, view_ms, title)
        return view_ms, title

    def _recent_annotation_ids(self, limit: int = 6) -> list[str]:
        if not self.annotations_dir.is_dir():
            return []
        entries: list[tuple[int, str]] = []
        try:
            with os.scandir(self.annotations_dir) as it:
                for entry in it:
                    if entry.name.endswith(".pbtxt") and entry.is_file():
                        try:
                            entries.append((entry.stat().st_mtime_ns, entry.name[:-6]))
                        except OSError:
                            continue
        except OSError:
            return []
        entries.sort(reverse=True)
        return [cid for _, cid in entries[:limit]]

    def _read_active_session(self, max_age_seconds: float = ACTIVE_HOOK_MAX_AGE_SECONDS) -> dict[str, Any] | None:
        path = self.active_session_path
        try:
            st = path.stat()
        except OSError:
            return None
        cached_mtime, cached_data = self._active_session_cache
        if cached_mtime != st.st_mtime_ns:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raw = None
            cached_data = raw if isinstance(raw, dict) else None
            self._active_session_cache = (st.st_mtime_ns, cached_data)
        if not cached_data:
            return None
        cid = str(cached_data.get("conversationId") or "").strip()
        if not cid:
            return None
        updated_ms = to_int(cached_data.get("updatedMs")) or int(st.st_mtime * 1000)
        if (time.time() * 1000 - updated_ms) > max_age_seconds * 1000:
            return None
        return {**cached_data, "conversationId": cid, "updatedMs": updated_ms}

    def _latest_project_switch(
        self,
        max_age_seconds: float = ACTIVE_HOOK_MAX_AGE_SECONDS,
    ) -> tuple[int, str, str, set[str]] | None:
        if not self.projects_dir.is_dir():
            return None
        entries: list[tuple[int, Path]] = []
        try:
            with os.scandir(self.projects_dir) as it:
                for entry in it:
                    if entry.name.endswith(".json") and entry.is_file():
                        try:
                            mtime_ns = entry.stat().st_mtime_ns
                        except OSError:
                            continue
                        entries.append((mtime_ns, Path(entry.path)))
        except OSError:
            return None
        if not entries:
            return None
        entries.sort(reverse=True)
        now_ms = time.time() * 1000
        for mtime_ns, path in entries:
            mtime_ms = mtime_ns // 1_000_000
            if (now_ms - mtime_ms) > max_age_seconds * 1000:
                break
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict) or data.get("sidecar"):
                continue
            resources = ((data.get("projectResources") or {}).get("resources")) or []
            workspaces = {
                norm
                for res in resources
                if isinstance(res, dict)
                for norm in (_normalize_workspace(str(res.get("folderUri") or "")),)
                if norm
            }
            if not workspaces:
                continue
            proj_id = str(data.get("id") or path.stem)
            proj_name = str(data.get("name") or "").strip()
            return mtime_ms, proj_id, proj_name, workspaces
        return None

    def _select_focused_candidate(
        self,
        candidates: list[_FocusCandidate],
        *,
        pinned_id: str,
    ) -> tuple[str, str, dict[str, Any] | None]:
        """Choose the active conversation by focus timestamps, hooks, and project switches."""
        if pinned_id and any(c.cid == pinned_id for c in candidates):
            return "existing", pinned_id, None

        hook = self._read_active_session()
        hook_cid = hook["conversationId"] if hook else ""
        hook_ms = hook["updatedMs"] if hook else 0

        def focus_for(c: _FocusCandidate) -> int:
            return max(c.view_ms, c.input_ms, hook_ms if c.cid == hook_cid else 0)

        max_focus_ms = max((focus_for(c) for c in candidates), default=0)
        proj = self._latest_project_switch()
        proj_ms = proj[0] if proj is not None else 0

        if hook and not any(c.cid == hook_cid for c in candidates) and hook_ms > max_focus_ms and hook_ms >= proj_ms - 2000:
            return "empty_hook", hook_cid, hook

        pool = candidates
        if proj is not None:
            _ms, proj_id, proj_name, proj_workspaces = proj
            if proj_ms > max_focus_ms + 2000:
                matched = [
                    c
                    for c in candidates
                    if (c.project_id and c.project_id == proj_id)
                    or (proj_workspaces and (c.workspaces & proj_workspaces))
                ]
                if matched:
                    pool = matched
                elif any(c.has_project_meta for c in candidates):
                    return "empty_project", f"project:{proj_id}", {"id": proj_id, "name": proj_name}

        if not pool:
            return "existing", "", None
        if any(focus_for(c) > 0 for c in pool):
            chosen = max(pool, key=lambda c: (focus_for(c), c.running, c.modified_ms))
        else:
            chosen = max(pool, key=lambda c: (c.running, c.modified_ms))
        return "existing", chosen.cid, None

    # ----- language server -------------------------------------------------

    def _refresh_user_status(self, max_age: float = 15.0) -> None:
        if time.monotonic() - self._user_status_at < max_age and self._labels:
            return
        data = self.ls.rpc("GetUserStatus")
        cfg = ((data.get("userStatus") or {}).get("cascadeModelConfigData")) or {}
        override = (cfg.get("defaultOverrideModelConfig") or {}).get("modelOrAlias") or {}
        self._selected_model = str(override.get("model") or override.get("alias") or self._selected_model)
        for item in cfg.get("clientModelConfigs") or []:
            alias = item.get("modelOrAlias") or {}
            key = alias.get("model") or alias.get("alias")
            if not key:
                continue
            if item.get("label"):
                self._labels[key] = str(item["label"])
            quota = item.get("quotaInfo")
            if isinstance(quota, dict):
                try:
                    pct = max(0.0, min(100.0, float(quota.get("remainingFraction", 0)) * 100.0))
                except (TypeError, ValueError):
                    pct = None
                self._quotas[key] = (pct, quota.get("resetTime"))
        self._user_status_at = time.monotonic()

    def _ls_summaries(self) -> list[tuple[str, dict[str, Any]]]:
        data = self.ls.rpc("GetAllCascadeTrajectories")
        summaries = data.get("trajectorySummaries") or {}
        items = [(cid, s) for cid, s in summaries.items() if isinstance(s, dict)]
        items.sort(
            key=lambda item: (
                RUNNING_STATUS in str(item[1].get("status")),
                parse_iso_ms(item[1].get("lastModifiedTime")) or 0,
            ),
            reverse=True,
        )
        return items

    def _ls_conversation(self, cid: str, summary: dict[str, Any]) -> ConversationUsage:
        stamp = f"{summary.get('lastModifiedTime')}|{summary.get('stepCount')}|{summary.get('status')}"
        cached = self._trajectory_cache.get(cid)
        if cached and cached[0] == stamp:
            return cached[1]
        steps, title = self._fetch_steps(cid)
        if not title:
            ann = summary.get("annotations") or {}
            _view_ms, pb_title = self._read_annotation(cid)
            title = clean_title(str(ann.get("title") or summary.get("summary") or pb_title or "")) or self._transcript_title(cid)
        meta = summary.get("trajectoryMetadata") or {}
        conversation = ConversationUsage(
            conversation_id=cid,
            title=title or f"会话 {cid[:8]}",
            status=str(summary.get("status") or ""),
            last_modified_ms=parse_iso_ms(summary.get("lastModifiedTime")) or 0,
            is_subagent=bool(meta.get("parentConversationId")),
            steps=steps,
        )
        self._trajectory_cache[cid] = (stamp, conversation)
        return conversation

    # A step that is still generating keeps changing, so every incremental
    # fetch re-reads this many trailing steps.
    REFETCH_TAIL = 2

    def _fetch_steps(self, cid: str) -> tuple[list[StepUsage], str]:
        """Fetch only steps added since the last poll (plus a short tail).

        Long conversations reach thousands of steps and tens of MB, so polling
        ``GetCascadeTrajectory`` every two seconds is wasteful.
        ``GetCascadeTrajectorySteps`` with ``stepOffset`` returns just the tail.
        """
        known = self._raw_steps.get(cid)
        parsed: list[StepUsage | None] | None = None
        title = known[2] if known else ""
        if known and known[0] > 0:
            offset = max(0, known[0] - self.REFETCH_TAIL)
            try:
                reply = self.ls.rpc("GetCascadeTrajectorySteps", {"cascadeId": cid, "stepOffset": offset}, timeout=8.0)
                raw_steps = reply.get("steps") or []
                if offset + len(raw_steps) >= known[0]:  # otherwise the trajectory shrank: refetch
                    parsed = known[1][:offset] + [parse_rpc_step(raw, self._labels) for raw in raw_steps]
                    self.incremental_fetches += 1
            except ConnectionError:
                raise
            except Exception:
                parsed = None
        if parsed is None:
            reply = self.ls.rpc("GetCascadeTrajectory", {"cascadeId": cid}, timeout=10.0)
            raw_steps = (reply.get("trajectory") or {}).get("steps") or []
            parsed = [parse_rpc_step(raw, self._labels) for raw in raw_steps]
            title = next((t for t in (user_input_title(raw) for raw in raw_steps) if t), "")
            self.full_fetches += 1
        self._raw_steps[cid] = (len(parsed), parsed, title)
        return [step for step in parsed if step is not None], title

    def _selected_label(self) -> str:
        return self._labels.get(self._selected_model) or (prettify_model(self._selected_model) if self._selected_model else "")

    def _ls_focus_candidate(self, cid: str, summary: dict[str, Any]) -> _FocusCandidate:
        ann = summary.get("annotations") or {}
        ls_view_ms = parse_iso_ms(ann.get("lastUserViewTime")) or 0
        pb_view_ms, pb_title = self._read_annotation(cid)
        input_ms = parse_iso_ms(summary.get("lastUserInputTime")) or 0
        modified_ms = parse_iso_ms(summary.get("lastModifiedTime")) or 0
        meta = summary.get("trajectoryMetadata") or {}
        ws_set: set[str] = set()
        for ws in (summary.get("workspaces") or meta.get("workspaces") or []):
            if isinstance(ws, dict):
                norm = _normalize_workspace(str(ws.get("workspaceFolderAbsoluteUri") or ""))
                if norm:
                    ws_set.add(norm)
        for uri in (meta.get("workspaceUris") or []):
            if isinstance(uri, str):
                norm = _normalize_workspace(uri)
                if norm:
                    ws_set.add(norm)
        project_id = str(meta.get("projectId") or "")
        title = clean_title(str(ann.get("title") or summary.get("summary") or pb_title or ""))
        return _FocusCandidate(
            cid=cid,
            title=title,
            running=RUNNING_STATUS in str(summary.get("status")),
            modified_ms=modified_ms,
            view_ms=max(ls_view_ms, pb_view_ms),
            input_ms=input_ms,
            project_id=project_id,
            workspaces=ws_set,
            has_project_meta=bool(project_id or ws_set),
        )

    def _live_from_ls(self, pinned_id: str) -> LiveSnapshot | None:
        self._refresh_user_status()
        summaries = [item for item in self._ls_summaries() if not (item[1].get("trajectoryMetadata") or {}).get("parentConversationId")]
        if not summaries:
            return None
        summary_map = dict(summaries)
        candidates = [self._ls_focus_candidate(cid, summary) for cid, summary in summaries]
        kind, target, extra = self._select_focused_candidate(candidates, pinned_id=pinned_id)
        listing = [(cid, self._title_for(cid, summary_map.get(cid))) for cid, _ in summaries[:12]]
        if kind == "empty_hook" and extra is not None:
            title = clean_title(str(extra.get("lastUserInput") or extra.get("title") or "")) or "新对话"
            conversation = ConversationUsage(conversation_id=target, title=title, status="CASCADE_RUN_STATUS_IDLE", steps=[])
        elif kind == "empty_project" and extra is not None:
            name = str(extra.get("name") or "").strip()
            title = f"{name} · 新对话" if name else "新对话"
            conversation = ConversationUsage(conversation_id=target, title=title, status="CASCADE_RUN_STATUS_IDLE", steps=[])
        else:
            if target not in summary_map:
                target = summaries[0][0]
            if not any(cid == target for cid, _ in listing):
                listing.insert(0, (target, self._title_for(target, summary_map.get(target))))
            conversation = self._ls_conversation(target, summary_map[target])
        quota_key = self._selected_model or (conversation.steps[-1].model_id if conversation.steps else "")
        return summarize_conversation(
            conversation,
            source="ls",
            selected_model_label=self._selected_label(),
            quota=self._quotas.get(quota_key, (None, None)),
            conversations=listing,
        )

    def _title_for(self, cid: str, summary: dict[str, Any] | None = None) -> str:
        cached = self._trajectory_cache.get(cid)
        if cached:
            return cached[1].title
        if isinstance(summary, dict):
            ann = summary.get("annotations") or {}
            title = clean_title(str(ann.get("title") or summary.get("summary") or ""))
            if title:
                return title
        _view_ms, pb_title = self._read_annotation(cid)
        return pb_title or self._transcript_title(cid) or f"会话 {cid[:8]}"

    # ----- disk fallbacks ----------------------------------------------------

    def _transcript_path(self, cid: str) -> Path:
        return self.brain_dir / cid / ".system_generated" / "logs" / "transcript.jsonl"

    def _transcript_title(self, cid: str) -> str:
        path = self._transcript_path(cid)
        try:
            with path.open(encoding="utf-8") as handle:
                for _ in range(5):
                    line = handle.readline()
                    if not line:
                        break
                    obj = json.loads(line)
                    if obj.get("type") == "USER_INPUT" and obj.get("content"):
                        return clean_title(str(obj["content"]))
        except (OSError, ValueError):
            pass
        return ""

    def _transcript_conversation(self, path: Path) -> ConversationUsage | None:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return None
        key = str(path)
        cached = self._transcript_cache.get(key)
        if cached and cached[0] == mtime:
            return cached[1]
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return None
        steps, title = parse_transcript_lines(lines)
        cid = path.parents[2].name
        conversation = ConversationUsage(
            conversation_id=cid,
            title=title or f"会话 {cid[:8]}",
            status="IDLE",
            last_modified_ms=int(mtime * 1000),
            steps=steps,
        )
        self._transcript_cache[key] = (mtime, conversation)
        return conversation

    def _transcript_conversations(self, limit: int = 60) -> list[ConversationUsage]:
        if not self.brain_dir.is_dir():
            return []
        paths = []
        for child in self.brain_dir.iterdir():
            path = child / ".system_generated" / "logs" / "transcript.jsonl"
            try:
                paths.append((path.stat().st_mtime, path))
            except OSError:
                continue
        paths.sort(reverse=True)
        result = []
        for _mtime, path in paths[:limit]:
            conversation = self._transcript_conversation(path)
            if conversation is not None:
                result.append(conversation)
        return result

    def _empty_sidecar_snapshot(
        self,
        payload: dict[str, Any],
        *,
        source: str,
        cid: str,
        title: str,
        conversations: list[tuple[str, str]],
    ) -> LiveSnapshot:
        active = payload.get("active") if isinstance(payload.get("active"), dict) else {}
        selected_label = str(payload.get("selectedModelLabel") or active.get("selectedModelLabel") or "")
        quota_pct = payload.get("selectedQuotaRemainingPct")
        if not isinstance(quota_pct, (int, float)):
            quota_pct = active.get("quotaRemainingPct") if isinstance(active.get("quotaRemainingPct"), (int, float)) else None
        quota_reset = payload.get("selectedQuotaResetTime") or active.get("quotaResetTime")
        return LiveSnapshot(
            source=source,
            conversation_id=cid,
            title=title,
            status="CASCADE_RUN_STATUS_IDLE",
            selected_model_label=selected_label,
            last_step_model_label="",
            quota_remaining_pct=float(quota_pct) if isinstance(quota_pct, (int, float)) else None,
            quota_reset_time=quota_reset,
            quota_windows=parse_quota_buckets(payload.get("quotaBuckets")),
            context_used_tokens=0,
            context_limit_tokens=infer_context_limit(selected_label),
            cumulative_total_tokens=0,
            conversations=conversations,
        )

    def _live_from_sidecar_file(self, path: Path, pinned_id: str, *, source: str) -> LiveSnapshot | None:
        try:
            if time.time() - path.stat().st_mtime > HUD_LIVE_MAX_AGE_SECONDS:
                return None
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None

        raw_convs = [c for c in (payload.get("conversations") or []) if isinstance(c, dict) and c.get("conversationId")]
        active = payload.get("active") if isinstance(payload.get("active"), dict) else None
        if active and active.get("conversationId") and not any(c.get("conversationId") == active.get("conversationId") for c in raw_convs):
            raw_convs = [active, *raw_convs]
        if not raw_convs:
            return snapshot_from_hud_live(payload, pinned_id=pinned_id, source=source)

        candidates: list[_FocusCandidate] = []
        for entry in raw_convs:
            cid = str(entry.get("conversationId") or "")
            pb_view_ms, pb_title = self._read_annotation(cid)
            entry_view_ms = to_int(entry.get("lastUserViewMs")) or (parse_iso_ms(entry.get("lastUserViewTime")) or 0)
            entry_input_ms = to_int(entry.get("lastUserInputMs")) or (parse_iso_ms(entry.get("lastUserInputTime")) or 0)
            mod_ms = parse_iso_ms(entry.get("lastModifiedTime")) or 0
            ws_set = {
                norm
                for u in (entry.get("workspaceUris") or [])
                if isinstance(u, str)
                for norm in (_normalize_workspace(u),)
                if norm
            }
            project_id = str(entry.get("projectId") or "")
            status = str(entry.get("status") or "")
            candidates.append(
                _FocusCandidate(
                    cid=cid,
                    title=str(entry.get("title") or pb_title or ""),
                    running=RUNNING_STATUS in status or status.upper() == "RUNNING",
                    modified_ms=mod_ms,
                    view_ms=max(entry_view_ms, pb_view_ms),
                    input_ms=entry_input_ms,
                    project_id=project_id,
                    workspaces=ws_set,
                    has_project_meta=bool(project_id or ws_set),
                )
            )

        # If user clicked an older conversation in sidebar not in top-N sidecar cache, include it in candidates.
        extra_convs: dict[str, ConversationUsage] = {}
        if not pinned_id:
            known_ids = {c.cid for c in candidates}
            max_known_focus = max((max(c.view_ms, c.input_ms) for c in candidates), default=0)
            for ann_cid in self._recent_annotation_ids(limit=3):
                if ann_cid in known_ids:
                    continue
                ann_view_ms, ann_title = self._read_annotation(ann_cid)
                if ann_view_ms > max_known_focus:
                    conv = self._transcript_conversation(self._transcript_path(ann_cid))
                    if conv is not None:
                        if ann_title and conv.title.startswith("会话 "):
                            conv.title = clean_title(ann_title)
                        extra_convs[ann_cid] = conv
                        candidates.append(
                            _FocusCandidate(
                                cid=ann_cid,
                                title=conv.title,
                                running=conv.running,
                                modified_ms=conv.last_modified_ms,
                                view_ms=ann_view_ms,
                                input_ms=0,
                            )
                        )

        kind, target_cid, extra = self._select_focused_candidate(candidates, pinned_id=pinned_id)
        listing = [
            (str(c.get("conversationId")), str(c.get("title") or c.get("conversationId", "")[:8]))
            for c in raw_convs
        ]
        if kind == "existing" and target_cid in extra_convs:
            selected_label = str(payload.get("selectedModelLabel") or (active.get("selectedModelLabel") if active else "") or "")
            return summarize_conversation(
                extra_convs[target_cid],
                source="transcript",
                selected_model_label=selected_label,
                quota_windows=parse_quota_buckets(payload.get("quotaBuckets")),
                conversations=listing,
            )
        if kind == "empty_hook" and extra is not None:
            title = clean_title(str(extra.get("lastUserInput") or extra.get("title") or "")) or "新对话"
            return self._empty_sidecar_snapshot(payload, source=source, cid=target_cid, title=title, conversations=listing)
        if kind == "empty_project" and extra is not None:
            name = str(extra.get("name") or "").strip()
            title = f"{name} · 新对话" if name else "新对话"
            return self._empty_sidecar_snapshot(payload, source=source, cid=target_cid, title=title, conversations=listing)
        effective_pin = pinned_id or target_cid
        snap = snapshot_from_hud_live(payload, pinned_id=effective_pin, source=source)
        if snap is not None and snap.conversation_id == target_cid and target_cid:
            _v, pb_title = self._read_annotation(target_cid)
            if pb_title and snap.title.startswith("会话 "):
                snap.title = clean_title(pb_title)
        return snap

    def _live_from_hud_file(self, pinned_id: str) -> LiveSnapshot | None:
        return self._live_from_sidecar_file(self.hud_live_path, pinned_id, source="hud_live")

    def _live_from_pet_file(self, pinned_id: str) -> LiveSnapshot | None:
        return self._live_from_sidecar_file(self.pet_live_path, pinned_id, source="pet_live")

    def _live_from_transcripts(self, pinned_id: str) -> LiveSnapshot | None:
        conversations = self._transcript_conversations(limit=12)
        known_ids = {c.conversation_id for c in conversations}
        extra_ids = []
        if pinned_id and pinned_id not in known_ids:
            extra_ids.append(pinned_id)
        for ann_cid in self._recent_annotation_ids(limit=4):
            if ann_cid not in known_ids and ann_cid not in extra_ids:
                extra_ids.append(ann_cid)
        for cid in extra_ids:
            conv = self._transcript_conversation(self._transcript_path(cid))
            if conv is not None:
                conversations.append(conv)
        if not conversations:
            return None
        conv_map = {c.conversation_id: c for c in conversations}
        candidates: list[_FocusCandidate] = []
        for conv in conversations:
            view_ms, pb_title = self._read_annotation(conv.conversation_id)
            if pb_title and conv.title.startswith("会话 "):
                conv.title = clean_title(pb_title)
            candidates.append(
                _FocusCandidate(
                    cid=conv.conversation_id,
                    title=conv.title,
                    running=conv.running,
                    modified_ms=conv.last_modified_ms,
                    view_ms=view_ms,
                    input_ms=0,
                )
            )
        listing = [(c.conversation_id, c.title) for c in conversations[:12]]
        kind, target_cid, extra = self._select_focused_candidate(candidates, pinned_id=pinned_id)
        if kind == "empty_hook" and extra is not None:
            title = clean_title(str(extra.get("lastUserInput") or extra.get("title") or "")) or "新对话"
            target = ConversationUsage(conversation_id=target_cid, title=title, status="IDLE", steps=[])
        elif kind == "empty_project" and extra is not None:
            name = str(extra.get("name") or "").strip()
            title = f"{name} · 新对话" if name else "新对话"
            target = ConversationUsage(conversation_id=target_cid, title=title, status="IDLE", steps=[])
        else:
            target = conv_map.get(target_cid) or conversations[0]
        return summarize_conversation(
            target,
            source="transcript",
            conversations=listing,
        )

    # ----- public API -------------------------------------------------------------

    def live_snapshot(self, pinned_id: str = "") -> LiveSnapshot | None:
        # Plugin channel first: when pet-bridge (or token-meter) is actively
        # writing fresh snapshots, skip standalone process scanning / LS RPC.
        sidecar = self._live_from_pet_file(pinned_id) or self._live_from_hud_file(pinned_id)
        if sidecar is not None:
            return sidecar
        # Standalone fallback: direct language-server RPC, then offline transcripts.
        if self.use_language_server:
            try:
                snapshot = self._live_from_ls(pinned_id)
                if snapshot is not None:
                    return snapshot
            except Exception:  # any LS failure must fall through to offline logs
                pass
        return self._live_from_transcripts(pinned_id)

    def meter_report(self, *, include_subagents: bool = True, pinned_id: str = "", limit: int = 40) -> MeterReport:
        live = self.live_snapshot(pinned_id)
        if self.use_language_server and self.ls.endpoint is not None:
            try:
                self._refresh_user_status()
                conversations = []
                for cid, summary in self._ls_summaries()[:limit]:
                    is_sub = bool((summary.get("trajectoryMetadata") or {}).get("parentConversationId"))
                    if is_sub and not include_subagents:
                        continue
                    conversations.append(self._ls_conversation(cid, summary))
                active_id = live.conversation_id if live else ""
                active = next((c for c in conversations if c.conversation_id == active_id), None)
                return MeterReport("ls", build_model_rows(conversations), len(conversations), active, live, conversations=conversations)
            except Exception:  # any LS failure must fall through to file sources
                pass
        conversations = self._transcript_conversations(limit=limit)
        active_id = live.conversation_id if live else ""
        active = next((c for c in conversations if c.conversation_id == active_id), conversations[0] if conversations else None)
        return MeterReport("transcript", build_model_rows(conversations), len(conversations), active, live, conversations=conversations)
