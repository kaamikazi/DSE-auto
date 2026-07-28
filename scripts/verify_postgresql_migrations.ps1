$ErrorActionPreference = "Stop"
docker compose up -d db db_test redis
docker compose run --rm backend alembic upgrade head
docker compose run --rm --no-deps `
  -e APP_ENV=test `
  -e DATABASE_ROLE=postgres_verification `
  -e DATABASE_URL="postgresql+psycopg://dse_test:$env:POSTGRES_TEST_PASSWORD@db_test:5432/dse_autotrader_test" `
  backend alembic upgrade head
docker compose run --rm --no-deps `
  -e APP_ENV=test `
  -e DATABASE_ROLE=postgres_verification `
  -e DATABASE_URL="postgresql+psycopg://dse_test:$env:POSTGRES_TEST_PASSWORD@db_test:5432/dse_autotrader_test" `
  backend alembic downgrade 0007
docker compose run --rm --no-deps `
  -e APP_ENV=test `
  -e DATABASE_ROLE=postgres_verification `
  -e DATABASE_URL="postgresql+psycopg://dse_test:$env:POSTGRES_TEST_PASSWORD@db_test:5432/dse_autotrader_test" `
  backend alembic upgrade head
