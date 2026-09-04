.PHONY: up down shell-db evaluate probe

up:
	docker compose up --build

down:
	docker compose down -v

shell-db:
	docker compose exec db psql -U signal signal

evaluate:
	cd backend && python -m app.evaluate --config ../configs/bench.yaml

probe:
	python scripts/probe_bhavcopy.py
