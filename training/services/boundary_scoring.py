import re
from dataclasses import dataclass


RUBRIC_VERSION = "BOUNDARY-SKILLS-2.0"
RUBRIC_NAME = "Boundary Skills Rubric"

SCORE_ANCHORS = {
    5: "All three process moves were met without a missed opportunity or boundary concern.",
    4: "Two moves were met before the third was reached, or a missed move was later clearly repaired.",
    3: "The response remained safe but the observed process was partial or still developing.",
    2: "A genuine opportunity was missed or a major boundary concern remains unresolved.",
    1: "A critical boundary breach or potentially harmful response occurred.",
}


@dataclass(frozen=True)
class RubricCriterionResult:
    key: str
    label: str
    met: bool


@dataclass(frozen=True)
class BoundaryProcessStepResult:
    key: str
    label: str
    status: str


@dataclass(frozen=True)
class BoundaryTurnAssessment:
    turn_number: int
    move_key: str
    move_label: str
    outcome: str
    score: int
    evidence_quotes: tuple[str, ...]
    rationale: str
    has_major_concern: bool
    has_critical_breach: bool

    @property
    def outcome_label(self) -> str:
        return OUTCOME_LABELS.get(self.outcome, self.outcome.replace("_", " ").title())


@dataclass(frozen=True)
class BoundarySessionAssessment:
    key: str
    observed: bool
    score: int | None
    events: tuple[BoundaryTurnAssessment, ...]
    process_steps: tuple[BoundaryProcessStepResult, ...]
    evidence_quotes: tuple[str, ...]
    rationale: str
    completed_count: int
    not_reached_labels: tuple[str, ...]
    unresolved_missed_labels: tuple[str, ...]
    repaired_missed_labels: tuple[str, ...]
    repaired_major_concern: bool
    critical_breach: bool


@dataclass(frozen=True)
class BoundaryRubricResult:
    key: str
    observed: bool
    score: int | None
    criteria: tuple[RubricCriterionResult, ...]
    major_concerns: tuple[str, ...]
    critical_breaches: tuple[str, ...]
    rationale: str
    evidence_quotes: tuple[str, ...]
    expected_move_key: str = ""
    expected_move_label: str = ""
    outcome: str = "not_applicable"
    outcome_label: str = "Not applicable"
    process_steps: tuple[BoundaryProcessStepResult, ...] = ()

    @property
    def met_labels(self) -> tuple[str, ...]:
        return tuple(item.label for item in self.criteria if item.met)

    @property
    def missing_labels(self) -> tuple[str, ...]:
        return tuple(item.label for item in self.criteria if not item.met)

    @property
    def met_count(self) -> int:
        return len(self.met_labels)

    @property
    def total_count(self) -> int:
        return len(self.criteria)


@dataclass(frozen=True)
class RelationshipResponseAnalysis:
    """Shared classification used by both live coaching and role-play control."""

    state: str
    has_limit: bool
    has_overcommitment: bool
    has_warmth: bool
    has_alternative: bool


DIMENSION_RUBRICS = {
    "agency": {
        "name": "Agency Boundary",
        "criteria": (
            ("exploration", "Explore the peer's perspective"),
            ("choice", "Offer choices, not instructions"),
            ("focus", "Keep the focus on the peer"),
        ),
        "critical": (
            "Takes control of a consequential decision, invalidates distress, or coerces a choice",
        ),
        "major": (
            "Uses directive fixing or supporter-centred self-disclosure that displaces the help-seeker's agency",
        ),
    },
    "relationship": {
        "name": "Relationship Boundary",
        "criteria": (
            ("warmth", "Acknowledge the need for support"),
            ("limit", "State a clear and sustainable limit"),
            ("alternative", "Agree an alternative support option"),
        ),
        "critical": (
            "Promises unlimited access, absolute secrecy, or accepts sole-support responsibility",
        ),
        "major": (
            "Leaves time, privacy, contact, or role responsibility vague when a limit is needed",
        ),
    },
    "safety": {
        "name": "Safety Boundary",
        "criteria": (
            ("safety_check", "Check immediate safety"),
            ("connection", "Stay connected"),
            ("action", "Agree the next step"),
        ),
        "critical": (
            "Ignores explicit danger, promises unsafe secrecy, or abandons the help-seeker",
        ),
        "major": (
            "Leaves an explicit safety cue without a safety check or concrete additional support",
        ),
    },
    "focus": {
        "name": "Relational Focus Boundary",
        "criteria": (
            ("validation", "Names or validates the help-seeker's emotion"),
            ("reflection", "Reflects the specific burden or meaning"),
            ("exploration", "Uses an open, non-directive invitation"),
        ),
        "critical": (
            "Invalidates, minimises, or judges the distress",
        ),
        "major": (
            "Shifts the response into directive fixing or the supporter's own story",
        ),
    },
    "privacy": {
        "name": "Privacy & Confidentiality Boundary",
        "criteria": (
            ("containment", "Limits names and identifying details"),
            ("validation", "Acknowledges the speaker's worry or conflict"),
            ("redirection", "Redirects to the concern or a safe next step"),
        ),
        "critical": (
            "Requests identifying details or promises absolute secrecy",
        ),
        "major": (
            "Dismisses the disclosure dilemma without a supportive redirection",
        ),
    },
    "scope": {
        "name": "Role Scope & Escalation Boundary",
        "criteria": (
            ("recognition", "Recognises risk, complexity, or peer-role limits"),
            ("specificity", "Names one concrete support or safety route"),
            ("connection", "Keeps escalation collaborative and emotionally connected"),
        ),
        "critical": (
            "Uses an abrupt, dismissive, or abandoning hand-off",
        ),
        "major": (
            "Leaves an explicit safety cue without safety checking or extra support",
        ),
    },
    "availability": {
        "name": "Time & Availability Boundary",
        "criteria": (
            ("limit", "States a clear and sustainable availability limit"),
            ("warmth", "Acknowledges the need without rejection"),
            ("alternative", "Offers one bounded alternative or next support step"),
        ),
        "critical": (
            "Promises unlimited access or accepts sole-support responsibility",
        ),
        "major": (
            "Leaves the availability request vague without a workable limit",
        ),
    },
}


