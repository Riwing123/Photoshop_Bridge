from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
STYLE_CARDS_PATH = ROOT / "style_cards" / "style_cards.json"


def _error(code: str, message: str, details: Any | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "error",
        "schema_version": "ps-agent/v1",
        "error": {"code": code, "message": message},
    }
    if details is not None:
        payload["error"]["details"] = details
    return payload


def load_style_cards(path: Path = STYLE_CARDS_PATH) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    cards = data.get("cards")
    return cards if isinstance(cards, list) else []


def _tokens(value: Any) -> set[str]:
    text = str(value or "").lower()
    tokens = set(re.findall(r"[a-z0-9_+-]+", text))
    cjk = re.findall(r"[\u4e00-\u9fff]", text)
    tokens.update(cjk)
    for index in range(max(0, len(cjk) - 1)):
        tokens.add(cjk[index] + cjk[index + 1])
    return {token for token in tokens if token.strip()}


def _normalized_text(value: Any) -> str:
    text = str(value or "").lower()
    return re.sub(r"\s+", " ", text).strip()


def _phrase_values(card: dict[str, Any], field: str) -> list[str]:
    values = card.get(field)
    if not isinstance(values, list):
        return []
    return [str(value).strip().lower() for value in values if str(value).strip()]


def _positive_phrases(card: dict[str, Any]) -> list[str]:
    phrases = [
        str(card.get("id") or "").replace("_", " ").strip().lower(),
        str(card.get("title") or "").strip().lower(),
    ]
    phrases.extend(_phrase_values(card, "aliases"))
    phrases.extend(_phrase_values(card, "keywords"))
    return [phrase for phrase in phrases if phrase]


def _phrase_matches(query_text: str, phrases: list[str]) -> list[str]:
    matches: list[str] = []
    for phrase in phrases:
        if not phrase:
            continue
        if phrase in query_text:
            matches.append(phrase)
    return sorted(set(matches))


def _card_tokens(card: dict[str, Any]) -> set[str]:
    values: list[Any] = [
        card.get("id"),
        card.get("title"),
        card.get("visual_goal"),
    ]
    values.extend(card.get("aliases") or [])
    values.extend(card.get("keywords") or [])
    values.extend(card.get("scene_types") or [])
    values.extend(card.get("recommended_operations") or [])
    values.extend(card.get("scoring_focus") or [])
    tokens: set[str] = set()
    for value in values:
        tokens.update(_tokens(value))
    return tokens


def _parameter_average(cards: list[dict[str, Any]]) -> dict[str, float]:
    sums: dict[str, float] = {}
    counts: dict[str, int] = {}
    for card in cards:
        bias = card.get("parameter_bias")
        if not isinstance(bias, dict):
            continue
        for key, value in bias.items():
            if isinstance(value, (int, float)):
                sums[key] = sums.get(key, 0.0) + float(value)
                counts[key] = counts.get(key, 0) + 1
    return {key: round(value / max(1, counts[key]), 4) for key, value in sums.items()}


def retrieve_style_guidance(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        cards = load_style_cards()
    except Exception as exc:
        return _error("style_cards_unavailable", str(exc), {"path": str(STYLE_CARDS_PATH)})

    user_goal = payload.get("user_goal") or payload.get("goal") or ""
    visual_brief = payload.get("visual_brief") or payload.get("codex_visual_brief") or ""
    scene_tags = payload.get("scene_tags") if isinstance(payload.get("scene_tags"), list) else []
    risk_flags: list[Any] = []
    target_metrics = payload.get("target_metrics")
    if isinstance(target_metrics, dict):
        global_metrics = target_metrics.get("global_metrics") if isinstance(target_metrics.get("global_metrics"), dict) else target_metrics
        if isinstance(global_metrics, dict) and isinstance(global_metrics.get("risk_flags"), list):
            risk_flags = global_metrics["risk_flags"]

    query_values: list[Any] = [user_goal, visual_brief]
    query_values.extend(scene_tags)
    query_values.extend(risk_flags)
    query_tokens: set[str] = set()
    for value in query_values:
        query_tokens.update(_tokens(value))
    user_goal_text = _normalized_text(user_goal)
    visual_brief_text = _normalized_text(visual_brief)
    query_text = _normalized_text(" ".join(str(value) for value in query_values if value is not None))

    limit = int(payload.get("limit") or 3)
    limit = max(1, min(limit, 8))

    scored: list[tuple[float, dict[str, Any], list[str], list[str]]] = []
    scene_set = {str(tag).lower() for tag in scene_tags}
    for card in cards:
        tokens = _card_tokens(card)
        matches = sorted(query_tokens & tokens)
        score = float(len(matches)) * 0.25
        reasons: list[str] = []

        goal_phrase_matches = _phrase_matches(user_goal_text, _positive_phrases(card))
        if goal_phrase_matches:
            score += 12.0 + 2.0 * len(goal_phrase_matches)
            matches.extend(goal_phrase_matches)
            reasons.append("goal_phrase:" + ",".join(goal_phrase_matches[:3]))

        brief_phrase_matches = _phrase_matches(visual_brief_text, _positive_phrases(card))
        if brief_phrase_matches:
            score += 5.0 + float(len(brief_phrase_matches))
            matches.extend(brief_phrase_matches)
            reasons.append("brief_phrase:" + ",".join(brief_phrase_matches[:3]))

        negative_matches = _phrase_matches(query_text, _phrase_values(card, "hard_negative_keywords"))
        if negative_matches:
            score -= 10.0 * len(negative_matches)
            reasons.append("negative:" + ",".join(negative_matches[:3]))

        card_scene = {str(tag).lower() for tag in card.get("scene_types") or []}
        scene_matches = sorted(scene_set & card_scene)
        if scene_matches:
            score += 2.0 * len(scene_matches)
            matches.extend(scene_matches)
            reasons.append("scene:" + ",".join(scene_matches[:3]))
        if matches and not reasons:
            reasons.append("token:" + ",".join(matches[:5]))
        if score <= 0 and not query_tokens:
            score = 0.1
            reasons.append("default")
        scored.append((score, card, sorted(set(matches)), reasons))

    selected = [
        {
            "score": round(score, 3),
            "matched_terms": matched_terms,
            "selected_reason": "; ".join(reasons) if reasons else "weak_token_match",
            "card": card,
        }
        for score, card, matched_terms, reasons in sorted(scored, key=lambda item: item[0], reverse=True)[:limit]
        if score > 0
    ]
    if not selected and cards:
        selected = [{"score": 0.0, "matched_terms": [], "selected_reason": "fallback_first_card", "card": cards[0]}]

    selected_cards = [item["card"] for item in selected]
    guidance = {
        "style_card_ids": [card.get("id") for card in selected_cards],
        "selected_reasons": [
            {
                "style_card_id": item["card"].get("id"),
                "reason": item.get("selected_reason"),
                "score": item.get("score"),
            }
            for item in selected
        ],
        "recommended_operations": sorted(
            {
                str(operation)
                for card in selected_cards
                for operation in (card.get("recommended_operations") or [])
            }
        ),
        "avoid": sorted({str(item) for card in selected_cards for item in (card.get("avoid") or [])}),
        "scoring_focus": sorted(
            {str(item) for card in selected_cards for item in (card.get("scoring_focus") or [])}
        ),
        "parameter_bias": _parameter_average(selected_cards),
    }

    return {
        "status": "ok",
        "schema_version": "ps-agent/v1",
        "query": {
            "user_goal": user_goal,
            "visual_brief": visual_brief,
            "scene_tags": scene_tags,
        },
        "matches": selected,
        "guidance": guidance,
    }
