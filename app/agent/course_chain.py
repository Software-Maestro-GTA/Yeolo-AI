"""Compatibility entry point for callers that need a final course without SSE."""

from contextlib import aclosing

from app.agent.course_graph import stream_course_generation
from app.schemas.course import CourseRequestSchema, CourseSchema


async def run_course_generation_chain(request: CourseRequestSchema) -> CourseSchema:
    """Run the verified graph and return its final course or propagate a failure."""
    async with aclosing(stream_course_generation(request)) as events:
        async for event, data in events:
            if event == 'complete':
                return CourseSchema.model_validate(data['course'])
    raise ValueError('Course generation ended without a complete course')