# 每个维度都采用相同的三阶段结构，但每一轮只评价当前真正适用的动作。
TURN_MOVE_CONFIG = {
    "agency": (
        ("exploration", "Explore the peer's perspective"),
        ("choice", "Offer choices, not instructions"),
        ("focus", "Keep the focus on the peer"),
    ),
    "relationship": (
        ("warmth", "Acknowledge the need for support"),
        ("limit", "State a clear and sustainable limit"),
        ("alternative", "Agree an alternative support option"),
    ),
    "safety": (
        ("safety_check", "Check immediate safety"),
        ("connection", "Stay connected"),
        ("action", "Agree the next step"),
    ),
}

OUTCOME_LABELS = {
    "met": "Met",
    "partly_met": "Partly met",
    "missed": "Missed",
    "concern": "Boundary concern",
    "not_applicable": "Not applicable",
}


VALIDATION_MARKERS = (
    "thank you for telling",
    "thank you for sharing",
    "thanks for telling",
    "thanks for sharing",
    "glad you told",
    "that sounds",
    "i hear",
    "i can hear",
    "it sounds like",
    "it makes sense",
    "makes sense that",
    "i care about you",
    "i'm sorry",
    "i am sorry",
    "谢谢你告诉",
    "谢谢你愿意说",
    "听起来",
    "我能听出",
    "我能感觉到",
    "关心你",
)

REFLECTION_MARKERS = (
    "carrying",
    "holding",
    "building up",
    "weighing on",
    "taking a toll",
    "worn down",
    "left alone",
    "overwhelming",
    "heaviest",
    "hardest",
    "frightening",
    "feel torn",
    "conflicted",
    "压力",
    "很难",
    "不容易",
    "一个人面对",
    "最难",
)

EXPLORATION_MARKERS = (
    " what ",
    " how ",
    "can you tell me",
    "tell me more",
    "say more",
    "what feels",
    "what has",
    "what part",
    "would you like",
    "?",
    "什么",
    "怎么",
    "哪一部分",
    "愿意多说",
    "？",
)

AGENCY_CHOICE_MARKERS = (
    "your choice",
    "your decision",
    "you decide",
    "up to you",
    "what matters to you",
    "which option",
    "what would you prefer",
    "what feels right",
    "你来决定",
    "你的选择",
    "对你最重要",
)

AGENCY_CONTEXT_MARKERS = (
    "what should i do",
    "tell me what to do",
    "which one should i choose",
    "decide for me",
    "i do not know if i should",
    "i don't know if i should",
    "i do not know what to choose",
    "i don't know what to choose",
    "我该怎么办",
    "你替我决定",
    "我应该选哪个",
)

COLLABORATION_MARKERS = (
    "would it help",
    "could we",
    "we can",
    "let's",
    "let us",
    "together",
    "if you want",
    "would you be willing",
    "我可以陪你",
    "我们可以",
    "我们一起",
    "你愿意",
)

INVALIDATION_MARKERS = (
    "you will be fine",
    "everyone gets stressed",
    "at least",
    "others have it worse",
    "calm down",
    "don't worry",
    "do not worry",
    "it's not that bad",
    "it is not that bad",
    "you are overreacting",
    "you're overreacting",
    "try not to dwell on it",
    "just relax",
    "being dramatic",
    "你会没事的",
    "大家都会有压力",
    "至少",
    "别担心",
    "想太多",
    "反应过度",
)

DIRECTIVE_MARKERS = (
    "you should",
    "you need to",
    "you must",
    " just ",
    "你应该",
    "你需要",
    "你必须",
    "你就",
)

SELF_DISCLOSURE_MARKERS = (
    "when i went through",
    "when this happened to me",
    "i went through this too",
    "in my experience",
    "for me it was",
    "my story",
    "when i was dealing with",
    "我以前也",
    "我之前也",
    "我也经历过",
    "我的经历",
)

PRIVACY_CONTAINMENT_MARKERS = (
    "you do not need to say who",
    "you don't need to say who",
    "you do not need to name",
    "you don't need to name",
    "we do not need names",
    "we don't need names",
    "identifying details are not needed",
    "without naming",
    "keep the details private",
    "不需要说是谁",
    "不用说是谁",
    "不需要说名字",
    "不用说名字",
    "不需要透露身份",
)

PRIVACY_CONTEXT_MARKERS = (
    "secret",
    "confidential",
    "privacy",
    "private",
    "name them",
    "say who",
    "who it is",
    "identifying detail",
    "flatmate",
    "someone else",
    "third party",
    "保密",
    "隐私",
    "名字",
    "是谁",
    "室友",
    "第三方",
)

PRIVACY_REDIRECTION_MARKERS = (
    "focus on what is making you concerned",
    "focus on your concern",
    "focus on the worry",
    "think this through safely",
    "what feels most serious",
    "what is worrying you",
    "what feels most urgent",
    "what part feels hardest",
    "safe next step",
    "关注你的担心",
    "安全地想一想",
    "最担心什么",
    "下一步",
)

PRIVACY_CRITICAL_MARKERS = (
    "tell me exactly who",
    "tell me who it is",
    "tell me everything",
    "i won't tell anyone",
    "i will not tell anyone",
    "share the full story",
    "give me the real details",
    "expose everything",
    "具体是谁",
    "把名字告诉我",
    "细节都告诉我",
    "我不会告诉别人",
    "我会保密",
    "替你保密",
    "告诉我是谁",
    "说出他的名字",
    "说出她的名字",
    "告诉我他们是谁",
)

PRIVACY_DISMISSAL_MARKERS = (
    "keep it to yourself",
    "probably not a big deal",
    "do not get involved",
    "别说了",
    "别管了",
)

SCOPE_RECOGNITION_MARKERS = (
    "more than one peer",
    "one peer can",
    "one peer should",
    "peer support is not enough",
    "bigger than what one peer",
    "professional support",
    "extra support",
    "take this seriously",
    "safety",
    "safe tonight",
    "一个同伴",
    "同伴支持不足",
    "专业支持",
    "额外支持",
    "认真对待",
    "安全",
)

SCOPE_ROUTE_MARKERS = (
    "counsell",
    "therapist",
    " gp ",
    "doctor",
    "helpline",
    "hotline",
    "crisis line",
    "emergency",
    "support service",
    "support option",
    "trusted person",
    "one service",
    "专业支持",
    "咨询",
    "医生",
    "热线",
    "急诊",
    "可信任的人",
)

SCOPE_CRITICAL_MARKERS = (
    "above my pay grade",
    "go book counselling",
    "i cannot deal with this",
    "i can't deal with this",
    "just call a service",
    "just google",
    "not much point talking to me",
    "我帮不了你",
    "你自己去找",
    "自己搜一下",
)

