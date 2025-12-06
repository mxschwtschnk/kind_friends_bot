import sys
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).resolve().parents[1]))

from kind_friends.config import SettingsLoaderError, _parse_id_list


def test_parse_id_list_allows_empty_quotes():
    assert _parse_id_list('""', "ALPHA_USERS") == []
    assert _parse_id_list("''", "ALPHA_USERS") == []


def test_parse_id_list_trims_whitespace_and_quotes():
    assert _parse_id_list(' 123, "456" , \'789\' ', "ALPHA_USERS") == [123, 456, 789]


def test_parse_id_list_supports_json_like_array_strings():
    assert _parse_id_list("[\"111\", '222', 333]", "ALPHA_USERS") == [111, 222, 333]
    assert _parse_id_list('["\""]', "ALPHA_USERS") == []


def test_parse_id_list_ignores_empty_quoted_space_entries():
    assert _parse_id_list('[" ", "123"]', "ALPHA_USERS") == [123]


def test_parse_id_list_invalid_value_raises_error():
    with pytest.raises(SettingsLoaderError):
        _parse_id_list("not-a-number", "ALPHA_USERS")
