"""Developer assignment recommender (FR-12, FR-23, FR-24, FR-26 to FR-28).

Three operating modes, chosen per project:
  manual      cold start and no declared profile data: no recommendations (FR-26)
  rule_based  cold start with declared skills and components: keyword and component matching only (FR-27)
  model       the trained CNN-LSTM combined with the developer profile (FR-24)

In model mode every candidate is scored from the profile inputs named in FR-24
(similarity to the developer's history, component overlap, current workload)
together with the model's probability. The duplicate flag and severity level
feed both the model (as input features) and the scoring: a probable duplicate
favours whoever handled the original, and higher severity weights availability
and spare capacity more heavily (Section 2.4.5).
"""
from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..config import get_settings
from ..models import (
    BUSY, ROLE_DEVELOPER, UNAVAILABLE, Bug, DeveloperProfile, ProjectMember, ResolutionHistory, User,
)
from ..schemas import ExcludedDeveloper, Recommendation, RecommendationsOut
from ..services import coldstart, profiles, settings_service
from .text import content_tokens, extract_keywords, severity_rank


@dataclass
class Candidate:
    user: User
    profile: DeveloperProfile
    open_bugs: int
    history: list[ResolutionHistory]
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    limited_history: bool = False
    declared_match: float = 0.0


def _declared_matches(profile: DeveloperProfile, bug: Bug, tokens: set[str], text_lower: str) -> tuple[list[str], list[str]]:
    """Declared skills and components that this bug mentions."""
    skills = [s.name for s in profile.skills
              if s.name.lower() in tokens or (len(s.name) > 3 and s.name.lower() in text_lower)]
    component = (bug.component or "").lower()
    components = [c.name for c in profile.components
                  if c.name.lower() == component or c.name.lower() in tokens or (len(c.name) > 3 and c.name.lower() in text_lower)]
    return skills, components


def _load_candidates(db: Session, bug: Bug) -> tuple[list[Candidate], list[ExcludedDeveloper]]:
    users = list(db.scalars(
        select(User).join(ProjectMember, ProjectMember.user_id == User.id)
        .where(ProjectMember.project_id == bug.project_id, ProjectMember.role == ROLE_DEVELOPER, User.is_active.is_(True))
    ))
    if not users:
        return [], []
    for user in users:
        profiles.ensure_profile(db, user)
    db.flush()
    profile_rows = db.scalars(
        select(DeveloperProfile)
        .where(DeveloperProfile.user_id.in_([u.id for u in users]))
        .options(selectinload(DeveloperProfile.skills), selectinload(DeveloperProfile.components))
    )
    by_user = {p.user_id: p for p in profile_rows}
    history: dict[int, list[ResolutionHistory]] = {}
    for entry in db.scalars(select(ResolutionHistory).where(
        ResolutionHistory.project_id == bug.project_id,
        ResolutionHistory.profile_id.in_([p.id for p in by_user.values()]),
    )):
        history.setdefault(entry.profile_id, []).append(entry)
    counts = profiles.open_counts(db, [u.id for u in users])

    candidates, excluded = [], []
    for user in users:
        profile = by_user[user.id]
        if not profile.active:
            excluded.append(ExcludedDeveloper(user_id=user.id, display_name=user.display_name, reason="Profile deactivated"))
        elif profile.availability == UNAVAILABLE:
            excluded.append(ExcludedDeveloper(user_id=user.id, display_name=user.display_name, reason="Marked Unavailable"))
        elif counts[user.id] >= profile.max_capacity:
            excluded.append(ExcludedDeveloper(
                user_id=user.id, display_name=user.display_name,
                reason=f"At capacity ({counts[user.id]} of {profile.max_capacity} open bugs)"))
        else:
            candidates.append(Candidate(user, profile, counts[user.id], history.get(profile.id, [])))
    return candidates, excluded


