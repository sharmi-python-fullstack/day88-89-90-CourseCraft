import asyncio
import json
import logging
import os
from io import BytesIO
from xml.sax.saxutils import escape

import requests
from asgiref.sync import sync_to_async
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db import DatabaseError, transaction
from django.db.models import Prefetch
from django.http import FileResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.text import slugify
from django.views.decorators.http import require_POST
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    ListFlowable,
    ListItem,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
)

from .forms import CourseTopicForm
from .models import (
    Answer,
    CourseTopic,
    GeneratedContent,
    Question,
    Quiz,
    Summary,
)

logger = logging.getLogger(__name__)
MIN_QUIZ_QUESTION_COUNT = 10
GEMINI_API_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{settings.GEMINI_MODEL}:generateContent"
)


def _save_generated_content(
    title,
    teacher_id,
    notes,
    key_points,
    summary_text,
    quiz_data,
):
    with transaction.atomic():
        topic = CourseTopic.objects.create(title=title, teacher_id=teacher_id)
        quiz = Quiz.objects.create(
            course_topic=topic,
            title=quiz_data["title"],
            description=quiz_data["description"],
        )
        content = GeneratedContent.objects.create(
            course_topic=topic,
            quiz=quiz,
            notes=notes,
            key_points="\n".join(key_points),
        )
        for question_data in quiz_data["questions"]:
            question = Question.objects.create(
                quiz=quiz,
                question_text=question_data["questionText"],
            )
            Answer.objects.bulk_create(
                [
                    Answer(
                        question=question,
                        answer_text=option["text"],
                        is_correct=option["isCorrect"],
                    )
                    for option in question_data["options"]
                ]
            )
        Summary.objects.create(course_topic=topic, summary_text=summary_text)
    return content


def _parse_generated_content(response_data):
    try:
        generated_text = response_data["candidates"][0]["content"]["parts"][0]["text"]
        content = json.loads(generated_text)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("The Gemini response did not contain valid JSON content.") from exc

    if not isinstance(content, dict):
        raise ValueError("The Gemini response must be a JSON object.")

    notes = content.get("notes")
    key_points = content.get("keyPoints")
    summary_text = content.get("summary")
    quiz_data = content.get("quiz")
    if not isinstance(notes, str) or not isinstance(key_points, list) or not all(
        isinstance(point, str) for point in key_points
    ):
        raise ValueError("The Gemini response is missing notes or valid key points.")
    if not isinstance(summary_text, str):
        raise ValueError("The Gemini response is missing a valid summary.")
    if (
        not isinstance(quiz_data, dict)
        or not isinstance(quiz_data.get("title"), str)
        or not isinstance(quiz_data.get("description"), str)
        or not isinstance(quiz_data.get("questions"), list)
        or len(quiz_data["questions"]) < MIN_QUIZ_QUESTION_COUNT
    ):
        raise ValueError(
            f"The quiz must contain at least {MIN_QUIZ_QUESTION_COUNT} questions."
        )

    for question in quiz_data["questions"]:
        if (
            not isinstance(question, dict)
            or not isinstance(question.get("questionText"), str)
            or not isinstance(question.get("options"), list)
            or len(question["options"]) < 2
        ):
            raise ValueError("The Gemini response contains an invalid quiz question.")

        options = question["options"]
        if any(
            not isinstance(option, dict)
            or not isinstance(option.get("text"), str)
            or not isinstance(option.get("isCorrect"), bool)
            for option in options
        ) or sum(option["isCorrect"] for option in options) != 1:
            raise ValueError("Each quiz question must have one correct answer.")

    return notes, key_points, summary_text, quiz_data


def student_dashboard(request):
    topics = CourseTopic.objects.prefetch_related(
        Prefetch(
            "generatedcontent_set",
            queryset=GeneratedContent.objects.order_by("-pk"),
            to_attr="student_contents",
        )
    ).order_by("title")
    return render(request, "generator/student_dashboard.html", {"topics": topics})


def view_content(request, topic_id):
    topic = get_object_or_404(CourseTopic, pk=topic_id)
    content = (
        GeneratedContent.objects.filter(course_topic=topic)
        .select_related("quiz")
        .prefetch_related("quiz__questions__answers")
        .order_by("-pk")
        .first()
    )
    summary = topic.summaries.order_by("-pk").first()
    quiz = content.quiz if content else None
    return render(
        request,
        "generator/view_content.html",
        {
            "topic": topic,
            "content": content,
            "summary": summary,
            "quiz": quiz,
        },
    )


