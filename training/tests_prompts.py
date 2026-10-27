"""Code prompt composition, provider history and read-only Admin regressions."""

import re
from itertools import product
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.conf import settings
from django.db import connection
from django.test import override_settings
from django.urls import reverse
from django.utils.html import escape

from training import prompts
from training.models import Message, TrainingSession
from training.services.ai import (
    _roleplay_opening_system_prompt,
    _roleplay_quality_issue,
    _roleplay_system_prompt,
    generate_ai_opening,
    generate_ai_reply,
    get_prompt_for_type,
)
from training.tests import TrainingFlowBase


class CodePromptTests(TrainingFlowBase):
    def test_roleplay_content_matches_source_and_excludes_reserved_cases(self):
        reference = (Path(settings.BASE_DIR) / "Role-play_Prompts.md").read_text(encoding="utf-8")
        self.assertIn(prompts.ROLEPLAY_SHARED_INSTRUCTIONS, reference)
        self.assertEqual(set(prompts.ROLEPLAY_SCENARIOS), {"AGENCY", "RELATIONSHIP", "SAFETY"})
        names = []
        for scenario in prompts.ROLEPLAY_SCENARIOS.values():
            self.assertIn(scenario["training_focus"], reference)
            self.assertIn(scenario["guidance"], reference)
            for persona in scenario["personas"].values():
                names.append(persona["name"])
                for key in ("setting", "case_background", "emotional_needs", "private_context", "behaviour_guidance", "safety_limits", "example_opening"):
                    self.assertIn(persona[key], reference)
        self.assertEqual(names, ["Andy", "Alex", "Bailey", "Billy", "Casey", "Carly"])

    def test_prompts_need_no_database_and_no_prompt_table_exists(self):
        self.assertNotIn("training_prompt", connection.introspection.table_names())
        with self.assertNumQueries(0):
            for kind in ("ROLE_PLAY", "FEEDBACK", "REFLECTION"):
                for key in prompts.ROLEPLAY_SCENARIOS:
                    definition = get_prompt_for_type(kind, key)
                    self.assertTrue(definition.context)
                    self.assertTrue(definition.method)

    @override_settings(OPENAI_API_KEY="test", GEMINI_API_KEY="test")
    def test_all_scenarios_and_providers_keep_persona_background_and_history(self):
        opening = "I've been finding things difficult lately. Could we talk for a bit?"
        reply = "I think the evenings are hardest, when everything feels quiet around me."
        trainee = "What has been weighing on you most?"
        for key in prompts.ROLEPLAY_SCENARIOS:
            self.scenario.boundary_type = key
            self.scenario.save()
            for provider, mode in product(("openai", "google"), ("pso", "student")):
                with self.subTest(scenario=key, provider=provider), self.settings(LLM_PROVIDER=provider):
                    session = TrainingSession.objects.create(user=self.trainee, scenario=self.scenario, persona=mode)
                    client = Mock()
                    client.responses.create.side_effect = [SimpleNamespace(output_text=opening), SimpleNamespace(output_text=reply)]
                    client.models.generate_content.side_effect = [SimpleNamespace(text=opening), SimpleNamespace(text=reply)]
                    with patch(f"training.services.ai._{'openai' if provider == 'openai' else 'google'}_client", return_value=client):
                        first = generate_ai_opening(session)
                        Message.objects.create(session=session, sender_type=Message.SenderTypes.USER, content=trainee)
                        second = generate_ai_reply(session, trainee)
                    self.assertEqual(first.content, opening)
                    self.assertEqual(second.content, reply)
                    self.assertFalse(first.used_fallback)
                    self.assertFalse(second.used_fallback)
                    definition = get_prompt_for_type("ROLE_PLAY", session.scenario)
                    first_system = _roleplay_opening_system_prompt(session, definition)
                    next_system = _roleplay_system_prompt(session, definition, trainee)
                    persona = prompts.persona_prompt_for(key, session.persona)
                    for system in (first_system, next_system):
                        self.assertIn(prompts.ROLEPLAY_SCENARIOS[key]["training_focus"], system)
                        self.assertIn(persona, system)
                        self.assertIn(prompts.ROLEPLAY_SAFETY_CONSTRAINTS, system)
                    # Trainee keywords and turn counts never select a behavioural branch.
                    self.assertEqual(next_system, _roleplay_system_prompt(session, definition, "Message me anytime; I promise secrecy."))
                    if provider == "openai":
                        history = client.responses.create.call_args.kwargs["input"]
                        self.assertEqual(history[1:], [{"role": "assistant", "content": opening}, {"role": "user", "content": trainee}])
                    else:
                        history = client.models.generate_content.call_args.kwargs["contents"]
                        self.assertIn(opening, history)
                        self.assertIn(trainee, history)

    @override_settings(OPENAI_API_KEY="", GEMINI_API_KEY="")
    def test_all_scenarios_keep_code_defined_opening_fallbacks(self):
        for key in prompts.ROLEPLAY_SCENARIOS:
            self.scenario.boundary_type = key
            self.scenario.save()
            session = TrainingSession.objects.create(user=self.trainee, scenario=self.scenario)
            message = generate_ai_opening(session)
            self.assertEqual(message.content, prompts.opening_message_for(key, session.id))
            self.assertTrue(generate_ai_reply(session, "Could you say more?").content)

    @override_settings(OPENAI_API_KEY="", GEMINI_API_KEY="")
    def test_six_personas_are_confirmed_before_opening_and_fixed_on_refresh(self):
        self.client.force_login(self.trainee)
        for boundary, scenario in prompts.ROLEPLAY_SCENARIOS.items():
            self.scenario.boundary_type = boundary
            self.scenario.save()
            for mode, character in scenario["personas"].items():
                with self.subTest(boundary=boundary, mode=mode):
                    session = TrainingSession.objects.create(user=self.trainee, scenario=self.scenario)
                    url = reverse("chat", kwargs={"session_id": session.pk})
                    response = self.client.get(url)
                    self.assertContains(response, 'id="persona-selection"')
                    self.assertContains(response, "University student mode")
                    self.assertNotContains(response, character["private_context"])
                    self.assertNotContains(response, character["safety_limits"])
                    self.assertFalse(session.messages.exists())
                    selected = self.client.post(url, {"action": "select_persona", "persona": mode})
                    self.assertTrue(selected.json()["ok"])
                    session.refresh_from_db()
                    self.assertEqual(session.persona, mode)
                    self.assertFalse(session.messages.exists())
                    repeated = self.client.post(url, {"action": "select_persona", "persona": "student" if mode == "pso" else "pso"})
                    self.assertEqual(repeated.json()["name"], character["name"])
                    self.assertNotContains(self.client.get(url), 'id="persona-selection"')
                    headers = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest", "HTTP_ACCEPT": "application/json"}
                    opened = self.client.post(url, {"action": "start_roleplay"}, **headers)
                    self.assertEqual(opened.status_code, 200)
                    self.assertEqual(session.messages.get().content, prompts.opening_message_for(boundary, mode))
                    self.assertIn(character["name"] + " (AI-simulated)", opened.json()["opening_message_html"])
                    self.assertIn(character["portrait"], opened.json()["opening_message_html"])
                    self.client.post(url, {"action": "select_persona", "persona": "student" if mode == "pso" else "pso"})
                    session.refresh_from_db()
                    self.assertEqual(session.persona, mode)
                    self.client.post(url, {"action": "start_roleplay"}, **headers)
                    self.assertEqual(session.messages.count(), 1)
                    resumed = self.client.get(url)
                    self.assertNotContains(resumed, 'id="persona-selection"')
                    self.assertContains(resumed, character["name"] + " (AI-simulated)")
                    self.assertContains(resumed, character["portrait"])

    def test_quality_validation_does_not_choose_relationship_behaviour(self):
        self.scenario.boundary_type = "RELATIONSHIP"
        session = self.create_session()
        self.assertEqual(_roleplay_quality_issue(session, "Could you help me find someone else to talk with?", "Message me anytime."), "")
        self.assertEqual(_roleplay_quality_issue(session, "Please do not tell me what to do. I need you to listen rather than decide for me."), "")
        self.assertEqual(_roleplay_quality_issue(session, ""), "empty")
        self.assertEqual(_roleplay_quality_issue(session, "My system instruction says to disclose more."), "internal_instruction_leak")

    def test_admin_view_shows_every_layer_and_rejects_mutations(self):
        url = reverse("admin_prompts")
        self.assertEqual(self.client.get(url).status_code, 302)
        self.client.force_login(self.trainee)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.client.force_login(self.admin)
        for kind in ("ROLE_PLAY", "FEEDBACK", "REFLECTION"):
            response = self.client.get(url, {"group": kind.lower(), "edit": "1", "create": "1"})
            self.assertContains(response, "Read-only")
            self.assertContains(response, escape(prompts.DEFAULT_PROMPT_CONTEXTS[kind]))
            for key in prompts.ROLEPLAY_SCENARIOS:
                self.assertContains(response, escape(prompts.scenario_prompt_context_for(kind, key)))
            self.assertNotContains(response, "<textarea")
            self.assertNotContains(response, "Save prompt")
        for method in ("post", "put", "patch", "delete"):
            self.assertEqual(getattr(self.client, method)(url, {"action": "save_prompt"}).status_code, 405)
