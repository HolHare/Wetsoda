.PHONY: setup up down restart logs build pull-model import-workflows export-workflows open

setup: .env up pull-model
	@echo ""
	@echo "Stack is running at http://localhost:5678"
	@echo "Login: $$(grep N8N_USER .env | cut -d= -f2) / $$(grep N8N_PASSWORD .env | cut -d= -f2)"

.env:
	cp .env.example .env
	@echo ".env created from .env.example — edit it before going to production!"

up:
	docker compose up -d --build

down:
	docker compose down

restart:
	docker compose restart n8n

logs:
	docker compose logs -f

build:
	docker compose build

# Download the default model into Ollama
pull-model:
	docker compose exec ollama ollama pull llama3.2

pull-mistral:
	docker compose exec ollama ollama pull mistral

# Import workflow JSON files into the running n8n instance
import-workflows:
	docker compose exec n8n n8n import:workflow --input=/import/workflows/

# Export all workflows from n8n back to ./workflows/
export-workflows:
	docker compose exec n8n n8n export:workflow --all --output=/tmp/wetsoda-export/
	docker cp $$(docker compose ps -q n8n):/tmp/wetsoda-export/. ./workflows/

open:
	xdg-open http://localhost:5678 2>/dev/null || open http://localhost:5678 2>/dev/null || echo "Open http://localhost:5678 in your browser"
