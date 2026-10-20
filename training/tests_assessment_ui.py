"""Verify trainee presentation without changing the internal assessment data."""

from types import SimpleNamespace
from pathlib import Path
from xml.etree import ElementTree

from django.contrib.staticfiles import finders
from django.template.loader import render_to_string
from django.test import SimpleTestCase

from training.templatetags.assessment_ui import assessment_status


class AssessmentPresentationTests(SimpleTestCase):
    def test_live_and_overall_mappings_including_unavailable(self):
        cases = [
            (1, "Needs attention", "Needs improvement", "attention"),
            (2, "Finding balance", "Keep practising", "balance"),
            (3, "Making progress", "Good progress", "progress"),
            (4, "Well handled", "Well done", "handled"),
            (5, "Excellent handling", "Excellent work", "excellent"),
            (None, "Listening in progress", "Not observed", "neutral"),
            (0, "Listening in progress", "Not observed", "neutral"),
        ]
        for score, live_label, overall_title, tone in cases:
            with self.subTest(score=score):
                live = render_to_string(
                    "trainee/includes/boundary_coach_live.html",
                    {"boundary_status": SimpleNamespace(score=score)},
                )
                self.assertIn(live_label, live)
                self.assertIn(f"peertrain-assessment-{tone}", live)
                self.assertEqual(live.count('class="peertrain-assessment-icon"'), 1)
                asset = assessment_status(score)["icon_asset"]
                self.assertIn(f'/static/{asset}', live)
                asset_path = finders.find(asset)
                self.assertIsNotNone(asset_path)
                svg = ElementTree.fromstring(Path(asset_path).read_text(encoding="utf-8"))
                self.assertEqual(svg.tag, "{http://www.w3.org/2000/svg}svg")
                self.assertNotIn("Level ", live)
                self.assertNotIn("boundary-scale", live)
                compact = render_to_string(
                    "trainee/includes/assessment_status.html",
                    {"score": score, "compact": True},
                )
                self.assertIn(f'title="{live_label}"', compact)
                self.assertIn(f'class="visually-hidden">{live_label}', compact)
                review = render_to_string(
                    "trainee/session_review.html",
                    {
                        "feedback": SimpleNamespace(overall_score=score),
                        "session": SimpleNamespace(scenario=SimpleNamespace(title="Practice")),
                    },
                )
                self.assertIn(overall_title, review)
                self.assertIn(f"peertrain-assessment-title peertrain-assessment-{tone}", review)
                self.assertNotIn("Overall observed point", review)
                self.assertNotIn(f"Score {score}/5", review)
                self.assertIn("Boundary notebook", review)
                self.assertIn("Reflection", review)

    def test_boundary_feedback_cards_are_open_by_default(self):
        dimension = SimpleNamespace(
            key="relationship", name="Relationship Boundary", tone="wobbling",
            label="Wobbling", summary="Keep the support collaborative.",
        )
        live = render_to_string(
            "trainee/includes/boundary_coach_live.html",
            {
                "boundary_status": SimpleNamespace(score=2),
                "boundary_map": SimpleNamespace(dimensions=[dimension]),
            },
        )
        self.assertIn('<details open class="peertrain-boundary-map-card', live)
        self.assertIn("Keep the support collaborative.", live)
        self.assertIn('<summary class="peertrain-boundary-map-row">', live)
