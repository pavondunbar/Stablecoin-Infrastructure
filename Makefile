.DEFAULT_GOAL := help
COMPOSE       := docker compose
PG_CONN       := postgresql+psycopg2://stablecoin:s3cr3t@localhost:5432/stablecoin_db
BOLD  := \033[1m
RESET := \033[0m
CYAN  := \033[36m
GREEN := \033[32m
RED   := \033[31m

.PHONY: help up down build restart logs ps shell-pg shell-kafka \
        test test-unit test-e2e lint health demo integrity kafka-tail \
        topics seed-accounts db-balances db-ledger db-rtgs db-fx open-docs

help: ## Show this help
	@echo ""
	@echo "$(BOLD)Stablecoin & Digital Cash Infrastructure$(RESET)"
	@echo ""
	@awk 'BEGIN {FS = ":.*##"} /^[a-zA-Z_-]+:.*##/ { \
		printf "  $(CYAN)%-22s$(RESET) %s\n", $$1, $$2 }' $(MAKEFILE_LIST)
	@echo ""

up: ## Build and start all services
	$(COMPOSE) up --build -d
	@echo "$(GREEN)Stack started — run 'make health' to verify.$(RESET)"

down: ## Stop and remove containers (keep volumes)
	$(COMPOSE) down

down-v: ## Stop and remove containers AND volumes
	$(COMPOSE) down -v

build: ## Rebuild all images without starting
	$(COMPOSE) build --no-cache

restart: ## Restart all services
	$(COMPOSE) restart

logs: ## Follow all service logs
	$(COMPOSE) logs -f

logs-svc: ## Follow one service: make logs-svc SVC=rtgs
	$(COMPOSE) logs -f $(SVC)

ps: ## Container status
	$(COMPOSE) ps

shell-pg: ## psql shell
	$(COMPOSE) exec postgres psql -U stablecoin -d stablecoin_db

shell-kafka: ## kafka bash shell
	$(COMPOSE) exec kafka bash

test: ## Full test suite
	PYTHONPATH=. pytest tests/ -v --tb=short -p no:warnings

test-unit: ## Unit tests only
	PYTHONPATH=. pytest tests/test_token_issuance.py tests/test_rtgs.py \
	  tests/test_payment_engine.py tests/test_fx_settlement.py \
	  tests/test_compliance.py -v --tb=short

test-e2e: ## End-to-end scenario tests
	PYTHONPATH=. pytest tests/test_e2e_scenarios.py -v --tb=short

health: ## Check gateway health
	@curl -sf http://localhost:8000/health | python3 -m json.tool || \
	  echo "$(RED)Gateway not reachable$(RESET)"

demo: ## Run live stack demo
	GATEWAY_URL=http://localhost:8000 \
	GATEWAY_API_KEY=$$(grep GATEWAY_API_KEY .env | cut -d= -f2) \
	python3 scripts/demo.py

integrity: ## Ledger double-entry integrity check
	DATABASE_URL=$(PG_CONN) python3 scripts/ledger_integrity.py

kafka-tail: ## Tail all Kafka topics
	KAFKA_BOOTSTRAP=localhost:9092 python3 scripts/kafka_tail.py

topics: ## List Kafka topics
	$(COMPOSE) exec kafka kafka-topics --bootstrap-server localhost:9092 --list

seed-accounts: ## Insert KYC-cleared demo accounts
	$(COMPOSE) exec postgres psql -U stablecoin -d stablecoin_db -c \
	  "INSERT INTO accounts (entity_name,account_type,kyc_verified,aml_cleared,risk_tier) \
	   VALUES ('Demo Bank A','institutional',true,true,1), \
	          ('Demo Bank B','correspondent',true,true,2) \
	   ON CONFLICT DO NOTHING;"
	@echo "$(GREEN)Demo accounts seeded.$(RESET)"

db-balances: ## Show all token balances
	$(COMPOSE) exec postgres psql -U stablecoin -d stablecoin_db -c \
	  "SELECT a.entity_name, b.currency, b.balance, b.reserved, b.balance-b.reserved AS available \
	   FROM token_balances b JOIN accounts a ON a.id=b.account_id ORDER BY a.entity_name,b.currency;"

db-ledger: ## Show recent ledger entries
	$(COMPOSE) exec postgres psql -U stablecoin -d stablecoin_db -c \
	  "SELECT txn_ref,entry_type,currency,amount,balance_after,LEFT(narrative,40),created_at \
	   FROM ledger_entries ORDER BY created_at DESC LIMIT 20;"

db-rtgs: ## Show recent RTGS settlements
	$(COMPOSE) exec postgres psql -U stablecoin -d stablecoin_db -c \
	  "SELECT settlement_ref,currency,amount,priority,status,queued_at,settled_at \
	   FROM rtgs_settlements ORDER BY queued_at DESC LIMIT 20;"

db-fx: ## Show recent FX settlements
	$(COMPOSE) exec postgres psql -U stablecoin -d stablecoin_db -c \
	  "SELECT settlement_ref,sell_currency,sell_amount,buy_currency,buy_amount,\
	          applied_rate,rails,status,LEFT(blockchain_tx_hash,20) \
	   FROM fx_settlements ORDER BY created_at DESC LIMIT 10;"

open-docs: ## Open API docs in browser
	open http://localhost:8000/docs 2>/dev/null || xdg-open http://localhost:8000/docs 2>/dev/null || \
	  echo "Visit http://localhost:8000/docs"
