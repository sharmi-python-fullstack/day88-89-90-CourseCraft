from django.contrib import admin
from .models import (
    Answer,
    CourseTopic,
    GeneratedContent,
    Question,
    Quiz,
    Summary,
)


admin.site.register(
    (CourseTopic, GeneratedContent, Quiz, Question, Answer, Summary)
)
