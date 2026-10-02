"""Freeze the existing course request and SSE payload schemas during refactoring."""

import json
from pathlib import Path

from app.schemas.course import (
    CourseRequestSchema,
    CourseResponseSchema,
    ProgressEventData,
)


def test_course_api_schemas_match_pre_refactor_contract():
    """Internal graph changes must not add, remove, or constrain public fields."""
    expected = json.loads(
        (Path(__file__).parent / 'fixtures/course_api_schema_baseline.json').read_text()
    )
    actual = {
        model.__name__: model.model_json_schema()
        for model in (CourseRequestSchema, CourseResponseSchema, ProgressEventData)
    }
    assert actual == expected
