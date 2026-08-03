import pytest

from api.nextfix import MAX_REPORT_LEN, NextfixValidationError, capture_nextfix


class _Issue:
    issue_id = "issue-local-1"
    status = "captured"


class _Store:
    def __init__(self):
        self.calls = []

    def capture_issue(self, **kwargs):
        self.calls.append(kwargs)
        return _Issue()


def test_capture_nextfix_records_only_user_confirmed_local_report():
    store = _Store()

    result = capture_nextfix(
        {
            "observed": "The response stopped before applying the requested change.",
            "expected": "The response should have explained the remaining blocker.",
            "session_id": "session-123",
            "message_index": 7,
        },
        store_factory=lambda: store,
    )

    assert result == {
        "issue_id": "issue-local-1",
        "status": "captured",
        "message": "Captured locally. Nothing was generated, synced, staged, or applied.",
    }
    assert store.calls == [
        {
            "observed": "The response stopped before applying the requested change.",
            "expected": "The response should have explained the remaining blocker.",
            "target_id": "fresh-verification",
            "surface": "interactive-webui",
            "local_message_ref": "webui:session-123:7",
        }
    ]


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"observed": "", "expected": "expected"}, "Observed problem is required"),
        ({"observed": "observed", "expected": ""}, "Expected behavior is required"),
        (
            {"observed": "x" * (MAX_REPORT_LEN + 1), "expected": "expected"},
            "Report fields must be <= 4096 characters",
        ),
    ],
)
def test_capture_nextfix_rejects_incomplete_or_oversized_reports(payload, message):
    with pytest.raises(NextfixValidationError, match=message):
        capture_nextfix(payload, store_factory=_Store)
