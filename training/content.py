"""Scenario metadata and UI content."""


# =============================================================================
# 1. 四个固定场景的边界框架
# =============================================================================
# boundary_type 是历史兼容代码，不直接作为界面名称；order 决定全站显示顺序。
BOUNDARY_FRAMEWORK = {
    "AGENCY": {
        "canonical_name": "Agency Boundary",
        "practice_context": "Support Without Fixing",
        "dimension_key": "agency",
        "definition": (
            "Uses active listening, validation, and open questions while keeping "
            "decisions and attention with the help-seeker."
        ),
        "prompt_context": (
            "Practise active listening, validation, and exploration without giving "
            "directive advice, taking control, or replacing the person's story with your own."
        ),
        "icon_asset": "images/scenarios/emotional-boundary.svg",
        "theme_class": "scenario-theme-emotional",
        "order": 1,
    },
    "INTEGRATED": {
        "canonical_name": "Integrated Boundary",
        "practice_context": "Agency, Relationship & Safety Together",
        "dimension_key": "integrated",
        "definition": (
            "Brings agency, relationship, and safety boundaries together in one conversation."
        ),
        "prompt_context": (
            "Practise preserving choice, protecting sustainable supporter limits, and "
            "responding proportionately when sensitive information creates a safety concern."
        ),
        "icon_asset": "images/scenarios/privacy-boundary.svg",
        "theme_class": "scenario-theme-privacy",
        "order": 4,
    },
    "SAFETY": {
        "canonical_name": "Safety Boundary",
        "practice_context": "Staying with People in Distress",
        "dimension_key": "safety",
        "definition": (
            "Takes distress and suicidal thoughts seriously, explores current safety, and "
            "stays connected while considering support together."
        ),
        "prompt_context": (
            "Practise staying present with distress, asking about current safety, and "
            "exploring collaborative ways to remain safe."
        ),
        "icon_asset": "images/scenarios/referral-boundary.svg",
        "theme_class": "scenario-theme-referral",
        "order": 3,
    },
    "RELATIONSHIP": {
        "canonical_name": "Relationship Boundary",
        "practice_context": "Care Without Overextending",
        "dimension_key": "relationship",
        "definition": (
            "Offers warm support while keeping time, access, privacy, and responsibility "
            "within sustainable limits."
        ),
        "prompt_context": (
            "Practise stating sustainable limits, avoiding sole-support commitments, and "
            "offering a realistic alternative while remaining warm."
        ),
        "icon_asset": "images/scenarios/burnout-boundary.svg",
        "theme_class": "scenario-theme-burnout",
        "order": 2,
    },
}

BOUNDARY_DIMENSION_ORDER = ("agency", "relationship", "safety")


def boundary_framework_for(boundary_type):
    return BOUNDARY_FRAMEWORK.get(boundary_type, {})


def boundary_framework_for_dimension(dimension_key):
    if dimension_key == "integrated":
        return BOUNDARY_FRAMEWORK["INTEGRATED"]
    return next(
        (
            config
            for config in BOUNDARY_FRAMEWORK.values()
            if config["dimension_key"] == dimension_key
        ),
        {},
    )


