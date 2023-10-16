"""Tests for the Immutable base class."""
import hashlib
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from agentcache.models import Immutable


class SampleImmutable(Immutable):
    """A sample Immutable subclass."""

    some_req_field: str
    some_opt_field: int = 2
    sub_immutable: "SampleImmutable" = None


def test_immutable_frozen() -> None:
    """Test that the `Immutable` class is frozen."""

    sample = SampleImmutable(some_req_field="test")

    with pytest.raises(ValidationError):
        sample.some_req_field = "test2"
    with pytest.raises(ValidationError):
        sample.some_opt_field = 3

    assert sample.some_req_field == "test"
    assert sample.some_opt_field == 2


def test_immutable_hash_key() -> None:
    """Test the `Immutable.hash_key` property."""
    sample = SampleImmutable(
        some_req_field="test", sub_immutable=SampleImmutable(some_req_field="юнікод", some_opt_field=3)
    )

    # print(sample.model_dump_json())
    expected_hash_key = hashlib.sha256(
        '{"model_":"tests.models.test_immutable.SampleImmutable","some_req_field":"test","some_opt_field":2,'
        '"sub_immutable":{"model_":"tests.models.test_immutable.SampleImmutable","some_req_field":"юнікод",'
        '"some_opt_field":3,"sub_immutable":null}}'.encode("utf-8")
    ).hexdigest()
    assert sample.hash_key == expected_hash_key


def test_immutable_hash_key_calculated_once() -> None:
    """
    Test that the `Immutable.hash_key` property is calculated only once and all subsequent calls return the same
    value without calculating it again.
    """
    original_sha256 = hashlib.sha256

    with patch("hashlib.sha256", side_effect=original_sha256) as mock_sha256:
        sample = SampleImmutable(some_req_field="test")
        mock_sha256.assert_not_called()  # not calculated yet

        assert sample.hash_key == "b7c86b292585e045aba60d4aed50f3b69e5600d7f8eae6feb9c012f5b6ed3da4"
        mock_sha256.assert_called_once()  # calculated once

        assert sample.hash_key == "b7c86b292585e045aba60d4aed50f3b69e5600d7f8eae6feb9c012f5b6ed3da4"
        mock_sha256.assert_called_once()  # check that it wasn't calculated again


def test_nested_object_not_copied() -> None:
    """Test that nested objects are not copied when the outer pydantic model is created."""
    sub_immutable = SampleImmutable(some_req_field="test")
    sample = SampleImmutable(some_req_field="test", sub_immutable=sub_immutable)

    assert sample.sub_immutable is sub_immutable
