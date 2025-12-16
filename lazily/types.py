from typing import Protocol, TypeVar


__all__ = ["LazilyCallable", "ResolveCallable"]

C_contra = TypeVar("C_contra", contravariant=True, bound=dict)
C_cov = TypeVar("C_cov", covariant=True, bound=dict)
T_cov = TypeVar("T_cov", covariant=True)

R_contra = TypeVar("R_contra", contravariant=True)  # <-- can be ANY input type


class LazilyCallable(Protocol[C_contra, T_cov]):
    __name__: str

    def __call__(self, ctx: C_contra) -> T_cov: ...


class ResolveCallable(Protocol[R_contra, C_cov]):
    __name__: str

    def __call__(self, ctx: R_contra) -> C_cov: ...
