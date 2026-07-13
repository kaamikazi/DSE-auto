$ErrorActionPreference = "Stop"
$campaign = "m7-distributed-30-day"
docker compose up -d db redis backend worker scheduler
docker compose exec -T backend python -m app.simulation_process `
  --campaign $campaign --start-day 1 --end-day 15 --require-distributed
docker compose restart worker scheduler db redis
docker compose exec -T backend python -m app.simulation_process `
  --campaign $campaign --start-day 16 --end-day 30 --require-distributed
docker compose exec -T backend python -c `
  "from app.core.database import SessionLocal; from app.services.task_queue import create_broker,requeue_ready_tasks; db=SessionLocal(); print({'requeued':requeue_ready_tasks(db,create_broker())}); db.close()"
