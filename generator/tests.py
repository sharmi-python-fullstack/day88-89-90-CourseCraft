import json
from unittest.mock import Mock, patch

import requests
from django.contrib.auth.models import User
from django.db import DatabaseError
from django.test import TestCase

from .models import Answer, CourseTopic, GeneratedContent, Question, Quiz, Summary


class GenerateContentTests(TestCase):
    def setUp(self):
        self.teacher = User.objects.create_user(
            username="teacher",
            password="test-password",
        )
        self.client.force_login(self.teacher)

    def gemini_api_response(self):
        api_response = Mock()
        api_response.json.return_value = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": json.dumps(
                                    {
                                        "notes": "A concise explanation.",
                                        "keyPoints": [
                                            "First point",
                                            "Second point",
                                        ],
                                        "summary": "A short topic summary.",
                                        "quiz": {
                                            "title": "Photosynthesis quiz",
                                            "description": "Check your understanding.",
                                            "questions": [
                                                {
                                                    "questionText": f"Question {number}?",
                                                    "options": [
                                                        {
                                                            "text": f"Correct option {number}",
                                                            "isCorrect": True,
                                                        },
                                                        {
                                                            "text": f"Incorrect option {number}",
                                                            "isCorrect": False,
                                                        },
                                                    ],
                                                }
                                                for number in range(1, 11)
                                            ],
                                        },
                                    }
                                )
                            }
                        ]
                    }
                }
            ]
        }
        return api_response

    def test_get_renders_blank_topic_form(self):
        response = self.client.get("/generate/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="title"')
        self.assertContains(response, "Enter a course topic")

    @patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"})
    @patch("generator.views.requests.post")
    def test_post_saves_generated_content_and_displays_it(self, mock_post):
        mock_post.return_value = self.gemini_api_response()

        response = self.client.post("/generate/", {"title": "Photosynthesis"})

        self.assertEqual(response.status_code, 302)
        topic = CourseTopic.objects.get(title="Photosynthesis")
        content = GeneratedContent.objects.get(course_topic=topic)
        self.assertEqual(topic.teacher, self.teacher)
        self.assertEqual(content.notes, "A concise explanation.")
        self.assertEqual(content.key_points, "First point\nSecond point")
        self.assertEqual(content.quiz.title, "Photosynthesis quiz")
        self.assertEqual(content.quiz.questions.count(), 10)
        question = content.quiz.questions.order_by("pk").first()
        self.assertEqual(question.question_text, "Question 1?")
        self.assertEqual(
            list(
                Answer.objects.filter(question=question)
                .order_by("id")
                .values_list("answer_text", "is_correct")
            ),
            [("Correct option 1", True), ("Incorrect option 1", False)],
        )
        self.assertEqual(
            Summary.objects.get(course_topic=topic).summary_text,
            "A short topic summary.",
        )
        self.assertIn(f"/content/{content.pk}/", response["Location"])

        detail_response = self.client.get(response["Location"])
        self.assertEqual(detail_response.status_code, 200)
        self.assertContains(detail_response, "A concise explanation.")
        self.assertContains(detail_response, "First point")
        self.assertContains(detail_response, "Second point")
        self.assertContains(detail_response, "A short topic summary.")
        self.assertContains(detail_response, "Photosynthesis quiz")
        self.assertContains(detail_response, "Correct option 1")
        self.assertContains(detail_response, "(Correct)")
        generation_config = mock_post.call_args.kwargs["json"]["generationConfig"]
        question_schema = generation_config["responseSchema"]["properties"]["quiz"][
            "properties"
        ]["questions"]
        self.assertEqual(question_schema["minItems"], 10)
        self.assertNotIn("maxItems", question_schema)

    @patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"})
    @patch("generator.views.requests.post")
    def test_fewer_than_ten_question_response_is_rejected(self, mock_post):
        api_response = self.gemini_api_response()
        response_data = api_response.json.return_value
        model_output = json.loads(
            response_data["candidates"][0]["content"]["parts"][0]["text"]
        )
        model_output["quiz"]["questions"] = model_output["quiz"]["questions"][:1]
        response_data["candidates"][0]["content"]["parts"][0]["text"] = json.dumps(
            model_output
        )
        mock_post.return_value = api_response

        with self.assertLogs("generator.views", level="ERROR"):
            response = self.client.post("/generate/", {"title": "Photosynthesis"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "returned an invalid response")
        self.assertFalse(CourseTopic.objects.exists())

    @patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"})
    @patch("generator.views.requests.post")
    def test_more_than_ten_questions_are_saved(self, mock_post):
        api_response = self.gemini_api_response()
        response_data = api_response.json.return_value
        model_output = json.loads(
            response_data["candidates"][0]["content"]["parts"][0]["text"]
        )
        model_output["quiz"]["questions"].append(
            {
                "questionText": "Question 11?",
                "options": [
                    {"text": "Correct option 11", "isCorrect": True},
                    {"text": "Incorrect option 11", "isCorrect": False},
                ],
            }
        )
        response_data["candidates"][0]["content"]["parts"][0]["text"] = json.dumps(
            model_output
        )
        mock_post.return_value = api_response

        response = self.client.post("/generate/", {"title": "Photosynthesis"})

        self.assertEqual(response.status_code, 302)
        content = GeneratedContent.objects.get(
            course_topic__title="Photosynthesis"
        )
        self.assertEqual(content.quiz.questions.count(), 11)

    @patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"})
    @patch("generator.views.requests.post")
    def test_invalid_model_json_shows_error_without_saving(self, mock_post):
        api_response = Mock()
        api_response.json.return_value = {
            "candidates": [
                {"content": {"parts": [{"text": "not valid JSON"}]}}
            ]
        }
        mock_post.return_value = api_response

        with self.assertLogs("generator.views", level="ERROR"):
            response = self.client.post("/generate/", {"title": "Cell biology"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "returned an invalid response")
        self.assertFalse(CourseTopic.objects.exists())
        self.assertFalse(GeneratedContent.objects.exists())

    @patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"})
    @patch("generator.views.requests.post")
    def test_unavailable_model_reports_status_without_logging_api_key(self, mock_post):
        api_response = Mock()
        api_response.raise_for_status.side_effect = requests.exceptions.HTTPError(
            response=Mock(status_code=404)
        )
        mock_post.return_value = api_response

        with self.assertLogs("generator.views", level="WARNING") as logs:
            response = self.client.post("/generate/", {"title": "Photosynthesis"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "configured Gemini model is unavailable")
        self.assertIn("HTTP status 404", "\n".join(logs.output))
        self.assertNotIn("test-key", "\n".join(logs.output))

    @patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"})
    @patch(
        "generator.views._save_generated_content",
        side_effect=DatabaseError("database unavailable"),
    )
    @patch("generator.views.requests.post")
    def test_database_error_is_logged_and_shown_to_the_teacher(
        self,
        mock_post,
        _mock_save,
    ):
        mock_post.return_value = self.gemini_api_response()

        with self.assertLogs("generator.views", level="ERROR"):
            response = self.client.post("/generate/", {"title": "Photosynthesis"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "save the generated content.")
        self.assertFalse(CourseTopic.objects.exists())


class StudentLmsTests(TestCase):
    def setUp(self):
        teacher = User.objects.create_user(username="course-teacher")
        self.topic = CourseTopic.objects.create(
            title="Photosynthesis",
            teacher=teacher,
        )
        self.quiz = Quiz.objects.create(
            course_topic=self.topic,
            title="Photosynthesis quiz",
            description="Check your understanding.",
        )
        self.questions = []
        for number in range(1, 5):
            question = Question.objects.create(
                quiz=self.quiz,
                question_text=f"Question {number}?",
            )
            correct_answer = Answer.objects.create(
                question=question,
                answer_text=f"Correct option {number}",
                is_correct=True,
            )
            incorrect_answer = Answer.objects.create(
                question=question,
                answer_text=f"Incorrect option {number}",
                is_correct=False,
            )
            self.questions.append((question, correct_answer, incorrect_answer))

        GeneratedContent.objects.create(
            course_topic=self.topic,
            quiz=self.quiz,
            notes="Course notes.",
            key_points="First key point\nSecond key point",
        )
        Summary.objects.create(
            course_topic=self.topic,
            summary_text="Course summary.",
        )

    def test_dashboard_and_topic_content_are_available_to_students(self):
        dashboard_response = self.client.get("/")

        self.assertEqual(dashboard_response.status_code, 200)
        self.assertContains(dashboard_response, "Photosynthesis")
        self.assertContains(
            dashboard_response,
            f"/topics/{self.topic.pk}/",
        )

        content_response = self.client.get(f"/topics/{self.topic.pk}/")
        self.assertEqual(content_response.status_code, 200)
        self.assertContains(content_response, "Course notes.")
        self.assertContains(content_response, "First key point")
        self.assertContains(content_response, "Course summary.")
        self.assertContains(content_response, "Photosynthesis quiz")
        self.assertContains(content_response, "Correct option 1")
        self.assertContains(
            content_response,
            f'name="question_{self.questions[0][0].pk}"',
        )

    def test_quiz_submission_scores_answers_and_reports_each_result(self):
        first_question, first_correct, _ = self.questions[0]
        second_question, _, second_incorrect = self.questions[1]
        third_question = self.questions[2][0]
        fourth_question = self.questions[3][0]

        response = self.client.post(
            f"/quiz/{self.quiz.pk}/submit/",
            {
                f"question_{first_question.pk}": str(first_correct.pk),
                f"question_{second_question.pk}": str(second_incorrect.pk),
                f"question_{third_question.pk}": str(first_correct.pk),
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "1 <span>/ 4</span>")
        self.assertContains(response, "Your score: 25%")
        self.assertContains(response, "Correct — Correct option 1")
        self.assertContains(response, "Your answer: Incorrect option 2")
        self.assertContains(response, "Correct answer: Correct option 2")
        self.assertContains(response, "You did not answer this question.")
        self.assertContains(response, "Correct answer: Correct option 3")
        self.assertContains(response, "Question 4?")
        self.assertEqual(len(response.context["results"]), 4)

    def test_quiz_submission_rejects_get_requests(self):
        response = self.client.get(f"/quiz/{self.quiz.pk}/submit/")

        self.assertEqual(response.status_code, 405)


class StudyMaterialModelTests(TestCase):
    fixtures = ["study_material.json"]

    def test_course_topic_can_be_created_updated_and_deleted(self):
        teacher = User.objects.get(pk=1)
        topic = CourseTopic.objects.create(title="New topic", teacher=teacher)
        topic.title = "Updated topic"
        topic.save()
        self.assertEqual(CourseTopic.objects.get(pk=topic.pk).title, "Updated topic")

        topic.delete()
        self.assertFalse(CourseTopic.objects.filter(pk=topic.pk).exists())

    def test_generated_content_can_be_created_updated_and_deleted(self):
        topic = CourseTopic.objects.get(pk=1)
        quiz = Quiz.objects.get(pk=1)
        content = GeneratedContent.objects.create(
            course_topic=topic,
            quiz=quiz,
            notes="Initial notes",
            key_points="Initial point",
        )
        content.notes = "Updated notes"
        content.save()
        self.assertEqual(
            GeneratedContent.objects.get(pk=content.pk).notes,
            "Updated notes",
        )

        content.delete()
        self.assertFalse(GeneratedContent.objects.filter(pk=content.pk).exists())

    def test_quiz_can_be_created_updated_and_deleted(self):
        topic = CourseTopic.objects.get(pk=1)
        quiz = Quiz.objects.create(
            course_topic=topic,
            title="New quiz",
            description="Initial description",
        )
        quiz.title = "Updated quiz"
        quiz.save()
        self.assertEqual(Quiz.objects.get(pk=quiz.pk).title, "Updated quiz")

        quiz.delete()
        self.assertFalse(Quiz.objects.filter(pk=quiz.pk).exists())

    def test_question_can_be_created_updated_and_deleted(self):
        quiz = Quiz.objects.get(pk=1)
        question = Question.objects.create(quiz=quiz, question_text="Initial question")
        question.question_text = "Updated question"
        question.save()
        self.assertEqual(
            Question.objects.get(pk=question.pk).question_text,
            "Updated question",
        )

        question.delete()
        self.assertFalse(Question.objects.filter(pk=question.pk).exists())

    def test_answer_can_be_created_updated_and_deleted(self):
        question = Question.objects.get(pk=1)
        answer = Answer.objects.create(
            question=question,
            answer_text="Initial answer",
            is_correct=False,
        )
        answer.answer_text = "Updated answer"
        answer.is_correct = True
        answer.save()
        updated_answer = Answer.objects.get(pk=answer.pk)
        self.assertEqual(updated_answer.answer_text, "Updated answer")
        self.assertTrue(updated_answer.is_correct)

        answer.delete()
        self.assertFalse(Answer.objects.filter(pk=answer.pk).exists())

    def test_summary_can_be_created_updated_and_deleted(self):
        topic = CourseTopic.objects.get(pk=1)
        summary = Summary.objects.create(
            course_topic=topic,
            summary_text="Initial summary",
        )
        summary.summary_text = "Updated summary"
        summary.save()
        self.assertEqual(
            Summary.objects.get(pk=summary.pk).summary_text,
            "Updated summary",
        )

        summary.delete()
        self.assertFalse(Summary.objects.filter(pk=summary.pk).exists())


class ExportPdfTests(TestCase):
    fixtures = ["study_material.json"]

    def test_export_pdf_returns_an_attachment(self):
        response = self.client.get("/topics/1/pdf/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn("attachment;", response["Content-Disposition"])
        pdf_data = b"".join(response.streaming_content)
        self.assertTrue(pdf_data.startswith(b"%PDF-"))
