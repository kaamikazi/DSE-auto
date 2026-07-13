from app.api.campaign_routes import router as campaign_router
from app.api.routes import router

router.include_router(campaign_router)

__all__ = ["router"]
