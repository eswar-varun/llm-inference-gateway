from __future__ import annotations

from dataclasses import dataclass

from .config import GatewayConfig
from .errors import ErrorKind, GatewayError
from .providers import Provider


@dataclass(frozen=True)
class Target:
    provider: Provider
    model: str


class Router:
    """Resolves a client-facing model alias to an ordered list of upstream targets.

    Targets are in preference order. Milestone 1 always uses the first; failover across the
    rest arrives with the resilience work in milestone 2.
    """

    def __init__(self, routes: dict[str, tuple[Target, ...]]) -> None:
        self._routes = routes

    @classmethod
    def from_config(cls, config: GatewayConfig, providers: dict[str, Provider]) -> Router:
        return cls({
            route.alias: tuple(Target(providers[t.provider], t.model) for t in route.targets)
            for route in config.models
        })

    def aliases(self) -> list[str]:
        return list(self._routes)

    def targets(self, alias: str) -> tuple[Target, ...]:
        try:
            return self._routes[alias]
        except KeyError:
            raise GatewayError(ErrorKind.MODEL_NOT_FOUND,
                               f"The model '{alias}' does not exist") from None