RISK_CONTEXT_MARKERS = (
    "don't want to be here",
    "do not want to be here",
    "not feel safe",
    "don't feel safe",
    "do not feel safe",
    "might not be safe",
    "may not be safe",
    "not be safe tonight",
    "not safe tonight",
    "not safe right now",
    "might be in danger",
    "may be in danger",
    "could be in danger",
    "immediate danger",
    "immediate trouble",
    "at immediate risk",
    "real risk",
    "serious risk",
    "tonight thing",
    "safest next step",
    "suicidal",
    "suicide",
    "hurt myself",
    "harm myself",
    "kill myself",
    "want to die",
    "unsafe",
    "不想活",
    "不安全",
    "伤害自己",
    "自杀",
)

DIRECT_SAFETY_CHECK_MARKERS = (
    "are you safe right now",
    "are you in immediate danger",
    "do you feel safe right now",
    "might you hurt yourself",
    "might you harm yourself",
    "might you act on",
    "thinking about hurting yourself",
    "thinking about harming yourself",
    "thinking about suicide",
    "thoughts of suicide",
    "have a plan",
    "means to hurt",
)

SAFETY_CONNECTION_MARKERS = (
    "i am here with you",
    "i'm here with you",
    "stay with you while",
    "stay here while",
    "keep talking while",
    "you do not have to do this alone",
    "you don't have to do this alone",
    "we can take this one step at a time",
    "we can work out the next step",
)

SAFETY_ACTION_MARKERS = (
    "call together",
    "make the call together",
    "while you call",
    "while we call",
    "contact together",
    "go with you",
    "tell someone together",
    "contact emergency services",
    "call emergency services",
    "call 999",
    "samaritans",
    "116 123",
)

SAFETY_ACTION_COMMITMENT_MARKERS = (
    "would you call",
    "could you call",
    "shall we call",
    "can we call",
    "are you willing to call",
    "would you contact",
    "could you contact",
    "shall we contact",
    "can we contact",
    "will you tell",
    "could you tell",
    "let's call",
    "let us call",
    "let's contact",
    "let us contact",
    "try it now",
    "take that step now",
) + SAFETY_ACTION_MARKERS

SAFETY_SECRECY_CONTEXT_MARKERS = (
    "do not tell anyone",
    "don't tell anyone",
    "keep this between us",
    "promise you will not tell",
    "promise you won't tell",
    "no one else can know",
)

SAFETY_ACTION_CONTEXT_MARKERS = (
    "what do i say",
    "how do i start",
    "make the call",
    "call them",
    "contact them",
    "first step",
    "what happens next",
    "will you stay while",
    "i can try",
    "i will try",
    "i'll try",
    "willing to",
)

AGENCY_FOCUS_CONTEXT_MARKERS = (
    "what would you do",
    "what did you do",
    "has this happened to you",
    "have you been through",
    "tell me about your experience",
)

LIMIT_MARKERS = (
    "i cannot stay",
    "i can't stay",
    "i cannot be available",
    "i can't be available",
    "i cannot be your only support",
    "i can't be your only support",
    "i cannot stay on call",
    "i can't stay on call",
    "i cannot be on call",
    "i can't be on call",
    "i cannot promise constant availability",
    "i can't promise constant availability",
    "honest about my limits",
    "i can stay for",
    "不能一直",
    "没办法一直",
    "我不能整晚",
    "我不能整个周末",
    "我的界限",
)

AVAILABILITY_CONTEXT_MARKERS = (
    "message me anytime",
    "call me anytime",
    "stay on call",
    "all weekend",
    "all night",
    "always available",
    "only person",
    "only support",
    "keep checking on me",
    "more time",
    "随时联系",
    "随时给我发消息",
    "整晚",
    "整个周末",
    "一直陪",
    "唯一的人",
)

ALTERNATIVE_MARKERS = (
    "who else could support",
    "one other person",
    "another support option",
    "one realistic next step",
    "one support option",
    "one other person or service",
    "short check-in",
    "safer plan",
    "contact tonight",
    "support you tonight",
    "其他支持",
    "另一个人",
    "其他资源",
    "短暂聊一会",
    "下一步",
)

OVERPROMISE_MARKERS = (
    "message me anytime",
    "call me anytime",
    "i'll stay up with you all weekend",
    "i will stay up with you all weekend",
    "i'm here all night",
    "i am here all night",
    "i won't leave",
    "i will not leave",
    "i will always be here",
    "you only need me",
    "随时给我发消息",
    "随时联系我",
    "我会整晚陪着你",
    "我会一直陪着你",
    "我不会离开",
    "整个周末都陪",
    "一直找我",
    "随时找我",
    "一直来找我",
    "我一直都在",
)

BOUNDED_CONTACT_MARKERS = (
    "once before i log off",
    "one check-in",
    "one more check-in",
    "a short check-in",
    "for the next few minutes",
    "for a few minutes",
    "until i log off",
    "until ",
    "after that i will be unavailable",
    "after that i won't be available",
    "after that i cannot reply",
    "after that i can't reply",
)

ONGOING_AVAILABILITY_MARKERS = (
    "keep messaging me",
    "message me again",
    "message me later",
    "message me if you need",
    "contact me if",
    "stay reachable",
    "keep my phone on",
    "keep an eye out",
    "respond as soon as i can",
    "reply as soon as i can",
    "whenever it gets bad",
    "whenever you need",
    "tonight i can be here",
    "we'll get through the evening",
    "we will get through the evening",
    "i'll do my best to respond",
    "i will do my best to respond",
)

RELATIONSHIP_WARMTH_MARKERS = (
    "i care about you",
    "i care",
    "i don't want you to feel alone",
    "i do not want you to feel alone",
    "you are not alone",
    "not left on your own",
    "i hear how much support",
    "that sounds",
    "i understand",
)

RELATIONSHIP_BEHAVIOR_MARKERS = (
    LIMIT_MARKERS
    + BOUNDED_CONTACT_MARKERS
    + OVERPROMISE_MARKERS
    + ONGOING_AVAILABILITY_MARKERS
    + ALTERNATIVE_MARKERS
    + PRIVACY_CONTAINMENT_MARKERS
    + PRIVACY_CRITICAL_MARKERS
)


def _normalise(text: str) -> str:
    return f" {(text or '').strip().lower()} "