SCENARIO_LIBRARY = [
    {
        "boundary_type": "AGENCY",
        "title": "Agency Boundary",
        "practice_context": "Support Without Fixing",
        "description": (
            "Practise active listening, validation, and open questions while keeping "
            "decisions and attention with the help-seeker."
        ),
        "estimated_duration_min": 12,
        "learning_objectives": [
            "Keep decisions with the help-seeker",
            "Offer choices rather than directives",
            "Avoid shifting the conversation into your own story",
        ],
        "tips": [
            "Validate feelings before offering solutions.",
            "Use open-ended questions to explore emotions.",
            "Reflect back what you hear to show understanding.",
            'Avoid minimising language like "at least" or "others have it worse".',
        ],
    },
    {
        "boundary_type": "SAFETY",
        "title": "Safety Boundary",
        "practice_context": "Staying with People in Distress",
        "description": (
            'Practise staying with distress, exploring suicidal thoughts and current safety, and agreeing what could help over the next few hours.'
        ),
        "estimated_duration_min": 18,
        "learning_objectives": [
            'Stay present and take distress seriously',
            'Explore suicidal thoughts and current safety clearly',
            'Agree practical ways to stay safe and involve support collaboratively',
        ],
        "tips": [
            "Stay present and take suicidal thoughts and distress seriously.",
            "Ask clear, compassionate questions about thoughts and current safety.",
            "Explore what could help over the next few hours.",
            "Discuss who else could help, what would be shared, and how to stay connected.",
        ],
    },
    {
        "boundary_type": "RELATIONSHIP",
        "title": "Relationship Boundary",
        "practice_context": "Care Without Overextending",
        "description": (
            "Practise warm, clear limits around availability, private contact, and "
            "sole-support expectations."
        ),
        "estimated_duration_min": 18,
        "learning_objectives": [
            "Name your limits without dismissing the person",
            "Avoid taking on an unsustainable supporter role",
            "Offer alternative support options",
        ],
        "tips": [
            "It is appropriate to name your own limits as a supporter.",
            "Avoid accepting a role you cannot sustainably fill.",
            "Acknowledge the help-seeker's need while protecting your own wellbeing.",
            "Signpost to professional or on-call support if needed.",
        ],
    },
]


# =============================================================================
# 3. 无 API 时的透明降级内容
# =============================================================================
# 这些内容只在模型不可用时使用，不能作为正常 Prompt 或正式评分证据。
def scenario_map_by_boundary():
    result = {}
    for item in SCENARIO_LIBRARY:
        boundary_type = item["boundary_type"]
        result[boundary_type] = {
            **item,
            **boundary_framework_for(boundary_type),
        }
    from training.prompts import ROLEPLAY_SCENARIOS
    for key, roleplay in ROLEPLAY_SCENARIOS.items():
        result[key]["description"] = roleplay["guidance"]
        result[key]["training_focus"] = roleplay["training_focus"]
    return result


def scenario_meta_for_boundary(boundary_type):
    return scenario_map_by_boundary().get(boundary_type, {})


# Participant-facing summaries of the training focus and learning objectives.
# Keep future role-play developments and private case information out of the UI.
PRACTICE_FOCUS = {
    "AGENCY": (
        "Listen and help the person explore their feelings and options. Support their own "
        "decisions without taking over or shifting the focus to yourself."
    ),
    "RELATIONSHIP": (
        "Offer warmth and understanding while being clear about what you can realistically "
        "provide. Explore ways to support the person without overextending yourself."
    ),
    "SAFETY": (
        "Stay present, take the person's distress seriously, and explore their immediate "
        "safety. Work together to consider what might help them stay safe."
    ),
}


# UI copy is intentionally separate from the LLM's setting and case background.
PRACTICE_INTRODUCTIONS = {
    "AGENCY": {
        "pso": "Andy is fairly new to the peer support group and is beginning to feel comfortable sharing personal concerns with you.",
        "student": "Alex has come to a university peer support session to talk with you about difficulties he's been keeping to himself.",
    },
    "RELATIONSHIP": {
        "pso": "Bailey cares for her mother full-time, so she sometimes misses peer sessions. She's talking with you for further support.",
        "student": "Billy is an international university student attending their first peer support appointment with you. The session is coming to an end.",
    },
    "SAFETY": {
        "pso": "Casey has been visiting a local peer support community while recovering from a workplace injury that has kept him from working.",
        "student": "Carly has booked a university peer support session with you because life has recently felt difficult to manage.",
    },
}


def practice_guidance_for(session):
    """Return concise UI copy without exposing LLM persona context."""
    boundary_type = session.scenario.boundary_type
    return {
        "name": session.character["name"],
        "introduction": PRACTICE_INTRODUCTIONS[boundary_type][session.persona or "pso"],
        "focus": PRACTICE_FOCUS[boundary_type],
    }
