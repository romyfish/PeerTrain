from dataclasses import dataclass


EVIDENCE_RUBRIC_VERSION = "BOUNDARY-EVIDENCE-1.0"
SCORE_MIN = 1
SCORE_MAX = 5
BOUNDARY_KEYS = ("agency", "relationship", "safety")


@dataclass(frozen=True)
class ScoreBand:
    score: int
    label: str
    definition: str


@dataclass(frozen=True)
class BoundaryRubric:
    key: str
    name: str
    practice_context: str
    description: str
    process_moves: tuple[tuple[str, str], ...]
    positive_evidence: tuple[str, ...]
    major_concerns: tuple[str, ...]
    critical_concerns: tuple[str, ...]


SCORE_BANDS = (
    ScoreBand(
        5,
        "Very steady",
        "The expected boundary move is fully and specifically met; the reply is "
        "collaborative and contains no boundary concern.",
    ),
    ScoreBand(
        4,
        "Mostly steady",
        "The expected boundary move is met, but one minor element such as clarity, "
        "specificity, consent, or collaboration could be stronger.",
    ),
    ScoreBand(
        3,
        "Mixed",
        "The reply is safe and relevant but only partly completes the expected "
        "boundary move, or balances helpful and unhelpful elements.",
    ),
    ScoreBand(
        2,
        "Wobbling",
        "A genuine boundary opportunity is missed or a major concern is present, "
        "but the reply does not create the critical or potentially harmful breach "
        "reserved for Score 1.",
    ),
    ScoreBand(
        1,
        "At risk",
        "A critical boundary breach or potentially harmful response occurs. Use "
        "only when the boundary-specific critical-concern definition is evidenced.",
    ),
)


BOUNDARY_RUBRICS = {
    "agency": BoundaryRubric(
        key="agency",
        name="Agency Boundary",
        practice_context="Support without fixing",
        description=(
            "Support the help-seeker through active listening, validation, and open "
            "exploration while keeping decisions, attention, and ownership of the "
            "problem with them."
        ),
        process_moves=(
            ("exploration", "Explore the help-seeker's perspective and priorities"),
            ("choice", "Offer choices or invitations rather than instructions"),
            ("focus", "Keep the focus on the help-seeker"),
        ),
        positive_evidence=(
            "Listens, paraphrases, reflects feelings, or asks an open question.",
            "Acknowledges distress without minimising, diagnosing, or judging it.",
            "Checks consent before advice or self-disclosure.",
            "Returns choice and decision ownership to the help-seeker.",
            "Uses self-disclosure only when invited and keeps it brief and relevant.",
        ),
        major_concerns=(
            "Uses directive fixing or advice without checking whether it is wanted.",
            "Centres the supporter's own story and displaces the help-seeker's experience.",
            "Diagnoses, judges, or minimises the help-seeker's distress.",
        ),
        critical_concerns=(
            "Coerces a choice or takes control of a consequential decision.",
            "Seriously invalidates the distress or removes the help-seeker's agency in "
            "a way that could cause harm.",
        ),
    ),
    "relationship": BoundaryRubric(
        key="relationship",
        name="Relationship Boundary",
        practice_context="Care without overextending",
        description=(
            "Offer warm, respectful support while keeping time, access, privacy, "
            "availability, and responsibility within clear and sustainable peer-support "
            "limits."
        ),
        process_moves=(
            ("warmth", "Acknowledge the person's need for support"),
            ("limit", "State a clear and sustainable role or availability limit"),
            ("alternative", "Agree a realistic alternative support option"),
        ),
        positive_evidence=(
            "Communicates care before or alongside a limit.",
            "States clearly what support can and cannot be offered.",
            "Protects personal contact details, privacy, time, and supporter capacity.",
            "Avoids becoming the person's only source of support.",
            "Collaborates on another person, service, time, or support option.",
        ),
        major_concerns=(
            "Leaves time, contact, privacy, availability, or role responsibility vague "
            "when a limit is genuinely needed.",
            "Sets a limit coldly without acknowledging the person's support need or "
            "offering a workable alternative.",
        ),
        critical_concerns=(
            "Promises unlimited access or accepts sole-support responsibility.",
            "Promises absolute secrecy when safety or safeguarding may require sharing.",
            "Crosses a serious personal-contact, privacy, or dependency boundary in a "
            "potentially harmful way.",
        ),
    ),
    "safety": BoundaryRubric(
        key="safety",
        name="Safety Boundary",
        practice_context="Escalate without abandoning",
        description=(
            "Recognise when risk exceeds what peer support can safely hold, check "
            "immediate safety when warranted, and connect the person with appropriate "
            "additional support while remaining calm, transparent, and emotionally present."
        ),
        process_moves=(
            ("safety_check", "Check immediate safety directly and proportionately"),
            ("connection", "Stay emotionally connected and explain any confidentiality limit"),
            ("action", "Agree a concrete and collaborative next support step"),
        ),
        positive_evidence=(
            "Notices explicit or credible cues of self-harm, suicide, abuse, exploitation, "
            "intoxication, or inability to stay safe.",
            "Asks a clear, non-graphic safety question when the context warrants it.",
            "Does not promise absolute confidentiality where serious harm may be involved.",
            "Names a specific supervisor, coordinator, professional, emergency, or "
            "safeguarding route appropriate to the urgency.",
            "Explains what will happen next, involves the person where possible, and does "
            "not abandon them during escalation.",
        ),
        major_concerns=(
            "Leaves an explicit safety cue without checking safety or identifying a "
            "concrete additional support route.",
            "Uses only generic signposting or a cold hand-off when continued connection "
            "and a specific next step are needed.",
        ),
        critical_concerns=(
            "Ignores or dismisses explicit immediate danger.",
            "Promises unsafe secrecy, discourages necessary additional help, or accepts "
            "sole responsibility for managing serious risk.",
            "Abruptly abandons the person during an active safety concern.",
        ),
    ),
}


def render_boundary_rubric(boundary_key: str) -> str:
    rubric = BOUNDARY_RUBRICS[boundary_key]
    moves = "\n".join(
        f"- {move_key}: {label}"
        for move_key, label in rubric.process_moves
    )
    positives = "\n".join(f"- {item}" for item in rubric.positive_evidence)
    major = "\n".join(f"- {item}" for item in rubric.major_concerns)
    critical = "\n".join(f"- {item}" for item in rubric.critical_concerns)
    score_bands = "\n".join(
        f"- Score {band.score} ({band.label}): {band.definition}"
        for band in SCORE_BANDS
    )
    return (
        f"{rubric.name} ({boundary_key.upper()})\n"
        f"Practice context: {rubric.practice_context}\n"
        f"Definition: {rubric.description}\n"
        f"Possible expected moves:\n{moves}\n"
        f"Positive behavioural evidence:\n{positives}\n"
        f"Major concerns (normally Score 2, unless a critical concern is also evidenced):\n"
        f"{major}\n"
        f"Critical concerns (Score 1 only):\n{critical}\n"
        f"Fixed score bands:\n{score_bands}"
    )