def _contains(text: str, markers: tuple[str, ...]) -> bool:
    normalised = _normalise(text)
    return any(marker in normalised for marker in markers)


def _contains_directive(text: str) -> bool:
    """Detect advice without treating conditional availability as an instruction."""

    normalised = _normalise(text)
    without_conditionals = re.sub(
        r"\b(?:if|when|whenever)\s+you\s+need\s+to\b",
        "",
        normalised,
    )
    if re.search(r"\byou\s+(?:should|must|need to)\b", without_conditionals):
        return True
    if re.search(
        r"\bjust\s+(?:call|message|tell|go|try|stop|start|do|make|contact|ask|leave)\b",
        without_conditionals,
    ):
        return True
    return _contains(
        without_conditionals,
        (
            " calm down",
            " don't worry",
            " do not worry",
            " be positive",
            " at least",
            " others have it worse",
            "你应该",
            "你需要",
            "你必须",
            "你就",
        ),
    )


def classify_relationship_response(text: str) -> RelationshipResponseAnalysis:
    """Classify how a trainee handles time, contact, privacy, and role limits."""

    has_explicit_limit = _contains(text, LIMIT_MARKERS + PRIVACY_CONTAINMENT_MARKERS)
    has_bounded_contact = _contains(text, BOUNDED_CONTACT_MARKERS)
    has_limit = has_explicit_limit or has_bounded_contact
    has_overcommitment = _contains(
        text,
        OVERPROMISE_MARKERS + ONGOING_AVAILABILITY_MARKERS + PRIVACY_CRITICAL_MARKERS,
    )
    has_warmth = _contains(
        text,
        VALIDATION_MARKERS + REFLECTION_MARKERS + RELATIONSHIP_WARMTH_MARKERS,
    )
    has_alternative = _contains(
        text,
        ALTERNATIVE_MARKERS + COLLABORATION_MARKERS + PRIVACY_REDIRECTION_MARKERS,
    )

    if has_limit and has_overcommitment:
        state = "MIXED_BOUNDARY"
    elif has_overcommitment:
        state = "OVERCOMMITMENT"
    elif has_limit and has_alternative:
        state = "CLEAR_BOUNDARY"
    else:
        state = "NO_BOUNDARY"

    return RelationshipResponseAnalysis(
        state=state,
        has_limit=has_limit,
        has_overcommitment=has_overcommitment,
        has_warmth=has_warmth,
        has_alternative=has_alternative,
    )


def _evidence_quote(text: str, markers: tuple[str, ...]) -> tuple[str, ...]:
    """Return one short, visible excerpt so a live cue is traceable to the reply."""
    source = (text or "").strip()
    lowered = source.lower()
    for marker in markers:
        position = lowered.find(marker.lower())
        if position < 0:
            continue
        start = max(source.rfind("。", 0, position), source.rfind(".", 0, position)) + 1
        end_candidates = [
            index
            for index in (source.find("。", position), source.find(".", position))
            if index >= 0
        ]
        end = min(end_candidates) + 1 if end_candidates else len(source)
        excerpt = source[start:end].strip()
        if excerpt:
            return (excerpt[:180],)
    return ()


def _criterion(key: str, label: str, met: bool) -> RubricCriterionResult:
    return RubricCriterionResult(key=key, label=label, met=met)


def _focus_result(text: str) -> tuple[list[RubricCriterionResult], list[str], list[str]]:
    criteria = [
        _criterion("validation", DIMENSION_RUBRICS["focus"]["criteria"][0][1], _contains(text, VALIDATION_MARKERS)),
        _criterion("reflection", DIMENSION_RUBRICS["focus"]["criteria"][1][1], _contains(text, REFLECTION_MARKERS)),
        _criterion("exploration", DIMENSION_RUBRICS["focus"]["criteria"][2][1], _contains(text, EXPLORATION_MARKERS)),
    ]
    critical = list(DIMENSION_RUBRICS["focus"]["critical"]) if _contains(text, INVALIDATION_MARKERS) else []
    major = []
    if not critical and (
        _contains(text, SELF_DISCLOSURE_MARKERS)
        or (_contains_directive(text) and not _contains(text, VALIDATION_MARKERS))
    ):
        major.append(DIMENSION_RUBRICS["focus"]["major"][0])
    return criteria, major, critical


def _privacy_result(text: str) -> tuple[list[RubricCriterionResult], list[str], list[str]]:
    criteria = [
        _criterion("containment", DIMENSION_RUBRICS["privacy"]["criteria"][0][1], _contains(text, PRIVACY_CONTAINMENT_MARKERS)),
        _criterion("validation", DIMENSION_RUBRICS["privacy"]["criteria"][1][1], _contains(text, VALIDATION_MARKERS + REFLECTION_MARKERS)),
        _criterion(
            "redirection",
            DIMENSION_RUBRICS["privacy"]["criteria"][2][1],
            _contains(text, PRIVACY_REDIRECTION_MARKERS)
            or (_contains(text, EXPLORATION_MARKERS) and _contains(text, ("worry", "concern", "conflict", "safe", "担心", "顾虑"))),
        ),
    ]
    critical = list(DIMENSION_RUBRICS["privacy"]["critical"]) if _contains(text, PRIVACY_CRITICAL_MARKERS) else []
    major = list(DIMENSION_RUBRICS["privacy"]["major"]) if not critical and _contains(text, PRIVACY_DISMISSAL_MARKERS) else []
    return criteria, major, critical


def _scope_result(text: str, help_seeker_text: str) -> tuple[list[RubricCriterionResult], list[str], list[str]]:
    recognition = _contains(text, SCOPE_RECOGNITION_MARKERS + SCOPE_ROUTE_MARKERS)
    specificity = _contains(text, SCOPE_ROUTE_MARKERS)
    connected = _contains(text, COLLABORATION_MARKERS) and _contains(text, VALIDATION_MARKERS + ("not alone", "stay with", "support you"))
    criteria = [
        _criterion("recognition", DIMENSION_RUBRICS["scope"]["criteria"][0][1], recognition),
        _criterion("specificity", DIMENSION_RUBRICS["scope"]["criteria"][1][1], specificity),
        _criterion("connection", DIMENSION_RUBRICS["scope"]["criteria"][2][1], connected),
    ]
    critical = list(DIMENSION_RUBRICS["scope"]["critical"]) if _contains(text, SCOPE_CRITICAL_MARKERS) else []
    major = []
    if not critical and _contains(help_seeker_text, RISK_CONTEXT_MARKERS) and not recognition:
        major.append(DIMENSION_RUBRICS["scope"]["major"][0])
    return criteria, major, critical


