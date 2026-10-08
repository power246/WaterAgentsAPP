from fastapi import APIRouter, Request, Response

router = APIRouter()


@router.get("/health")
async def health(request: Request, response: Response) -> dict[str, str]:
    response.headers["X-Frontend-Available"] = str(request.app.state.frontend_responder.available).lower()
    return {"status": "healthy"}
