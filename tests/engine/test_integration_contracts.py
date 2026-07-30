"""Public integration-contract tests."""

from librae.integrations import (
    BalanceReader,
    BarDataFetcher,
    BrokerBalance,
    BrokerOrderReport,
    BrokerPosition,
    ExecutionReport,
    LiveStateStore,
    Notifier,
    OrderAdapter,
    OrderRequest,
    OrderSignal,
    PositionRequest,
)


def test_public_integration_contracts_are_importable() -> None:
    assert OrderAdapter is not None
    assert BalanceReader is not None
    assert BarDataFetcher is not None
    assert Notifier is not None
    assert LiveStateStore is not None
    assert OrderRequest is not None
    assert PositionRequest is not None
    assert ExecutionReport is not None
    assert OrderSignal is not None
    assert BrokerOrderReport is not None
    assert BrokerPosition is not None
    assert BrokerBalance is not None


def test_order_adapter_contract_includes_position_reconciliation() -> None:
    assert callable(getattr(OrderAdapter, "get_position", None))