def _to_schema(c: Candidate) -> Recommendation:
    return Recommendation(
        user_id=c.user.id, display_name=c.user.display_name, score=round(max(c.score, 0.0), 3), reasons=c.reasons,
        limited_history=c.limited_history, open_bugs=c.open_bugs, capacity=c.profile.max_capacity,
        availability=c.profile.availability,
    )


def _workload_reason(c: Candidate) -> str:
    text = f"Workload: {c.open_bugs} of {c.profile.max_capacity} open bugs"
    return text + " (marked Busy)" if c.profile.availability == BUSY else text


def _rule_based(candidates: list[Candidate], bug: Bug) -> list[Candidate]:
    searchable = bug.text + " " + (bug.component or "")
    tokens = set(content_tokens(searchable))
    text_lower = searchable.lower()
    scored = []
    for c in candidates:
        skills, components = _declared_matches(c.profile, bug, tokens, text_lower)
        if not skills and not components:
            continue
        skill_part = min(1.0, len(skills) / 3)
        c.score = 0.6 * (1.0 if components else 0.0) + 0.4 * skill_part
        if components:
            c.reasons.append("Declared component match: " + ", ".join(components))
        if skills:
            c.reasons.append("Declared skill match: " + ", ".join(skills))
        c.reasons.append(_workload_reason(c))
        scored.append(c)
    return sorted(scored, key=lambda c: (-c.score, c.open_bugs, c.user.id))[:3]


def _model_based(db: Session, candidates: list[Candidate], bug: Bug, model, labels: list[str]) -> list[Candidate]:
    settings = get_settings()
    rank = severity_rank(bug.severity, labels)
    urgency = (rank or 0) / 3
    searchable = bug.text + " " + (bug.component or "")
    tokens = set(content_tokens(searchable))
    text_lower = searchable.lower()
    bug_keywords = set(bug.keywords or extract_keywords(bug.text))
    component = (bug.component or "").lower()

    probabilities = model.probabilities(bug.text, rank, bug.is_duplicate_flagged) if model else {}
    top_p = max((probabilities.get(c.user.id, 0.0) for c in candidates), default=0.0)

    original_id = bug.duplicate_of_id or bug.probable_duplicate_of_id
    original = db.get(Bug, original_id) if original_id else None

    # A keyword found in every developer's history says nothing about who should take the bug,
    # so each keyword's weight is shared between the developers whose history contains it.
    history_keywords = {c.user.id: {k for h in c.history for k in h.keywords} for c in candidates}
    holders = Counter(k for keywords in history_keywords.values() for k in keywords)

    for c in candidates:
        n_history = len(c.history)
        c.limited_history = n_history < settings.limited_history_threshold
        skills, components = _declared_matches(c.profile, bug, tokens, text_lower)
        c.declared_match = (1.0 if components else 0.0) * 0.6 + min(1.0, len(skills) / 3) * 0.4

        # 1. similarity between the new report and the developer's historical record
        shared = [k for k in bug_keywords if k in history_keywords[c.user.id]]
        history_sim = sum(1 / holders[k] for k in shared) / len(bug_keywords) if bug_keywords else 0.0
        distinctive = sorted(shared, key=lambda k: (holders[k], k))
        # 2. component overlap: declared association, or share of past fixes in this component
        same_component = sum(1 for h in c.history if component and (h.component or "").lower() == component)
        component_score = 1.0 if components else (same_component / n_history if n_history else 0.0)
        # 3. current workload
        load = c.open_bugs / c.profile.max_capacity
        p = probabilities.get(c.user.id, 0.0)
        p_rel = p / top_p if top_p > 0 else 0.0

        score = 0.40 * p_rel + 0.25 * history_sim + 0.15 * component_score + 0.05 * min(1.0, len(skills) / 3)
        score -= (0.10 + 0.20 * urgency) * load
        if c.profile.availability == BUSY:
            score -= 0.05 + 0.15 * urgency

        if original is not None and original.assignee_id == c.user.id:
            score += 0.25
            kind = "confirmed" if bug.duplicate_of_id else "probable"
            c.reasons.append(f"Assigned to bug #{original.id}, of which this is a {kind} duplicate")
        if p > 0:
            c.reasons.append(f"Assignment model confidence {p:.0%}")
        if same_component:
            c.reasons.append(f"Resolved {same_component} earlier bug{'s' if same_component != 1 else ''} in component '{bug.component}'")
        elif components:
            c.reasons.append("Associated with component: " + ", ".join(components))
        if shared:
            c.reasons.append(f"History matches this report on: {', '.join(distinctive[:5])} (similarity {history_sim:.0%})")
        same_severity = sum(1 for h in c.history if h.severity == bug.severity)
        if same_severity and rank is not None:
            c.reasons.append(f"{same_severity} earlier {bug.severity}-severity fix{'es' if same_severity != 1 else ''}")
        if skills:
            c.reasons.append("Declared skill match: " + ", ".join(skills))
        if c.limited_history:
            c.reasons.append(f"Limited history ({n_history} verified bug{'s' if n_history != 1 else ''} here): ranked on declared skills and components")
        c.reasons.append(_workload_reason(c) + ("; weighted heavily at this severity" if urgency >= 0.67 else ""))
        c.score = score

    ranked = sorted(candidates, key=lambda c: (-c.score, c.open_bugs, c.user.id))
    top = ranked[:3]
    # FR-28: a new developer whose declared skills match must get a chance to build a history.
    if not any(c.limited_history for c in top):
        newcomers = [c for c in ranked[3:] if c.limited_history and c.declared_match > 0]
        if newcomers:
            newcomer = max(newcomers, key=lambda c: (c.declared_match, -c.open_bugs))
            newcomer.reasons.insert(0, "Included so a new developer with matching declared skills can build a history")
            top = top[:2] + [newcomer]
    return top


