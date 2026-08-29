"""Coverage for app/password_hashing.py (roadmap package 19, issue #74)."""

from app.password_hashing import hash_password, verify_password


def test_hash_is_not_the_plaintext_password():
    stored = hash_password("correct horse battery staple")
    assert "correct horse battery staple" not in stored


def test_same_password_hashes_differently_each_time():
    """A fresh random salt per call -- two hashes of the same password must
    never be byte-identical (defence against a rainbow-table/identical-hash
    leak revealing two coaches share a password)."""
    first = hash_password("correct horse battery staple")
    second = hash_password("correct horse battery staple")
    assert first != second


def test_verify_password_accepts_the_correct_password():
    stored = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", stored) is True


def test_verify_password_rejects_the_wrong_password():
    stored = hash_password("correct horse battery staple")
    assert verify_password("wrong password", stored) is False


def test_verify_password_rejects_malformed_stored_value_instead_of_raising():
    assert verify_password("anything", "not-a-real-hash") is False
    assert verify_password("anything", "") is False


def test_verify_password_rejects_a_hash_from_a_different_algorithm_tag():
    stored = hash_password("correct horse battery staple")
    tampered = stored.replace("scrypt$", "md5$", 1)
    assert verify_password("correct horse battery staple", tampered) is False
