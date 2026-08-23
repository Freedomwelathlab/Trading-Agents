from fastapi import Request

from apps.api.app.execution.registry import PaperBrokerRegistry


def get_paper_broker_registry(request: Request) -> PaperBrokerRegistry:
    return request.app.state.paper_broker_registry
