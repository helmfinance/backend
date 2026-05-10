from fastapi import Response


def cache_for(seconds: int):
    """FastAPI dependency. Sets Cache-Control header on the response.

    Usage:
        @app.get("/foo", dependencies=[Depends(cache_for(300))])
    """
    def dep(response: Response) -> None:
        response.headers["Cache-Control"] = f"public, max-age={seconds}"
    return dep