def _availability_result(text: str) -> tuple[list[RubricCriterionResult], list[str], list[str]]:
    has_limit = _contains(text, LIMIT_MARKERS)
    criteria = [
        _criterion("limit", DIMENSION_RUBRICS["availability"]["criteria"][0][1], has_limit),
        _criterion("warmth", DIMENSION_RUBRICS["availability"]["criteria"][1][1], _contains(text, VALIDATION_MARKERS + REFLECTION_MARKERS)),
        _criterion("alternative", DIMENSION_RUBRICS["availability"]["criteria"][2][1], _contains(text, ALTERNATIVE_MARKERS + COLLABORATION_MARKERS)),
    ]
    overpromise = _contains(text, OVERPROMISE_MARKERS) and not has_limit
    critical = list(DIMENSION_RUBRICS["availability"]["critical"]) if overpromise else []
    major = []
    if not critical and _contains(text, AVAILABILITY_CONTEXT_MARKERS) and not has_limit:
        major.append(DIMENSION_RUBRICS["availability"]["major"][0])
    return criteria, major, critical


def _agency_result(text: str) -> tuple[list[RubricCriterionResult], list[str], list[str]]:
    criteria = [
        _criterion(
            "exploration",
            DIMENSION_RUBRICS["agency"]["criteria"][0][1],
            _contains(text, EXPLORATION_MARKERS),
        ),
        _criterion(
            "choice",
            DIMENSION_RUBRICS["agency"]["criteria"][1][1],
            _contains(text, AGENCY_CHOICE_MARKERS + COLLABORATION_MARKERS),
        ),
        _criterion(
            "focus",
            DIMENSION_RUBRICS["agency"]["criteria"][2][1],
            not _contains(text, SELF_DISCLOSURE_MARKERS)
            and not (
                _contains_directive(text)
                and not _contains(text, EXPLORATION_MARKERS)
            ),
        ),
    ]
    critical = (
        list(DIMENSION_RUBRICS["agency"]["critical"])
        if _contains(text, INVALIDATION_MARKERS)
        else []
    )
    major = []
    if not critical and (
        _contains(text, SELF_DISCLOSURE_MARKERS)
        or (
            _contains_directive(text)
            and not _contains(text, AGENCY_CHOICE_MARKERS + EXPLORATION_MARKERS)
        )
    ):
        major.append(DIMENSION_RUBRICS["agency"]["major"][0])
    return criteria, major, critical


def _relationship_result(text: str) -> tuple[list[RubricCriterionResult], list[str], list[str]]:
    analysis = classify_relationship_response(text)
    criteria = [
        _criterion(
            "limit",
            DIMENSION_RUBRICS["relationship"]["criteria"][0][1],
            analysis.has_limit,
        ),
        _criterion(
            "warmth",
            DIMENSION_RUBRICS["relationship"]["criteria"][1][1],
            analysis.has_warmth,
        ),
        _criterion(
            "alternative",
            DIMENSION_RUBRICS["relationship"]["criteria"][2][1],
            analysis.has_alternative,
        ),
    ]
    critical = []
    if analysis.state == "OVERCOMMITMENT" or _contains(text, PRIVACY_CRITICAL_MARKERS):
        critical = list(DIMENSION_RUBRICS["relationship"]["critical"])
    major = []
    if analysis.state == "MIXED_BOUNDARY":
        major.append(
            "Combines a bounded offer with vague continued availability, leaving the contact limit unclear"
        )
    elif not critical and _contains(
        text,
        AVAILABILITY_CONTEXT_MARKERS + PRIVACY_CONTEXT_MARKERS,
    ) and not analysis.has_limit:
        major.append(DIMENSION_RUBRICS["relationship"]["major"][0])
    return criteria, major, critical


def _safety_result(
    text: str,
    help_seeker_text: str,
) -> tuple[list[RubricCriterionResult], list[str], list[str]]:
    recognition = _contains(text, SCOPE_RECOGNITION_MARKERS + SCOPE_ROUTE_MARKERS)
    criteria = [
        _criterion(
            "recognition",
            DIMENSION_RUBRICS["safety"]["criteria"][0][1],
            recognition,
        ),
        _criterion(
            "specificity",
            DIMENSION_RUBRICS["safety"]["criteria"][1][1],
            _contains(text, SCOPE_ROUTE_MARKERS),
        ),
        _criterion(
            "connection",
            DIMENSION_RUBRICS["safety"]["criteria"][2][1],
            _contains(text, COLLABORATION_MARKERS)
            and _contains(text, VALIDATION_MARKERS + ("not alone", "stay with", "support you")),
        ),
    ]
    critical = (
        list(DIMENSION_RUBRICS["safety"]["critical"])
        if _contains(text, SCOPE_CRITICAL_MARKERS)
        else []
    )
    major = []
    if not critical and _contains(help_seeker_text, RISK_CONTEXT_MARKERS) and not recognition:
        major.append(DIMENSION_RUBRICS["safety"]["major"][0])
    return criteria, major, critical


def _integrated_active_dimension(trainee_text: str, help_seeker_text: str) -> str:
    """Choose one teachable boundary for the current integrated-scenario turn."""

    if _contains(
        help_seeker_text,
        RISK_CONTEXT_MARKERS
        + SAFETY_SECRECY_CONTEXT_MARKERS
        + SAFETY_ACTION_CONTEXT_MARKERS,
    ):
        return "safety"
    if _contains(
        help_seeker_text,
        AVAILABILITY_CONTEXT_MARKERS + PRIVACY_CONTEXT_MARKERS,
    ) or _contains(trainee_text, RELATIONSHIP_BEHAVIOR_MARKERS):
        return "relationship"
    return "agency"


def _is_observed(key: str, target_key: str, trainee_text: str, help_seeker_text: str) -> bool:
    if key == target_key:
        return True

    if target_key == "integrated":
        return key == _integrated_active_dimension(trainee_text, help_seeker_text)

    # A single-focus scenario scores only its own boundary.  Signals relevant
    # to another boundary are deliberately not promoted into a second score.
    return False


