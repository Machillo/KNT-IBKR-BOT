import asyncio

from execution.paper import PaperExecutionRequest, PaperExecutionResult
from run_knt_signal_paper_once import OneShotCappedPaperExecutor


class _FakeExecutor:
    def __init__(self):
        self.requests = []

    async def submit(self, contract, request):
        self.requests.append(request)
        return PaperExecutionResult(True, "bracket_confirmed", 123)


def _request(quantity=25):
    return PaperExecutionRequest(
        symbol="TEST",
        strategy="test_strategy",
        side="LONG",
        quantity=quantity,
        entry_price=100.0,
        stop_price=95.0,
        target_price=110.0,
        regime="TRENDING",
    )


def test_one_shot_executor_caps_quantity_and_submits_only_once():
    fake = _FakeExecutor()
    one_shot = OneShotCappedPaperExecutor(fake, max_quantity=1)

    first = asyncio.run(one_shot.submit(object(), _request(25)))
    second = asyncio.run(one_shot.submit(object(), _request(25)))

    assert first.submitted is True
    assert fake.requests[0].quantity == 1
    assert one_shot.submitted == 1
    assert second.submitted is False
    assert second.reason == "one_shot_submission_limit_reached"
    assert len(fake.requests) == 1


def test_one_shot_executor_rejects_invalid_cap():
    try:
        OneShotCappedPaperExecutor(_FakeExecutor(), max_quantity=0)
    except ValueError as exc:
        assert "max_quantity" in str(exc)
    else:
        raise AssertionError("expected ValueError")
