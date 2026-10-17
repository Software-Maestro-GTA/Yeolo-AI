"""Freeze the approved language, numeric budget and minimal course response contract."""

import json
from pathlib import Path

from app.schemas.course import (
    CourseRequestSchema,
    CourseResponseSchema,
    ProgressEventData,
)


def test_course_api_schemas_match_approved_minimal_response_contract():
    """Capture the approved API-AI-2 schema without restoring removed detail fields."""
    expected = json.loads(
        (Path(__file__).parent / 'fixtures/course_api_schema_baseline.json').read_text()
    )
    actual = {
        model.__name__: model.model_json_schema()
        for model in (CourseRequestSchema, CourseResponseSchema, ProgressEventData)
    }
    assert actual == expected