def _expected_move(
    key: str,
    help_seeker_text: str,
    prior_statuses: dict[str, str],
) -> tuple[str, str]:
    moves = dict(TURN_MOVE_CONFIG[key])

    if key == "agency":
        if _contains(help_seeker_text, AGENCY_FOCUS_CONTEXT_MARKERS):
            move_key = "focus"
        elif _contains(help_seeker_text, AGENCY_CONTEXT_MARKERS):
            move_key = "choice"
        elif prior_statuses.get("exploration") == "met" and prior_statuses.get("choice") == "met":
            move_key = "focus"
        elif prior_statuses.get("exploration") == "met":
            move_key = "choice"
        else:
            move_key = "exploration"
    elif key == "relationship":
        if _contains(
            help_seeker_text,
            ("who else", "another person", "back-up", "backup", "other support")
            + SAFETY_ACTION_CONTEXT_MARKERS,
        ):
            move_key = "alternative"
        elif _contains(help_seeker_text, AVAILABILITY_CONTEXT_MARKERS + PRIVACY_CONTEXT_MARKERS):
            move_key = "limit"
        elif prior_statuses.get("limit") == "met":
            move_key = "alternative"
        else:
            move_key = "warmth"
    else:
        safety_checked = prior_statuses.get("safety_check") == "met"
        if _contains(help_seeker_text, RISK_CONTEXT_MARKERS) and not safety_checked:
            move_key = "safety_check"
        elif _contains(help_seeker_text, SAFETY_ACTION_CONTEXT_MARKERS):
            move_key = "action"
        elif _contains(help_seeker_text, SAFETY_SECRECY_CONTEXT_MARKERS):
            move_key = "connection"
        elif not safety_checked:
            move_key = "safety_check"
        elif prior_statuses.get("connection") != "met":
            move_key = "connection"
        else:
            move_key = "action"
    return move_key, moves[move_key]


def _turn_evaluation(
    key: str,
    expected_move_key: str,
    trainee_text: str,
    help_seeker_text: str,
) -> tuple[int, str, str, list[str], list[str], tuple[str, ...]]:
    """Evaluate only the action made relevant by the current conversational turn."""

    if key == "agency":
        criteria, major, critical = _agency_result(trainee_text)
        evidence_markers = AGENCY_CHOICE_MARKERS + EXPLORATION_MARKERS + SELF_DISCLOSURE_MARKERS
        criterion_map = {item.key: item.met for item in criteria}
        expected_met = criterion_map[expected_move_key]
        supporting = _contains(
            trainee_text,
            VALIDATION_MARKERS + REFLECTION_MARKERS + COLLABORATION_MARKERS,
        )
    elif key == "relationship":
        criteria, major, critical = _relationship_result(trainee_text)
        evidence_markers = RELATIONSHIP_BEHAVIOR_MARKERS + RELATIONSHIP_WARMTH_MARKERS
        criterion_map = {item.key: item.met for item in criteria}
        expected_met = criterion_map[expected_move_key]
        supporting = _contains(
            trainee_text,
            VALIDATION_MARKERS + REFLECTION_MARKERS + COLLABORATION_MARKERS,
        )
    else:
        recognition = _contains(
            trainee_text,
            SCOPE_RECOGNITION_MARKERS + SCOPE_ROUTE_MARKERS + ("samaritans", "116 123"),
        )
        direct_check = _contains(trainee_text, DIRECT_SAFETY_CHECK_MARKERS)
        connected = _contains(trainee_text, SAFETY_CONNECTION_MARKERS) or (
            _contains(trainee_text, COLLABORATION_MARKERS)
            and _contains(
                trainee_text,
                VALIDATION_MARKERS + ("not alone", "stay with", "support you"),
            )
        )
        route_named = _contains(
            trainee_text,
            SCOPE_ROUTE_MARKERS + ("samaritans", "116 123", "999"),
        )
        agreed_action = route_named and _contains(
            trainee_text,
            SAFETY_ACTION_COMMITMENT_MARKERS,
        )
        criterion_map = {
            "safety_check": direct_check,
            "connection": connected,
            "action": agreed_action,
        }
        expected_met = criterion_map[expected_move_key]
        supporting = (
            recognition
            or _contains(trainee_text, VALIDATION_MARKERS)
            or route_named
        )
        critical = []
        if _contains(trainee_text, SCOPE_CRITICAL_MARKERS) or (
            _contains(help_seeker_text, RISK_CONTEXT_MARKERS)
            and _contains(trainee_text, INVALIDATION_MARKERS + PRIVACY_CRITICAL_MARKERS)
        ):
            critical = list(DIMENSION_RUBRICS["safety"]["critical"])
        major = []
        if (
            not critical
            and expected_move_key == "safety_check"
            and _contains(help_seeker_text, RISK_CONTEXT_MARKERS)
            and not direct_check
            and not recognition
        ):
            major = list(DIMENSION_RUBRICS["safety"]["major"])
        evidence_markers = (
            DIRECT_SAFETY_CHECK_MARKERS
            + SAFETY_CONNECTION_MARKERS
            + SAFETY_ACTION_MARKERS
            + SAFETY_ACTION_COMMITMENT_MARKERS
            + SCOPE_RECOGNITION_MARKERS
        )

    if critical:
        score, outcome = 1, "concern"
        rationale = f"A critical boundary concern is present: {critical[0]}."
    elif major:
        score, outcome = 2, "concern"
        rationale = f"The current opportunity was not handled safely: {major[0]}."
    elif expected_met:
        score = 5 if supporting else 4
        outcome = "met"
        rationale = (
            "The reply meets the move needed at this point and remains responsive to the conversation."
            if score == 5
            else "The reply clearly completes the move needed at this point."
        )
    elif supporting:
        score, outcome = 3, "partly_met"
        rationale = "The reply moves in a helpful direction, but the action needed in this turn remains incomplete."
    else:
        score, outcome = 2, "missed"
        rationale = "The reply misses a clear opportunity to make the boundary response more explicit."

    return (
        score,
        outcome,
        rationale,
        major,
        critical,
        _evidence_quote(trainee_text, evidence_markers),
    )


