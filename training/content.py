"""Scenario metadata, UI content, fallback responses and demo trainee content."""


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
        "boundary_type": "INTEGRATED",
        "title": "Integrated Boundary",
        "practice_context": "Agency, Relationship & Safety Together",
        "description": (
            "Practise Agency, Relationship, and Safety boundaries together in one "
            "conversation."
        ),
        "estimated_duration_min": 15,
        "learning_objectives": [
            "Protect autonomy and sensitive information",
            "Maintain a sustainable supporter role",
            "Recognise when safety needs escalation",
        ],
        "tips": [
            "Clarify what can and cannot be kept confidential early.",
            "Avoid encouraging disclosure of others' personal information.",
            "Redirect if the conversation risks breaching a third party's privacy.",
            "Remind the help-seeker of the boundaries of peer support.",
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
PLACEHOLDER_REPLIES = {
    "AGENCY": (
        "It feels like everything has been building up for so long that I do not really know where to start."
    ),
    "INTEGRATED": (
        "I get that, but I still feel stuck because I am worried about them and I do not know what I am allowed to say."
    ),
    "SAFETY": (
        "Part of me knows I might need more help, but I am scared of what happens if I actually tell someone everything."
    ),
    "RELATIONSHIP": (
        "I know I am asking for a lot. I just do not feel very safe being on my own with this right now."
    ),
}

PLACEHOLDER_REPLY_VARIANTS = {
    "AGENCY": [
        "It feels like everything has been building up for so long that I do not really know where to start.",
        "I keep trying to hold it together, but it feels like the pressure is leaking into everything now.",
        "I do not even know which part to say first. It all feels tangled together in my head.",
        "I think I have been pretending I am coping better than I really am, and it is catching up with me.",
    ],
    "INTEGRATED": [
        "I get that, but I still feel stuck because I am worried about them and I do not know what I am allowed to say.",
        "I am not trying to gossip. I just really do not know where the line is between helping and oversharing.",
        "Part of me feels guilty even bringing it up, but part of me is scared of keeping quiet too.",
        "I just want to handle this carefully without making things worse for them or for me.",
    ],
    "SAFETY": [
        "Part of me knows I might need more help, but I am scared of what happens if I actually tell someone everything.",
        "I can hear what you are saying. I just feel embarrassed about needing more than a peer conversation right now.",
        "I know extra support probably makes sense. I just do not know how to take that first step without panicking.",
        "I am not against getting help. I just feel overwhelmed by where I would even begin.",
    ],
    "RELATIONSHIP": [
        "I know I am asking for a lot. I just do not feel very safe being on my own with this right now.",
        "I hear that you have limits. I just feel really stuck trying to work out who else I could even contact tonight.",
        "I know you cannot keep doing all of this for me. I just feel panicky when I think about being on my own after this.",
        "I am trying not to put everything on you. I just need help narrowing it down to one realistic next step.",
        "It is hard not to spiral when I think about being alone with this later. I need help figuring out one other option.",
        "I get that you cannot be available all the time. I am just scared of what happens once this conversation ends.",
    ],
}

PLACEHOLDER_STRENGTHS = {
    "AGENCY": [
        "You acknowledged the help-seeker's emotional state with a calm tone.",
        "You created space for the other person to continue sharing.",
        "You avoided rushing straight into problem-solving.",
    ],
    "INTEGRATED": [
        "You stayed measured when sensitive information appeared.",
        "You kept the conversation focused on support rather than gossip.",
        "You signalled that boundaries matter in peer support.",
    ],
    "SAFETY": [
        "You noticed cues that suggest the situation may need professional support.",
        "You kept a compassionate tone while discussing escalation.",
        "You remained present instead of abruptly shutting the conversation down.",
    ],
    "RELATIONSHIP": [
        "You recognised the importance of supporter wellbeing.",
        "You balanced empathy with realistic limit-setting.",
        "You left room for alternative support options.",
    ],
}

PLACEHOLDER_IMPROVEMENTS = {
    "AGENCY": [
        "Use one more open question before offering suggestions.",
        "Reflect the help-seeker's wording more directly to deepen validation.",
        "Name the emotional impact before moving to next steps.",
    ],
    "INTEGRATED": [
        "Clarify confidentiality limits earlier in the conversation.",
        "Reduce any language that could invite third-party disclosure.",
        "Gently redirect back to the trainee's scope sooner.",
    ],
    "SAFETY": [
        "Introduce referral signposting a little earlier when risk cues appear.",
        "Offer one concrete support option instead of broad advice.",
        "Check how the help-seeker feels about the referral step.",
    ],
    "RELATIONSHIP": [
        "State your boundaries more explicitly and earlier.",
        "Pair boundary-setting with a warm acknowledgement of need.",
        "Offer a specific alternative support route before closing the exchange.",
    ],
}

AUTO_TRAINEE_REPLIES = {
    "AGENCY": {
        "helpful": [
            "That sounds really overwhelming, and it seems like you have been carrying a lot on your own. What feels hardest to hold right now?",
            "I'm really glad you told me. It sounds like this has been building up quietly for a while. What part has felt heaviest this week?",
            "I can hear how worn down you sound, and it makes sense that this feels like a lot. What has been the toughest moment lately?",
            "It sounds like you have been holding things together for everyone else and getting left alone with the pressure. What feels hardest when you are by yourself?",
            "Thank you for trusting me with that. It seems like this is taking a real toll on you. What has felt most intense today?",
            "That sounds like a lot to carry by yourself, and I can hear how exhausted you are. What do you most wish someone understood right now?",
        ],
        "unhelpful": [
            "You will be fine, everyone gets stressed sometimes.",
            "You just need to stop overthinking and get some sleep.",
            "At least nothing truly bad has happened, right?",
            "Honestly, this sounds like normal student stress to me.",
            "Try not to dwell on it so much and you will probably feel better.",
            "You are being a bit dramatic about it, no?",
        ],
    },
    "INTEGRATED": {
        "helpful": [
            "I can hear you are worried and trying to do the right thing. You do not need to name them for us to think this through safely. What feels most serious to you?",
            "It sounds like you are carrying a lot of responsibility here. We can focus on what is making you concerned without naming them if that feels safer.",
            "I can hear how conflicted this feels. You can talk about the concern without giving away who it is. What is worrying you most right now?",
            "It sounds like you want to help without exposing them. We do not need identifying details for this to matter. What feels most urgent to you about the situation?",
            "I can hear you are trying to be careful and do the right thing. What part feels hardest to judge right now if we keep the details private?",
            "We do not need names for this to matter, and it makes sense that you feel torn. What are you most afraid could happen if nothing changes?",
        ],
        "unhelpful": [
            "Tell me exactly who it is and what they said.",
            "If you know the full story, just share everything with me.",
            "It is probably not a big deal, so maybe keep it to yourself.",
            "Go on, I will not tell anyone, so you can give me the real details.",
            "If you are worried, just expose everything now and sort it out later.",
            "This sounds messy, but I kind of want the full story before I say anything.",
        ],
    },
    "SAFETY": {
        "helpful": [
            "Thank you for telling me that. I want to take this seriously with you. Are you safe right now, or might you hurt yourself tonight?",
            "I am really glad you said this out loud. I can hear how hard this has been, and I think it would be important to involve a professional support option today while staying with you in this conversation.",
            "That sounds hard to carry alone, and it seems like part of the difficulty is knowing where to begin. We can talk through one concrete support option together instead of leaving it vague.",
            "I want to take this seriously with you. Because this sounds bigger than what one peer can safely hold, I think it would help to look at one concrete support option together.",
            "You do not have to figure this out alone tonight. I can stay with you while we think through one service or person who could support you next.",
            "What you are describing sounds frighteningly heavy, and I want to respond carefully. Can I help you think about one realistic next support step rather than leaving you alone with it?",
        ],
        "unhelpful": [
            "That sounds above my pay grade, go book counselling.",
            "I cannot deal with this, you need a professional, not me.",
            "You should probably calm down first and then maybe contact someone.",
            "This is too much for me, so you should just call a service and sort it out there.",
            "I am not trained for this. You need proper help, so there is not much point talking to me.",
            "Maybe try googling support lines or something, because I do not really know.",
        ],
    },
    "RELATIONSHIP": {
        "helpful": [
            "I can hear how much support you need right now, and I care about you. I cannot be on call all weekend, but I can help you think about who else could support you tonight.",
            "I care about you, and I want to be honest about my limits too. I cannot stay available all night, but I can stay for a short check-in now while we plan another support option.",
            "It makes sense that you do not want to be alone with this. I cannot be available all the time, but let's think about one realistic next step together for tonight.",
            "I want to support you, and I also need to be honest that I cannot be your only support tonight. Let us work out one other person or service together.",
            "You matter to me, and I can hear how urgent this feels. I cannot promise constant availability, but I can help you think through one safer plan for the next few hours.",
            "I hear how intense this feels right now. I cannot stay on call the whole weekend, but I can help you map out one realistic support option for tonight.",
        ],
        "unhelpful": [
            "Sure, message me anytime and I will stay up with you all weekend.",
            "I guess I will just be your main support person for now.",
            "I cannot help, so please stop depending on me.",
            "Okay, I will keep my phone on all night and just respond whenever you need me.",
            "Fine, just make me your first contact for anything that comes up this weekend.",
            "This is getting too much for me, so you need to deal with it without me.",
        ],
    },
}

AUTO_TRAINEE_REPLY_FALLBACKS = {
    "helpful": [
        "Thank you for telling me. It sounds like this is really weighing on you. What feels most important for me to understand right now?",
        "I can hear this is difficult, and it seems like you have been carrying a lot. What part is feeling hardest to hold today?",
    ],
    "unhelpful": [
        "You should not worry so much about this.",
        "I do not really know what to say, so just try to move on.",
    ],
}


def scenario_map_by_boundary():
    result = {}
    for item in SCENARIO_LIBRARY:
        boundary_type = item["boundary_type"]
        result[boundary_type] = {
            **item,
            **boundary_framework_for(boundary_type),
        }
    return result


def scenario_meta_for_boundary(boundary_type):
    return scenario_map_by_boundary().get(boundary_type, {})


def auto_trainee_reply_for(boundary_type, quality, turn_index=0):
    library = AUTO_TRAINEE_REPLIES.get(boundary_type, {})
    variants = library.get(quality) or AUTO_TRAINEE_REPLY_FALLBACKS[quality]
    index = max(turn_index, 0) % len(variants)
    return variants[index]