@require_POST
def submit_quiz(request, quiz_id):
    quiz = get_object_or_404(
        Quiz.objects.prefetch_related(
            Prefetch(
                "questions",
                queryset=Question.objects.prefetch_related("answers"),
            )
        ),
        pk=quiz_id,
    )
    questions = list(quiz.questions.all())
    results = []
    score = 0

    for question in questions:
        try:
            selected_answer_id = int(request.POST.get(f"question_{question.pk}"))
        except (TypeError, ValueError):
            selected_answer_id = None

        answers = list(question.answers.all())
        selected_answer = next(
            (answer for answer in answers if answer.pk == selected_answer_id),
            None,
        )
        correct_answer = next(
            (answer for answer in answers if answer.is_correct),
            None,
        )
        is_correct = selected_answer is not None and selected_answer.is_correct
        score += int(is_correct)
        results.append(
            {
                "question": question,
                "selected_answer": selected_answer,
                "correct_answer": correct_answer,
                "is_correct": is_correct,
            }
        )

    total = len(questions)
    percentage = round(score / total * 100) if total else 0
    return render(
        request,
        "generator/quiz_results.html",
        {
            "quiz": quiz,
            "results": results,
            "score": score,
            "total": total,
            "percentage": percentage,
        },
    )


def export_pdf(request, topic_id):
    topic = get_object_or_404(
        CourseTopic.objects.select_related("teacher"),
        pk=topic_id,
    )
    content = get_object_or_404(
        GeneratedContent.objects.filter(course_topic=topic)
        .select_related("quiz")
        .prefetch_related("quiz__questions__answers")
        .order_by("-pk")
    )
    summary = topic.summaries.order_by("-pk").first()
    quiz = content.quiz

    buffer = BytesIO()
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "CoverTitle",
        parent=styles["Title"],
        alignment=TA_CENTER,
        spaceAfter=18,
    )
    body_style = styles["BodyText"]
    story = [
        Spacer(1, 2 * inch),
        Paragraph(escape(topic.title), title_style),
        Paragraph(
            f"Teacher: {escape(topic.teacher.get_full_name() or topic.teacher.username)}",
            ParagraphStyle("TeacherName", parent=body_style, alignment=TA_CENTER),
        ),
        PageBreak(),
    ]

    story.append(Paragraph("Summary", styles["Heading1"]))
    if summary:
        story.append(
            Paragraph(
                escape(summary.summary_text).replace("\n", "<br/>"),
                body_style,
            )
        )
    else:
        story.append(Paragraph("No summary is available.", body_style))
    story.append(Spacer(1, 12))

    story.append(Paragraph("Notes", styles["Heading1"]))
    story.append(Paragraph(escape(content.notes).replace("\n", "<br/>"), body_style))
    story.append(Spacer(1, 12))

    story.append(Paragraph("Key Points", styles["Heading1"]))
    key_points = [
        point.strip()
        for point in content.key_points.splitlines()
        if point.strip()
    ]
    if key_points:
        story.append(
            ListFlowable(
                [
                    ListItem(Paragraph(escape(point), body_style))
                    for point in key_points
                ],
                bulletType="bullet",
                leftIndent=18,
            )
        )
    else:
        story.append(Paragraph("No key points are available.", body_style))

    if quiz:
        questions = list(quiz.questions.all())
        story.extend(
            [
                Spacer(1, 12),
                Paragraph(escape(quiz.title), styles["Heading1"]),
                Paragraph(escape(quiz.description).replace("\n", "<br/>"), body_style),
            ]
        )
        correct_answers = []
        for index, question in enumerate(questions, start=1):
            story.append(
                Paragraph(
                    f"{index}. {escape(question.question_text)}",
                    styles["Heading3"],
                )
            )
            answers = list(question.answers.all())
            story.append(
                ListFlowable(
                    [
                        ListItem(Paragraph(escape(answer.answer_text), body_style))
                        for answer in answers
                    ],
                    bulletType="bullet",
                    leftIndent=18,
                )
            )
            correct_answers.extend(
                f"{index}. {question.question_text}: {answer.answer_text}"
                for answer in answers
                if answer.is_correct
            )

        story.append(PageBreak())
        story.append(Paragraph("Correct Answers", styles["Heading1"]))
        if correct_answers:
            story.append(
                ListFlowable(
                    [
                        ListItem(
                            Paragraph(
                                escape(answer).replace("\n", "<br/>"),
                                body_style,
                            )
                        )
                        for answer in correct_answers
                    ],
                    bulletType="1",
                    leftIndent=18,
                )
            )
        else:
            story.append(Paragraph("No correct answers are available.", body_style))

    document = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        rightMargin=0.75 * inch,
        leftMargin=0.75 * inch,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch,
        title=topic.title,
        author=topic.teacher.get_full_name() or topic.teacher.username,
    )
    document.build(story)
    buffer.seek(0)
    filename = f"{slugify(topic.title) or 'course-topic'}.pdf"
    return FileResponse(
        buffer,
        as_attachment=True,
        filename=filename,
        content_type="application/pdf",
    )