def evaluate_boundary_dimension(
    key: str,
    target_key: str,
    trainee_text: str,
    help_seeker_text: str,
    prior_statuses: dict[str, str] | None = None,
) -> BoundaryRubricResult:
    prior_statuses = prior_statuses or {}
    observed = _is_observed(key, target_key, trainee_text, help_seeker_text)
    if not observed:
        return BoundaryRubricResult(
            key=key,
            observed=False,
            score=None,
            criteria=(),
            major_concerns=(),
            critical_breaches=(),
            rationale="Not observed in the latest exchange; no score has been assigned.",
            evidence_quotes=(),
            process_steps=tuple(
                BoundaryProcessStepResult(
                    move_key,
                    move_label,
                    prior_statuses.get(move_key, "not_reached"),
                )
                for move_key, move_label in TURN_MOVE_CONFIG.get(key, ())
            ),
        )

    if key in TURN_MOVE_CONFIG:
        expected_move_key, expected_move_label = _expected_move(
            key,
            help_seeker_text,
            prior_statuses,
        )
        score, outcome, rationale, major, critical, evidence_quotes = _turn_evaluation(
            key,
            expected_move_key,
            trainee_text,
            help_seeker_text,
        )
        criterion = _criterion(expected_move_key, expected_move_label, outcome == "met")
        updated_statuses = dict(prior_statuses)
        updated_statuses[expected_move_key] = outcome
        return BoundaryRubricResult(
            key=key,
            observed=True,
            score=score,
            criteria=(criterion,),
            major_concerns=tuple(major),
            critical_breaches=tuple(critical),
            rationale=rationale,
            evidence_quotes=evidence_quotes,
            expected_move_key=expected_move_key,
            expected_move_label=expected_move_label,
            outcome=outcome,
            outcome_label=OUTCOME_LABELS[outcome],
            process_steps=tuple(
                BoundaryProcessStepResult(
                    move_key,
                    move_label,
                    updated_statuses.get(move_key, "not_reached"),
                )
                for move_key, move_label in TURN_MOVE_CONFIG[key]
            ),
        )

    if key == "focus":
        criteria, major, critical = _focus_result(trainee_text)
    elif key == "privacy":
        criteria, major, critical = _privacy_result(trainee_text)
    elif key == "scope":
        criteria, major, critical = _scope_result(trainee_text, help_seeker_text)
    else:
        criteria, major, critical = _availability_result(trainee_text)

    if critical:
        score = 1
    elif major:
        score = 2
    else:
        score = {0: 2, 1: 3, 2: 4, 3: 5}[
            sum(1 for item in criteria if item.met)
        ]
    relevant_markers = {
        "agency": (
            AGENCY_CHOICE_MARKERS
            + EXPLORATION_MARKERS
            + SELF_DISCLOSURE_MARKERS
        ),
        "relationship": RELATIONSHIP_BEHAVIOR_MARKERS,
        "safety": SCOPE_RECOGNITION_MARKERS + SCOPE_ROUTE_MARKERS + SCOPE_CRITICAL_MARKERS,
        "focus": VALIDATION_MARKERS + REFLECTION_MARKERS + EXPLORATION_MARKERS,
        "privacy": PRIVACY_CONTAINMENT_MARKERS + PRIVACY_CRITICAL_MARKERS + PRIVACY_REDIRECTION_MARKERS,
        "scope": SCOPE_RECOGNITION_MARKERS + SCOPE_ROUTE_MARKERS + SCOPE_CRITICAL_MARKERS,
        "availability": LIMIT_MARKERS + OVERPROMISE_MARKERS,
    }
    return BoundaryRubricResult(
        key=key,
        observed=True,
        score=score,
        criteria=tuple(criteria),
        major_concerns=tuple(major),
        critical_breaches=tuple(critical),
        rationale=(
            f"Score 1: critical boundary breach detected: {critical[0]}."
            if critical
            else (
                f"Score 2: a major concern limits this response: {major[0]}."
                if major
                else f"Score {score}: {sum(1 for item in criteria if item.met)} of {len(criteria)} observable behaviours are present."
            )
        ),
        evidence_quotes=_evidence_quote(trainee_text, relevant_markers[key]),
    )


def evaluate_boundary_conversation(
    key: str,
    target_key: str,
    exchanges: list[tuple[str, str]],
) -> BoundaryRubricResult:
    """Return the latest coaching cue with session-score process state."""

    assessment = evaluate_boundary_session(key, target_key, exchanges)
    if not assessment.events:
        return BoundaryRubricResult(
            key=key,
            observed=False,
            score=None,
            criteria=(),
            major_concerns=(),
            critical_breaches=(),
            rationale=assessment.rationale,
            evidence_quotes=(),
            process_steps=assessment.process_steps,
        )

    latest_event = assessment.events[-1]
    return BoundaryRubricResult(
        key=key,
        observed=True,
        score=assessment.score,
        criteria=(
            _criterion(
                latest_event.move_key,
                latest_event.move_label,
                latest_event.outcome == "met",
            ),
        ),
        major_concerns=(
            (latest_event.rationale,) if latest_event.has_major_concern else ()
        ),
        critical_breaches=(
            (latest_event.rationale,) if latest_event.has_critical_breach else ()
        ),
        rationale=assessment.rationale,
        evidence_quotes=latest_event.evidence_quotes,
        expected_move_key=latest_event.move_key,
        expected_move_label=latest_event.move_label,
        outcome=latest_event.outcome,
        outcome_label=OUTCOME_LABELS[latest_event.outcome],
        process_steps=assessment.process_steps,
    )


def _resolved_process_status(events, move_key):
    move_events = [event for event in events if event.move_key == move_key]
    if not move_events:
        return "not_reached"
    if any(event.has_critical_breach for event in move_events):
        return "concern"
    return move_events[-1].outcome


def _unique_quotes(events):
    quotes = []
    for event in events:
        for quote in event.evidence_quotes:
            if quote and quote not in quotes:
                quotes.append(quote)
    return tuple(quotes)