def recommend(db: Session, bug: Bug) -> RecommendationsOut:
    state = coldstart.get_state(db, bug.project_id)
    cold = coldstart.to_out(state)
    candidates, excluded = _load_candidates(db, bug)
    base = {"excluded": excluded, "cold_start": cold, "elapsed_ms": 0.0}

    if not candidates and not excluded:
        return RecommendationsOut(mode="manual", message="This project has no members with the Developer role yet.",
                                  recommendations=[], **base)
    if cold.cold_start:
        why = (
            f"The assignment model is not available yet: {cold.verified_count} of {cold.threshold} verified bugs "
            f"with an assignee have been recorded for this project ({cold.remaining} more needed)."
            if cold.remaining else "The assignment model is being trained for the first time."
        )
        all_profiles = [c.profile for c in candidates]
        if not any(p.skills and p.components for p in all_profiles):
            return RecommendationsOut(
                mode="manual",
                message=why + " Assign manually; every assignment is recorded and becomes training data.",
                recommendations=[], **base)
        top = _rule_based(candidates, bug)
        message = why + (
            " These are rule-based suggestions from declared skills and components only; they carry no historical evidence."
            if top else " No declared skills or components match this bug, so there is no rule-based suggestion. Assign manually.")
        return RecommendationsOut(mode="rule_based", message=message, recommendations=[_to_schema(c) for c in top], **base)

    model = coldstart.load_model(db, state)
    if model is None:
        return RecommendationsOut(mode="unavailable", message="The trained assignment model could not be loaded. Assign manually.",
                                  recommendations=[], **base)
    top = _model_based(db, candidates, bug, model, settings_service.severity_labels(db))
    message = "Recommendations from the trained assignment model and developer profiles. The final decision is yours."
    if not top:
        message = "Every developer on this project is at capacity or unavailable. You can still assign manually."
    return RecommendationsOut(mode="model", message=message, recommendations=[_to_schema(c) for c in top], **base)
