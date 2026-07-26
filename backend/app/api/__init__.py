from app.api.campaign_routes import router as campaign_router
from app.api.evidence_workspace_routes import router as evidence_workspace_router
from app.api.infrastructure_routes import router as infrastructure_router
from app.api.governed_research_routes import router as governed_research_router
from app.api.routes import router

router.include_router(campaign_router)
router.include_router(infrastructure_router)
router.include_router(evidence_workspace_router)
router.include_router(governed_research_router)

__all__ = ["router"]
