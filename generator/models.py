from django.db import models
from django.contrib.auth.models import User


class CourseTopic(models.Model):
    title = models.CharField(max_length=255)
    teacher = models.ForeignKey(User, on_delete=models.CASCADE)

    def __str__(self):
        return self.title


class Quiz(models.Model):
    course_topic = models.ForeignKey(
        CourseTopic,
        on_delete=models.CASCADE,
        related_name="quizzes",
    )
    title = models.CharField(max_length=255)
    description = models.TextField()

    def __str__(self):
        return self.title


class Question(models.Model):
    quiz = models.ForeignKey(
        Quiz,
        on_delete=models.CASCADE,
        related_name="questions",
    )
    question_text = models.TextField()

    def __str__(self):
        return self.question_text


class Answer(models.Model):
    question = models.ForeignKey(
        Question,
        on_delete=models.CASCADE,
        related_name="answers",
    )
    answer_text = models.TextField()
    is_correct = models.BooleanField(default=False)

    def __str__(self):
        return self.answer_text


class GeneratedContent(models.Model):
    course_topic = models.ForeignKey(CourseTopic, on_delete=models.CASCADE)
    quiz = models.ForeignKey(
        Quiz,
        on_delete=models.CASCADE,
        related_name="generated_contents",
        null=True,
        blank=True,
    )
    notes = models.TextField()
    key_points = models.TextField()

    def __str__(self):
        return f"Generated content for {self.course_topic}"


class Summary(models.Model):
    course_topic = models.ForeignKey(
        CourseTopic,
        on_delete=models.CASCADE,
        related_name="summaries",
    )
    summary_text = models.TextField()

    def __str__(self):
        return f"Summary for {self.course_topic}"