@login_required(login_url="/admin/login/")
async def generate_content(request):
    if request.method == "POST":
        form = CourseTopicForm(request.POST)
        if form.is_valid():
            api_key = os.environ.get("GEMINI_API_KEY")
            if not api_key:
                form.add_error(None, "The content generation service is not configured.")
            else:
                prompt = (
                    "Act as an educational content generator. Create clear, accurate, "
                    "well-organized study notes, concise key points, a short summary, "
                    "and a multiple-choice quiz for this course topic: "
                    f"{form.cleaned_data['title']!r}. The quiz must contain at least "
                    f"{MIN_QUIZ_QUESTION_COUNT} distinct questions, at least two "
                    "options per question, and exactly one "
                    "correct option per question. Return only a JSON object matching "
                    'this schema: {"notes": "string", "keyPoints": ["string"], '
                    '"summary": "string", "quiz": {"title": "string", '
                    '"description": "string", "questions": [{"questionText": "string", '
                    '"options": [{"text": "string", "isCorrect": true}]}]}}.'
                )
                payload = {
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {
                        "responseMimeType": "application/json",
                        "responseSchema": {
                            "type": "OBJECT",
                            "properties": {
                                "notes": {"type": "STRING"},
                                "keyPoints": {
                                    "type": "ARRAY",
                                    "items": {"type": "STRING"},
                                },
                                "summary": {"type": "STRING"},
                                "quiz": {
                                    "type": "OBJECT",
                                    "properties": {
                                        "title": {"type": "STRING"},
                                        "description": {"type": "STRING"},
                                        "questions": {
                                            "type": "ARRAY",
                                            "minItems": MIN_QUIZ_QUESTION_COUNT,
                                            "items": {
                                                "type": "OBJECT",
                                                "properties": {
                                                    "questionText": {
                                                        "type": "STRING"
                                                    },
                                                    "options": {
                                                        "type": "ARRAY",
                                                        "items": {
                                                            "type": "OBJECT",
                                                            "properties": {
                                                                "text": {
                                                                    "type": "STRING"
                                                                },
                                                                "isCorrect": {
                                                                    "type": "BOOLEAN"
                                                                },
                                                            },
                                                            "required": [
                                                                "text",
                                                                "isCorrect",
                                                            ],
                                                        },
                                                    },
                                                },
                                                "required": [
                                                    "questionText",
                                                    "options",
                                                ],
                                            },
                                        },
                                    },
                                    "required": [
                                        "title",
                                        "description",
                                        "questions",
                                    ],
                                },
                            },
                            "required": [
                                "notes",
                                "keyPoints",
                                "summary",
                                "quiz",
                            ],
                        },
                    },
                }

                try:
                    response = await asyncio.to_thread(
                        requests.post,
                        GEMINI_API_URL,
                        params={"key": api_key},
                        json=payload,
                        timeout=30,
                    )
                    response.raise_for_status()
                    response_data = response.json()
                    notes, key_points, summary_text, quiz_data = (
                        _parse_generated_content(response_data)
                    )
                except (json.JSONDecodeError, ValueError):
                    logger.exception("Gemini returned invalid generated content.")
                    form.add_error(
                        None,
                        "The content generation service returned an invalid response. "
                        "Please try again.",
                    )
                except requests.exceptions.HTTPError as error:
                    status_code = (
                        error.response.status_code
                        if error.response is not None
                        else None
                    )
                    logger.warning(
                        "Gemini API returned HTTP status %s.",
                        status_code,
                    )
                    if status_code == 404:
                        error_message = (
                            "The configured Gemini model is unavailable. "
                            "Check GEMINI_MODEL in the deployment settings."
                        )
                    elif status_code in {401, 403}:
                        error_message = (
                            "Gemini rejected the API credentials or project access. "
                            "Check GEMINI_API_KEY and API access."
                        )
                    elif status_code == 429:
                        error_message = (
                            "Gemini's quota or rate limit was reached. Please try again later."
                        )
                    else:
                        error_message = (
                            "The content generation service rejected the request. "
                            "Check the deployment logs."
                        )
                    form.add_error(None, error_message)
                except requests.exceptions.RequestException as error:
                    logger.warning(
                        "Gemini API request failed (%s).",
                        type(error).__name__,
                    )
                    form.add_error(
                        None,
                        "Could not reach the content generation service. Please try again.",
                    )
                else:
                    teacher_id = await sync_to_async(
                        lambda: request.user.pk,
                        thread_sensitive=True,
                    )()
                    try:
                        content = await sync_to_async(
                            _save_generated_content,
                            thread_sensitive=True,
                        )(
                            form.cleaned_data["title"],
                            teacher_id,
                            notes,
                            key_points,
                            summary_text,
                            quiz_data,
                        )
                    except DatabaseError:
                        logger.exception("Could not save generated course content.")
                        form.add_error(
                            None,
                            "We couldn't save the generated content. Please try again.",
                        )
                    else:
                        return redirect(
                            "generator:content_detail", content_id=content.pk
                        )
    else:
        form = CourseTopicForm()

    return render(request, "generator/generate_content.html", {"form": form})


@login_required(login_url="/admin/login/")
def content_detail(request, content_id):
    content = get_object_or_404(
        GeneratedContent,
        pk=content_id,
        course_topic__teacher=request.user,
    )
    summary = content.course_topic.summaries.first()
    return render(
        request,
        "generator/content_detail.html",
        {"content": content, "summary": summary},
    )
