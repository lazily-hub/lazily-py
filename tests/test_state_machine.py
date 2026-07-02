from lazily import StateMachine, slot


class TestStateMachine:
    """Test the StateMachine primitive."""

    def test_traffic_light_transitions(self) -> None:
        ctx: dict = {}

        def transition(s: str, e: str) -> str | None:
            if e == "advance":
                return {"Red": "Green", "Green": "Yellow", "Yellow": "Red"}.get(s)
            return None

        m = StateMachine(ctx, "Red", transition)
        assert m.state == "Red"

        assert m.send("advance") is True
        assert m.state == "Green"

        assert m.send("advance") is True
        assert m.state == "Yellow"

        assert m.send("advance") is True
        assert m.state == "Red"

    def test_guard_rejection(self) -> None:
        ctx: dict = {}
        m = StateMachine(
            ctx, "Locked", lambda s, e: "Unlocked" if e == "coin" else None
        )

        assert m.send("push") is False
        assert m.state == "Locked"

        assert m.send("coin") is True
        assert m.state == "Unlocked"

    def test_self_transition_noop(self) -> None:
        """A self-transition is accepted (True) but suppresses propagation."""
        ctx: dict = {}
        notified: list[tuple[str, str]] = []
        m = StateMachine(ctx, "Idle", lambda s, e: "Idle")
        m.on_transition(lambda old, new: notified.append((old, new)))

        assert m.send("tick") is True
        assert m.state == "Idle"
        assert notified == []

    def test_on_transition_fires(self) -> None:
        ctx: dict = {}
        transitions: list[tuple[str, str]] = []
        m = StateMachine(
            ctx,
            "A",
            lambda s, e: {"A": "B", "B": "C", "C": "A"}.get(s) if e == "go" else None,
        )
        m.on_transition(lambda old, new: transitions.append((old, new)))

        m.send("go")
        assert transitions == [("A", "B")]

        m.send("go")
        assert transitions == [("A", "B"), ("B", "C")]

    def test_on_transition_dispose(self) -> None:
        ctx: dict = {}
        fired: list[str] = []
        m = StateMachine(
            ctx, "A", lambda s, e: chr(ord(s) + 1) if e == "next" else None
        )
        dispose = m.on_transition(lambda old, new: fired.append(new))

        m.send("next")
        assert fired == ["B"]

        dispose()
        m.send("next")
        assert fired == ["B"]

    def test_reactive_invalidation(self) -> None:
        """A slot that reads machine state recomputes on transition."""
        ctx: dict = {}
        m = StateMachine(ctx, "Red", lambda s, e: "Green" if e == "go" else None)

        computed: list[int] = []

        @slot
        def state_len(ctx: dict) -> int:
            val = len(m.state)
            computed.append(val)
            return val

        assert state_len(ctx) == 3
        assert computed == [3]

        m.send("go")
        assert state_len(ctx) == 5
        assert computed == [3, 5]

    def test_on_transition_old_new_correct(self) -> None:
        """Handler receives the correct old and new states across multiple transitions."""
        ctx: dict = {}
        pairs: list[tuple[int, int]] = []
        m = StateMachine(ctx, 0, lambda s, e: (s + 1) % 3 if e == "inc" else None)
        m.on_transition(lambda old, new: pairs.append((old, new)))

        m.send("inc")
        m.send("inc")
        m.send("inc")

        assert pairs == [(0, 1), (1, 2), (2, 0)]
