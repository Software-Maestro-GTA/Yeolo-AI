"""Freeze requests/SSE and the approved nullable response attribution additions."""

import json
from pathlib import Path

from app.schemas.course import (
    CourseRequestSchema,
    CourseResponseSchema,
    ProgressEventData,
)


def test_course_api_schemas_match_approved_attribution_contract():
    """Only the explicitly approved photo attribution response extension is allowed."""
    expected = json.loads(
        (Path(__file__).parent / 'fixtures/course_api_schema_baseline.json').read_text()
    )
    actual = {
        model.__name__: model.model_json_schema()
        for model in (CourseRequestSchema, CourseResponseSchema, ProgressEventData)
    }
    assert actual == expected
