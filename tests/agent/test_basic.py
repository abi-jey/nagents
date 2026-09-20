"""Basic tests for nagents package - placeholder."""


class TestPlaceholder:
    """Placeholder tests - using examples for testing instead."""

    def test_import(self) -> None:
        """Test that package imports work."""
        from nagents import Agent
        from nagents import Provider
        from nagents import ProviderType

        assert Agent is not None
        assert Provider is not None
        assert ProviderType is not None
