from dataclasses import dataclass

from lazily import cell_def, slot_def


class TestDef:
    def test_def(self) -> None:
        @dataclass
        class CustomCtxResolver:
            ctx: dict

        def resolve_ctx(resolver: CustomCtxResolver | dict) -> dict:
            return resolver.ctx if isinstance(resolver, CustomCtxResolver) else resolver

        @slot_def(resolve_ctx)
        def slot_events(ctx: dict) -> list[str]:
            return []

        @cell_def(resolve_ctx)
        def hello(ctx: dict) -> str:
            slot_events(ctx).append("hello")
            return "Hello"

        @cell_def(resolve_ctx)
        def name(ctx: dict) -> str:
            slot_events(ctx).append("name")
            return "World"

        @slot_def(resolve_ctx)
        def greeting(ctx: dict) -> str:
            slot_events(ctx).append("greeting")
            return f"{hello(ctx).value} {name(ctx).value}!"

        @cell_def(resolve_ctx)
        def response(ctx: dict) -> str:
            return "How are you?"

        @slot_def(resolve_ctx)
        def greeting_and_response(ctx: dict) -> str:
            slot_events(ctx).append("greeting_and_response")
            return f"{greeting(ctx)} {response(ctx).value}"

        ctx: dict[object, object] = {}
        custom_ctx_resolver = CustomCtxResolver(ctx)

        assert ctx.get(greeting) is None
        assert slot_events(ctx) == []
        assert greeting(custom_ctx_resolver) == "Hello World!"
        assert slot_events(ctx) == ["greeting", "hello", "name"]
        assert greeting_and_response(custom_ctx_resolver) == "Hello World! How are you?"
        assert slot_events(ctx) == ["greeting", "hello", "name", "greeting_and_response"]
        name(custom_ctx_resolver).value = "You"
        assert ctx.get(greeting) is None
        assert slot_events(ctx) == ["greeting", "hello", "name", "greeting_and_response"]
        assert greeting_and_response(custom_ctx_resolver) == "Hello You! How are you?"
        assert slot_events(ctx) == [
            "greeting",
            "hello",
            "name",
            "greeting_and_response",
            "greeting_and_response",
            "greeting",
        ]

