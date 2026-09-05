.PHONY: up down shell-db migrate ingest evaluate replay detect calibrate test probe

up:
	docker compose up --build

down:
	docker compose down -v

shell-db:
	docker compose exec db psql -U signal signal

evaluate:
	cd backend && python -m app.benchmark

replay:
	cd backend && python -m app.evaluate --config ../configs/bench.yaml

probe:
	python scripts/probe_bhavcopy.py

migrate:
	cd backend && python -m app.db

ingest:
	cd backend && python -m app.ingest --what all --from 2026-02-27 --to 2026-09-03

detect:
	cd backend && python -m app.detect --date 2026-09-03 --report-zscore --assert-sane

calibrate:
	cd backend && python -m app.calibrate

test:
	cd backend && python -m pytest tests/ -q
