"""Verify the explicitly approved nullable photo attribution response additions."""

import pytest
from pydantic import ValidationError

from app.schemas.course import CourseSchema, PlaceSchema


def test_approved_response_fields_are_nullable_and_default_to_none():
    place = PlaceSchema(placeId='id', placeName='검증 장소', category='museum', latitude=35, longitude=139)
    assert place.photoAttribution is None
    assert CourseSchema.model_fields['coverImageAttribution'].default is None
    assert 'photoAttribution' not in PlaceSchema.model_json_schema()['required']
    assert 'coverImageAttribution' not in CourseSchema.model_json_schema()['required']


def test_attribution_preserves_multiple_authors_and_optional_author_links():
    from app.schemas.course import PhotoAttributionSchema

    attribution = PhotoAttributionSchema.model_validate({
        'googleMapsUri': 'https://www.google.com/maps/place/?cid=1',
        'authorAttributions': [
            {'displayName': '작성자 A', 'uri': 'https://www.google.com/maps/contrib/a',
             'photoUri': 'https://lh3.googleusercontent.com/a/author-a'},
            {'displayName': '작성자 B'},
        ],
    })
    assert attribution.provider == 'Google Maps'
    assert [author.displayName for author in attribution.authorAttributions] == ['작성자 A', '작성자 B']
    assert attribution.authorAttributions[1].uri is None
    assert attribution.authorAttributions[1].photoUri is None
    empty_authors = PhotoAttributionSchema(googleMapsUri='https://www.google.com/maps/place/?cid=2')
    assert empty_authors.authorAttributions == []
    empty_authors.authorAttributions.append(attribution.authorAttributions[0])
    assert len(attribution.authorAttributions) == 2


@pytest.mark.parametrize('payload', [{}, {'googleMapsUri': 'https://www.google.com/maps/place/?cid=1', 'provider': 'Unsplash'}, {'googleMapsUri': 'https://www.google.com/maps/place/?cid=1', 'authorAttributions': [{}]}])
def test_attribution_requires_source_provider_and_author_names(payload):
    from app.schemas.course import PhotoAttributionSchema

    with pytest.raises(ValidationError):
        PhotoAttributionSchema.model_validate(payload)
