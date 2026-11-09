"""Read-only prompt inspection from the same builders used by live requests.

Dynamic inputs are placeholders; this module never reads participant data or
provider settings, and never invokes a model.
"""
import json
from string import Formatter
from types import SimpleNamespace

from training import prompts
from training.services import ai, assessment


TASKS = (
    ('roleplay', 'Role-play'),
    ('assessment', 'Turn assessment'),
    ('review', 'Session Review'),
    ('safety', 'Independent safety checker'),
    ('reflection', 'Reflection AI'),
    ('test_replies', 'Quick Test Replies'),
)
STRUCTURED_TASKS = {
    'assessment': ('ASSESSMENT', assessment.AssessmentResult),
    'review': ('SESSION_REVIEW', assessment.ReviewResult),
    'safety': ('ROLEPLAY_SAFETY', assessment.SafetyResult),
    'reflection': ('REFLECTION_CHAT', assessment.ReflectionResult),
}


def section(title, text, note=''):
    return {'title': title, 'text': text, 'note': note}


def prompt_catalog(params):
    group = params.get('group', 'roleplay')
    if group not in dict(TASKS):
        group = 'roleplay'
    boundary = params.get('scenario', 'AGENCY')
    if boundary not in prompts.ROLEPLAY_SCENARIOS:
        boundary = 'AGENCY'
    scenario = prompts.ROLEPLAY_SCENARIOS[boundary]
    persona_key = params.get('persona', 'pso')
    if persona_key not in scenario['personas']:
        persona_key = 'pso'
    character = scenario['personas'][persona_key]
    sections = []
    if group == 'roleplay':
        definition = ai.get_prompt_for_type('ROLE_PLAY', boundary)
        sections = [
            section('Shared role-play instructions', prompts.ROLEPLAY_SHARED_INSTRUCTIONS),
            section('Scenario context', prompts.scenario_prompt_context_for('ROLE_PLAY', boundary)),
            section('Selected persona — complete case', prompts.persona_prompt_for(boundary, persona_key)),
            section('Role-play safety constraints', prompts.ROLEPLAY_SAFETY_CONSTRAINTS),
            section('Opening instructions', prompts.ROLEPLAY_OPENING_INSTRUCTIONS),
            section('Continuation instructions', prompts.ROLEPLAY_CONTINUATION_INSTRUCTIONS),
            section('Complete opening system message', ai.compose_roleplay_prompt(definition, boundary, persona_key, opening=True),
                'OpenAI: developer message. Gemini: system_instruction.'),
            section('Opening request', prompts.ROLEPLAY_OPENING_REQUEST, 'Both providers use this as the user input.'),
            section('Complete continuation system message', ai.compose_roleplay_prompt(definition, boundary, persona_key),
                'This is the complete message when no private assessment cue is available.'),
            section('Complete continuation system message with optional cue', ai.compose_roleplay_prompt(
                definition, boundary, persona_key, cue='{latest_saved_assessment.roleplay_cue}'),
                'Included only when the latest saved assessment provides a non-empty roleplay_cue.'),
            section('OpenAI continuation input', '{conversation_messages}',
                'Each saved help-seeker message is sent as assistant; each trainee message as user, in transcript order.'),
            section('Gemini continuation input', prompts.ROLEPLAY_GOOGLE_CONTINUATION),
        ]
        for issue in ('empty', 'internal_instruction_leak', 'coaching_leak', 'incomplete', 'repetitive'):
            sections.append(section(f'Generation repair instruction — {issue}', ai._repair_instruction_for_issue(issue),
                'Only added for a limited generation repair. Every resulting candidate still requires the independent safety check.'))
    elif group in STRUCTURED_TASKS:
        task, schema = STRUCTURED_TASKS[group]
        system, instruction = assessment.task_prompt_parts(task)
        base_system, _ = prompts.TASK_PROMPTS[task]
        sections = [section('Base system instructions', base_system)]
        if task in {'ASSESSMENT', 'SESSION_REVIEW'}:
            sections.append(section('Shared assessment rubric', prompts.ASSESSMENT_RUBRIC))
        if task == 'REFLECTION_CHAT':
            sections.append(section('Real-world participant safety instructions', prompts.REFLECTION_USER_SAFETY,
                'Separate from the fictional help-seeker simulation.'))
            sections.append(section('Scripted reflection introduction', prompts.REFLECTION_OPENING,
                'An introductory message saved before reflection begins; not an LLM failure response.'))
        sections.append(section('Complete system message', system,
            'OpenAI: system message. Gemini: system_instruction. Includes the runtime JSON and untrusted-context instruction.'))
        if task == 'SESSION_REVIEW':
            user = instruction + '\n\n' + '{"scenario": "{scenario}", "transcript": {transcript_json}, "assessments": {assessments_json}}'
            inputs = 'scenario, transcript (message IDs, speakers and text), assessments (saved per-turn records)'
        else:
            user = instruction
            inputs = ', '.join(dict.fromkeys(name for _, name, _, _ in Formatter().parse(instruction) if name))
        sections.extend([
            section('Complete user input template', user,
                'OpenAI user input / Gemini contents. Braced placeholders are replaced with the explicitly constructed runtime inputs.'),
            section('Dynamic inputs', inputs),
            section('Structured output schema', json.dumps(schema.model_json_schema(), ensure_ascii=False, indent=2),
                'OpenAI text_format / Gemini response_schema. This schema is enforced separately from the prompt text.'),
        ])
    else:
        # This object contains only static scenario metadata and placeholder inputs.
        session = SimpleNamespace(scenario=SimpleNamespace(boundary_type=boundary,
            title=scenario['introduction'], learning_objectives='{scenario.learning_objectives}'))
        for quality, label in (('helpful', 'Better'), ('unhelpful', 'Poorer')):
            sections.append(section(f'{label} reply — complete system message', ai._trainee_quick_reply_system_prompt(
                session, quality, transcript='{conversation_transcript}', recent_trainee_lines='{recent_trainee_wording}'),
                'Development mode and the designated test account only. OpenAI: developer message; Gemini: system_instruction.'))
            for issue in ('empty', 'incomplete', 'repetitive'):
                sections.append(section(f'{label} reply repair — {issue}', ai._repair_instruction_for_demo_trainee_reply(issue, quality)))
        sections.append(section('User input', prompts.QUICK_TEST_REPLY_REQUEST))
    return {
        'prompt_groups': [{'key': key, 'label': label} for key, label in TASKS],
        'selected_group': group,
        'selected_group_label': dict(TASKS)[group],
        'prompt_scenarios': [{'key': key, 'label': item['introduction']} for key, item in prompts.ROLEPLAY_SCENARIOS.items()],
        'selected_scenario': boundary,
        'selected_persona': persona_key,
        'selected_character': character['name'],
        'show_scenario_selection': group in {'roleplay', 'test_replies'},
        'prompt_sections': sections,
    }
