import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from lazily import Slot, slot


class TestSlot:
    """Test the base Slot class functionality."""

    def test_slot_abstract_nature(self):
        """Test that Slot cannot be instantiated directly without callable."""
        with pytest.raises(AttributeError):
            instance_slot = Slot()
            instance_slot({})

    def test_slot_with_manual_callable(self):
        """Test Slot with manually assigned callable."""
        instance_slot = Slot()
        instance_slot.callable = lambda ctx: "test value"

        ctx = {}
        instance = instance_slot(ctx)

        assert instance == "test value"
        assert instance_slot in ctx
        assert ctx[instance_slot] == "test value"

    def test_slot_get_method(self):
        """Test the get method."""
        instance_slot = Slot()
        instance_slot.callable = lambda ctx: "test value"

        ctx = {}

        # Should return None when not in context
        assert instance_slot.get(ctx) is None

        # Should return value after calling
        instance_slot(ctx)
        assert instance_slot.get(ctx) == "test value"

    def test_slot_is_in_method(self):
        """Test the is_in method."""
        instance_slot = Slot()
        instance_slot.callable = lambda ctx: "test value"

        ctx = {}

        # Should not be in context initially
        assert not instance_slot.is_in(ctx)

        # Should be in context after calling
        instance_slot(ctx)
        assert instance_slot.is_in(ctx)


class TestSlotClass:
    """Test the slot class functionality."""

    def test_simple_slot(self):
        """Test basic slot functionality."""
        hello = slot(lambda ctx: "Hello")

        ctx = {}
        result_hello = hello(ctx)

        assert result_hello == "Hello"
        assert hello in ctx
        assert ctx[hello] == "Hello"

        @slot
        def world(ctx: dict) -> str:
            return "World"

        result_world = world(ctx)
        assert result_world == "World"
        assert world in ctx
        assert ctx[world] == "World"

    def test_slot_caching(self):
        """Test that slot caches results."""
        call_count = 0

        @slot
        def counter(ctx: dict):
            nonlocal call_count
            call_count += 1
            return f"called {call_count} times"

        ctx = {}

        # First call
        result1 = counter(ctx)
        assert result1 == "called 1 times"
        assert call_count == 1

        # Second call should return cached value
        result2 = counter(ctx)
        assert result2 == "called 1 times"
        assert call_count == 1  # Should not increment

    def test_slot_dependency_chain(self):
        """Test slot objects depending on other slot objects."""

        first = slot(lambda ctx: "Hello")
        second = slot(lambda ctx: "World")
        combined = slot(lambda ctx: f"{first(ctx)} {second(ctx)}!")

        ctx = {}
        result = combined(ctx)

        assert result == "Hello World!"
        assert first in ctx
        assert second in ctx
        assert combined in ctx

    def test_multiple_contexts(self):
        """Test that different contexts are independent."""
        value = slot(lambda ctx: len(ctx))

        ctx1 = {}
        ctx2 = {"existing": "value"}

        result1 = value(ctx1)
        result2 = value(ctx2)

        assert result1 == 0
        assert result2 == 1

    def test_slot_with_complex_types(self):
        """Test slot with complex return types."""
        dict_slot = slot(lambda ctx: {"key": "value", "number": 42})
        list_slot = slot(lambda ctx: [1, 2, 3])

        ctx = {}

        dict_result = dict_slot(ctx)
        list_result = list_slot(ctx)

        assert dict_result == {"key": "value", "number": 42}
        assert list_result == [1, 2, 3]


class TestIntegration:
    """Integration tests combining different slot types."""

    def test_complex_dependency_graph(self):
        """Test a complex dependency graph."""

        @slot
        def config(ctx: dict) -> dict:
            return {"api_url": "https://api.example.com", "timeout": 30}

        class HttpClient(Slot[dict, dict, str]):
            def callable(self, ctx: dict) -> str:
                _config = config(ctx)
                return f"HttpClient({_config['api_url']}, timeout={_config['timeout']})"

        http_client = HttpClient()

        user_service_slot = slot(lambda ctx: f"UserService({http_client(ctx)})")
        auth_service_slot = slot(lambda ctx: f"AuthService({http_client(ctx)})")

        @slot
        def app(ctx: dict) -> str:
            return f"App(user={user_service_slot(ctx)}, auth={auth_service_slot(ctx)})"

        ctx = {}
        result = app(ctx)

        expected = "App(user=UserService(HttpClient(https://api.example.com, timeout=30)), auth=AuthService(HttpClient(https://api.example.com, timeout=30)))"
        assert result == expected


class TestEdgeCases:
    """Test edge cases and error conditions."""

    def test_empty_context(self):
        """Test behavior with empty context."""
        simple_slot = slot(lambda ctx: "value")

        ctx = {}
        result = simple_slot(ctx)

        assert result == "value"
        assert len(ctx) == 1

    def test_context_mutation(self):
        """Test that slot objects can read from context mutations."""
        reader_slot = slot(lambda ctx: ctx.get("dynamic_value", "not_found"))

        ctx = {}

        # First call - value not in context
        result1 = reader_slot(ctx)
        assert result1 == "not_found"

        # Add value to context
        ctx["dynamic_value"] = "found"

        # Create new slot that reads the same key
        reader_slot2 = slot(lambda ctx: ctx.get("dynamic_value", "not_found"))
        result2 = reader_slot2(ctx)
        assert result2 == "found"

        # Original reader_slot should still return cached value
        result3 = reader_slot(ctx)
        assert result3 == "not_found"  # Cached result

    def test_none_values(self):
        """Test handling of None values."""
        none_slot = slot(lambda ctx: None)

        ctx = {}
        result = none_slot(ctx)

        assert result is None
        assert none_slot.is_in(ctx)
        assert none_slot.get(ctx) is None

    def test_exception_in_callable(self):
        """Test behavior when callable raises an exception."""
        error_slot = slot(lambda ctx: 1 / 0)  # Division by zero

        ctx = {}

        with pytest.raises(ZeroDivisionError):
            error_slot(ctx)

        # Should not slot cached after exception
        assert not error_slot.is_in(ctx)

    def test_multiple_context_types(self) -> None:
        class FooCtx(dict): ...

        class BarCtx(dict): ...

        @slot
        def foo(ctx: FooCtx) -> int:
            return 1

        foo_ctx = FooCtx()
        foo(foo_ctx)

        """
        The following has an incompatible type mypy error.
        :see: TestEdgeCases.test_mypy_rejects_barctx_for_fooctx
        """
        # bar_ctx = BarCtx()
        # foo(bar_ctx)

    def test_mypy_rejects_barctx_for_fooctx(self) -> None:
        # Write a small module that should FAIL mypy.
        p = Path(tempfile.gettempdir()) / "negative_case.py"
        p.write_text(
            """
from lazily import slot

class FooCtx(dict): ...
class BarCtx(dict): ...

@slot
def foo(ctx: FooCtx) -> int:
    return 1

bar_ctx = BarCtx()
foo(bar_ctx)
            """.lstrip()
        )

        # Run mypy against that file
        proc = subprocess.run(
            [sys.executable, "-m", "mypy", "--explicit-package-bases", str(p)],
            capture_output=True,
            text=True,
        )

        assert proc.returncode != 0, proc.stdout + proc.stderr
        # Optional: assert a specific diagnostic shows up
        assert "incompatible type" in (proc.stdout + proc.stderr).lower()
