from ledgersync.errors import FileTooLarge, ModelError


def test_errors_carry_status_code_and_message():
    assert FileTooLarge("too big").to_dict() == {"code": "file_too_large", "message": "too big", "status_code": 413}


def test_code_can_be_specialised_per_instance():
    err = ModelError("cut off", code="ai_output_truncated")
    assert (err.code, err.status_code, ModelError.code) == ("ai_output_truncated", 502, "ai_error")
