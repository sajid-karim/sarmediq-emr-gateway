from app.core.drift import diff_shapes, shape_hash, shape_of


def test_shape_records_paths_and_types():
    shape = shape_of([{"id": "1", "name": {"family": "A"}, "tags": [{"k": 1}]}])
    assert shape == {
        "id": ["string"],
        "name": ["object"],
        "name.family": ["string"],
        "tags": ["array"],
        "tags[]": ["object"],
        "tags[].k": ["number"],
    }


def test_null_adds_presence_but_no_type():
    assert shape_of([{"middle": None}]) == {"middle": []}
    assert shape_of([{"middle": None}, {"middle": "Ann"}]) == {"middle": ["string"]}


def test_array_indexes_do_not_change_the_shape():
    short = shape_of([{"ids": [{"v": "a"}]}])
    long = shape_of([{"ids": [{"v": "a"}, {"v": "b"}, {"v": "c"}]}])
    assert shape_hash(short) == shape_hash(long)


def test_key_order_does_not_change_the_shape():
    assert shape_of([{"a": 1, "b": "x"}]) == shape_of([{"b": "x", "a": 1}])


def test_non_objects_are_ignored():
    assert shape_of([None, "x", {"a": 1}]) == {"a": ["number"]}


def test_diff_added_removed_changed():
    baseline = shape_of([{"dob": "1985-01-01", "mrn": "M1", "age": 3}])
    current = shape_of([{"birth_date": "1985-01-01", "mrn": "M1", "age": "3"}])
    diff = diff_shapes(baseline, current)
    assert diff.added == ("birth_date",)
    assert diff.removed == ("dob",)
    assert diff.changed == {"age": {"from": ["number"], "to": ["string"]}}


def test_null_only_page_is_not_a_change():
    baseline = shape_of([{"middle": "Ann"}])
    assert diff_shapes(baseline, shape_of([{"middle": None}])).is_empty


def test_same_change_same_identity():
    baseline = shape_of([{"dob": "x"}])
    a = diff_shapes(baseline, shape_of([{"birth_date": "x"}]))
    b = diff_shapes(baseline, shape_of([{"birth_date": "y"}, {"birth_date": None}]))
    assert a.hash() == b.hash()