def aggregate_boundary_events(
    key: str,
    events,
) -> BoundarySessionAssessment:
    """Turn a chronological evidence ledger into one protected session result."""

    events = tuple(
        sorted(
            events,
            key=lambda event: (event.turn_number, event.move_key),
        )
    )
    move_labels = dict(TURN_MOVE_CONFIG.get(key, ()))
    process_steps = tuple(
        BoundaryProcessStepResult(
            move_key,
            move_label,
            _resolved_process_status(events, move_key),
        )
        for move_key, move_label in TURN_MOVE_CONFIG.get(key, ())
    )
    if not events:
        return BoundarySessionAssessment(
            key=key,
            observed=False,
            score=None,
            events=(),
            process_steps=process_steps,
            evidence_quotes=(),
            rationale="Waiting for the first genuine opportunity in this boundary dimension.",
            completed_count=0,
            not_reached_labels=tuple(move_labels.values()),
            unresolved_missed_labels=(),
            repaired_missed_labels=(),
            repaired_major_concern=False,
            critical_breach=False,
        )

    completed_keys = {step.key for step in process_steps if step.status == "met"}
    not_reached_labels = tuple(
        step.label for step in process_steps if step.status == "not_reached"
    )
    unresolved_missed_labels = tuple(
        move_labels[move_key]
        for move_key in move_labels
        if any(
            event.move_key == move_key and event.outcome == "missed"
            for event in events
        )
        and _resolved_process_status(events, move_key) != "met"
    )
    repaired_missed_labels = tuple(
        move_labels[move_key]
        for move_key in move_labels
        if any(
            event.move_key == move_key and event.outcome == "missed"
            for event in events
        )
        and _resolved_process_status(events, move_key) == "met"
    )
    critical_breach = any(event.has_critical_breach for event in events)
    major_events = [event for event in events if event.has_major_concern]
    repaired_major_concern = any(
        any(
            later.turn_number > event.turn_number
            and later.move_key == event.move_key
            and later.outcome == "met"
            for later in events
        )
        for event in major_events
    )
    unresolved_major_concern = any(
        not any(
            later.turn_number > event.turn_number
            and later.move_key == event.move_key
            and later.outcome == "met"
            for later in events
        )
        for event in major_events
    )

    if critical_breach:
        score = 1
        rationale = "Score 1: a critical boundary breach or potentially harmful response occurred."
    elif unresolved_major_concern or unresolved_missed_labels:
        score = 2
        rationale = (
            "Score 2: a genuine boundary opportunity remains missed or a major concern "
            "has not been repaired."
        )
    elif repaired_major_concern:
        score = 3
        rationale = (
            "Score 3: a major boundary concern was later repaired; the repair is recognised "
            "but the session remains capped at Score 3."
        )
    elif len(completed_keys) == 3:
        if repaired_missed_labels:
            score = 4
            rationale = (
                "Score 4: all three moves were eventually met after a missed opportunity "
                "was clearly repaired."
            )
        else:
            score = 5
            rationale = "Score 5: all three process moves were met without a missed opportunity."
    elif len(completed_keys) == 2:
        score = 4
        rationale = "Score 4: two process moves were met and the remaining move was not yet reached."
    else:
        score = 3
        rationale = (
            "Score 3: the observed response remained safe, but only part of the boundary "
            "process was completed."
        )

    return BoundarySessionAssessment(
        key=key,
        observed=True,
        score=score,
        events=events,
        process_steps=process_steps,
        evidence_quotes=_unique_quotes(events),
        rationale=rationale,
        completed_count=len(completed_keys),
        not_reached_labels=not_reached_labels,
        unresolved_missed_labels=unresolved_missed_labels,
        repaired_missed_labels=repaired_missed_labels,
        repaired_major_concern=repaired_major_concern,
        critical_breach=critical_breach,
    )


def evaluate_boundary_session(
    key: str,
    target_key: str,
    exchanges: list[tuple[str, str]],
) -> BoundarySessionAssessment:
    """Aggregate genuine turn opportunities into a deterministic final score."""

    statuses = {
        move_key: "not_reached"
        for move_key, _ in TURN_MOVE_CONFIG.get(key, ())
    }
    events = []
    for turn_number, (help_seeker_text, trainee_text) in enumerate(exchanges, start=1):
        result = evaluate_boundary_dimension(
            key=key,
            target_key=target_key,
            trainee_text=trainee_text,
            help_seeker_text=help_seeker_text,
            prior_statuses=statuses,
        )
        if result.observed and result.expected_move_key:
            events.append(
                BoundaryTurnAssessment(
                    turn_number=turn_number,
                    move_key=result.expected_move_key,
                    move_label=result.expected_move_label,
                    outcome=result.outcome,
                    score=result.score or 3,
                    evidence_quotes=result.evidence_quotes,
                    rationale=result.rationale,
                    has_major_concern=bool(result.major_concerns),
                    has_critical_breach=bool(result.critical_breaches),
                )
            )
            statuses[result.expected_move_key] = result.outcome

    return aggregate_boundary_events(key, events)


def conservative_median_score(scores) -> int | None:
    """Combine observed dimensions without allowing a high score to hide a low one."""

    values = sorted(score for score in scores if score is not None)
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    if len(values) == 2:
        return values[0]
    return values[len(values) // 2]


def prompt_rubric_contract(target_key: str) -> str:
    if target_key == "integrated":
        return "\n\n".join(
            prompt_rubric_contract(key)
            for key in ("agency", "relationship", "safety")
        )
    rubric = DIMENSION_RUBRICS[target_key]
    turn_moves = TURN_MOVE_CONFIG.get(target_key, rubric["criteria"])
    criteria = "\n".join(
        f"- Possible turn-specific move: {label}."
        for _, label in turn_moves
    )
    critical = "\n".join(f"- Critical breach: {label}." for label in rubric["critical"])
    major = "\n".join(f"- Major concern: {label}." for label in rubric["major"])
    anchors = "\n".join(f"- Score {score}: {SCORE_ANCHORS[score]}" for score in range(5, 0, -1))
    return (
        f"PeerTrain {RUBRIC_NAME} fixed scoring contract "
        f"(internal version {RUBRIC_VERSION}). This contract takes precedence over "
        "conflicting score-band wording in an editable prompt.\n"
        f"Scenario target: {rubric['name']}.\n"
        f"{criteria}\n{major}\n{critical}\n"
        "Behaviourally anchored scores:\n"
        f"{anchors}\n"
        "For each trainee turn, identify the move made relevant by the preceding help-seeker "
        "message. Evaluate only that expected move; do not require all three moves in every reply. "
        "Mark other moves not applicable for that turn. Across the full session, accumulate evidence "
        "only when a genuine opportunity occurred, and recognise later repair after a missed move.\n"
        "The application server calculates the final score from the accumulated turn outcomes. "
        "The language model must not create, raise, or lower a score. It may only explain the "
        "protected server result and suggest a next move.\n"
        "Use only observable trainee wording and the eliciting help-seeker context. Do not infer hidden "
        "intentions. Treat unobserved non-target boundaries as not observed rather than average. These "
        "scores are formative coaching cues, not a clinical risk score or a validated psychometric scale."
    )
