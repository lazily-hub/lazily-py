from dataclasses import dataclass

from lazily import Cell, cell, slot, slot_def


class TestCell:
    """Test the base cell class functionality."""

    def test_cell_value_with_subscriber(self) -> None:
        @dataclass
        class CustomCtxResolver:
            ctx: dict

        def resolve_ctx(resolver: CustomCtxResolver | dict) -> dict:
            return resolver.ctx if isinstance(resolver, CustomCtxResolver) else resolver

        @slot
        def slot_events(ctx: dict) -> list[str]:
            return []

        @slot
        def hello(ctx: dict) -> Cell[str]:
            slot_events(ctx).append("hello")
            return Cell(ctx, "Hello")

        @cell
        def name(ctx: dict) -> str:
            slot_events(ctx).append("name")
            return "World"

        @slot
        def greeting(ctx: dict) -> str:
            slot_events(ctx).append("greeting")
            return f"{hello(ctx).value} {name(ctx).value}!"

        @cell
        def response(ctx: dict) -> str:
            return "How are you?"

        @slot_def(resolve_ctx)
        def greeting_and_response(ctx: dict) -> str:
            slot_events(ctx).append("greeting_and_response")
            return f"{greeting(ctx)} {response(ctx).value}"

        ctx = {}
        custom_ctx_resolver = CustomCtxResolver(ctx)

        assert ctx.get(greeting) is None
        assert slot_events(ctx) == []
        assert greeting(ctx) == "Hello World!"
        assert slot_events(ctx) == ["greeting", "hello", "name"]
        assert greeting_and_response(ctx) == "Hello World! How are you?"
        assert slot_events(ctx) == [
            "greeting",
            "hello",
            "name",
            "greeting_and_response",
        ]
        name(ctx).value = "You"
        assert ctx.get(greeting) is None
        assert slot_events(ctx) == [
            "greeting",
            "hello",
            "name",
            "greeting_and_response",
        ]
        assert greeting_and_response(custom_ctx_resolver) == "Hello You! How are you?"
        assert slot_events(ctx) == [
            "greeting",
            "hello",
            "name",
            "greeting_and_response",
            "greeting_and_response",
            "greeting",
        ]

    def test_cell_get_and_set_with_subscriber(self) -> None:
        @dataclass
        class CustomCtxResolver:
            ctx: dict

        def resolve_ctx(resolver: CustomCtxResolver | dict) -> dict:
            return resolver.ctx if isinstance(resolver, CustomCtxResolver) else resolver

        @slot
        def slot_events(ctx: dict) -> list[str]:
            return []

        @slot
        def hello(ctx: dict) -> Cell[str]:
            slot_events(ctx).append("hello")
            return Cell(ctx, "Hello")

        @cell
        def name(ctx: dict) -> str:
            slot_events(ctx).append("name")
            return "World"

        @slot
        def greeting(ctx: dict) -> str:
            slot_events(ctx).append("greeting")
            return f"{hello(ctx).value} {name(ctx).value}!"

        @cell
        def response(ctx: dict) -> str:
            return "How are you?"

        @slot_def(resolve_ctx)
        def greeting_and_response(ctx: dict) -> str:
            slot_events(ctx).append("greeting_and_response")
            return f"{greeting(ctx)} {response(ctx).get()}"

        ctx = {}
        custom_ctx_resolver = CustomCtxResolver(ctx)

        assert ctx.get(greeting) is None
        assert slot_events(ctx) == []
        assert greeting(ctx) == "Hello World!"
        assert slot_events(ctx) == ["greeting", "hello", "name"]
        assert greeting_and_response(ctx) == "Hello World! How are you?"
        assert slot_events(ctx) == [
            "greeting",
            "hello",
            "name",
            "greeting_and_response",
        ]
        name(ctx).set("You")
        assert ctx.get(greeting) is None
        assert slot_events(ctx) == [
            "greeting",
            "hello",
            "name",
            "greeting_and_response",
        ]
        assert greeting_and_response(custom_ctx_resolver) == "Hello You! How are you?"
        assert slot_events(ctx) == [
            "greeting",
            "hello",
            "name",
            "greeting_and_response",
            "greeting_and_response",
            "greeting",
        ]

    def test_empty_cell(self) -> None:
        empty_cell = cell()

        @slot
        def cell_dependency(ctx: dict) -> str:
            return f"empty_cell={empty_cell(ctx).value}"

        ctx = {}
        assert empty_cell(ctx).value == None
        assert cell_dependency(ctx) == "empty_cell=None"
        empty_cell(ctx).value = "test"
        assert empty_cell(ctx).value == "test"
        assert cell_dependency(ctx) == "empty_cell=test"
