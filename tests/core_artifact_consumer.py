from lazily import Computed, Context, Source, TeardownScope, batch


raw: dict[object, object] = {}
context = Context(raw)
source = Source(raw, 1)
computed = Computed(raw, lambda read: read.read(source) * 2)
runs: list[int] = []
scope = TeardownScope(raw)
scope.effect(lambda read: runs.append(read.read(computed)))
assert runs == [2]


def writes() -> None:
    source.value = 2
    source.value = 3


batch(writes)
assert computed.value == 6
assert runs == [2, 6], "batched writes must coalesce to one effect rerun"

scope.close()
source.value = 4
assert runs == [2, 6], "a disposed scope must detach its effect"
assert source.ctx is raw and computed.ctx is raw and context.underlying is raw
print("lazily-py Core artifact consumer: ok")
