# DevGuard AI

Agentic AI-powered DevSecOps platform. Submit a GitHub repo URL → AI agents analyze, secure, cost-estimate, and deploy to AWS.

## Team

| Member | Role | Primary | Secondary |
|--------|------|---------|-----------|
| **Nada** | Subgroup 1 Lead | CodeSec Agent, RAG Backend | Code review, sprint planning |
| **Karim** | Frontend Lead, Subgroup 1 | InfraCost Agent, Dashboard UI | UI/UX design |
| **Oussema** | Subgroup 2 Lead | DeployOps Agent, Backend Core | API design, DB schema |
| **Hbib** | DevOps Lead, Subgroup 2 | Orchestrator, Chat, CI/CD | Platform deployment |

## Tech Stack

| Layer | Technology |
|-------|------------|
| Frontend | React 19 + Tailwind CSS + Vite |
| Backend API | FastAPI 0.110+ (Python 3.12) |
| Orchestration | LangChain + LangGraph |
| LLM | GPT-4o API (OpenAI) |
| Task Queue | Celery + Redis |
| Database | PostgreSQL 16 |
| Vector Store | Qdrant |
| Security | Semgrep, GitLeaks, Trivy, Bandit |
| IaC | Terraform + Infracost |
| CI/CD | GitHub Actions |

## Quick Start

### 1. Clone & Setup

```bash
git clone https://github.com/NadaBhm/devguard-ai.git
cd devguard-ai
```

### 2. Environment Variables

```bash
cp .env.example .env
# Edit .env and add your OPENAI_API_KEY
```

### 3. Start Infrastructure (Docker)

```bash
cd infrastructure
docker-compose up -d
```

This starts: PostgreSQL, Redis, Qdrant.

### 4. Install Python Dependencies

```bash
python -m venv .venv

# Windows:
.venv\Scripts\activate

# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt
```

### 5. Run Backend

```bash
cd src/backend
uvicorn main:app --reload --port 8000
```

### 6. Run Frontend (separate terminal)

```bash
cd src/frontend
npm install
npm run dev
```

## Folder Structure

```
devguard-ai/
├── src/
│   ├── subgroup1/               # Nada + Karim
│   │   ├── codesec/             # Nada owns
│   │   │   ├── agent.py
│   │   │   ├── scanners/
│   │   │   └── tests/
│   │   ├── infracost/           # Karim owns ⭐
│   │   │   ├── agent.py
│   │   │   ├── templates/
│   │   │   └── tests/
│   │   └── rag/                 # Nada owns
│   │       ├── embeddings.py
│   │       └── tests/
│   │
│   ├── subgroup2/               # Oussema + Hbib
│   │   ├── deployops/           # Oussema owns
│   │   │   ├── agent.py
│   │   │   └── tests/
│   │   └── orchestrator/        # Hbib owns
│   │       ├── graph.py
│   │       └── tests/
│   │
│   ├── backend/                 # Shared API core (Oussema leads)
│   │   ├── main.py
│   │   ├── routes/
│   │   ├── models/
│   │   └── tests/
│   │
│   └── frontend/                # Shared React (Karim leads)
│       ├── src/
│       │   ├── components/
│       │   └── pages/
│       └── package.json
│
├── infrastructure/              # Docker, Terraform
│   ├── docker-compose.yml
│   └── terraform/
├── tests/                       # Integration & E2E tests
├── docs/
└── README.md

```

## Development Workflow

1. **Branch**: Create a feature branch from `main`

   ```bash
   git checkout -b feature/your-name-short-desc
   ```

2. **Code**: Work in your assigned folder
3. **Test**: Run `pytest` before pushing
4. **PR**: Open a Pull Request → requires 1 approval
5. **Merge**: Squash and merge after approval + CI pass

## Sprint 1 Goal (July 6–12)

Mock agents running end-to-end:

- CodeSec returns realistic JSON
- InfraCost accepts stack data
- DeployOps mocks Terraform apply
- Orchestrator wires them together
- Dashboard shows real-time progress

## API Contracts

Locked schemas are in `docs/api-contracts/`:

- `codesec-mock-schema.json` — CodeSec output format
- *(Karim: add InfraCost schema here)*
- *(Hbib: add Orchestrator input schema here)*

## Environment Variables

See `.env.example` for all required variables.

## License

Internal — Team Use Only





## alembic migration commands : 

# initial migration
alembic revision --autogenerate -m "Initial migration with User model"
alembic upgrade head

# runs migration 
alembic upgrade head 

# start backend server 
uvicorn src.backend.main:app --reload

# Generate a new migration with autogenerate (if you have models)
alembic revision --autogenerate -m "initial_migration"

# OR generate a blank migration
alembic revision -m "initial_migration"