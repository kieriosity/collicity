import time
from uuid import RFC_4122

from collicity_core.ids import new_id


def test_new_id_is_rfc9562_version_7():
    value = new_id()
    assert value.version == 7
    assert value.variant == RFC_4122


def test_new_id_embeds_the_current_unix_millisecond():
    before = time.time_ns() // 1_000_000
    value = new_id()
    after = time.time_ns() // 1_000_000
    assert before <= value.int >> 80 <= after


def test_new_ids_are_unique_and_ordered_across_milliseconds():
    first = new_id()
    time.sleep(0.002)
    second = new_id()
    assert first != second
    assert first < second
    assert len({new_id() for _ in range(10_000)}) == 10_000
