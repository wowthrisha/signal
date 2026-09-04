.PHONY: up down shell-db evaluate probe

up:
	docker compose up --build

down:
	docker compose down -v

shell-db:
	docker compose exec db psql -U signal signal

evaluate:
	@echo "TODO: make evaluate (B0/B1/B2)"

probe:
	python scripts/probe_bhavcopy.py
