# claude-steward — Briefing para Planificación

## Qué es esto

Un MCP server que actúa como **capa de orquestación** entre Claude Code (orchestrador) y cualquier combinación de LLMs locales, LLMs remotas, agentes de coding y comandos del sistema — locales o en hosts remotos.

El objetivo es que Claude pueda delegar trabajo pesado, mecánico o de larga duración sin bloquear su contexto, sin esperar resultados síncronos, y sin que outputs grandes pasen por su ventana de contexto.

---

## Contexto del usuario

- Usa **Claude Code con plan Pro** (suscripción plana, no paga por token)
- Puede tener suscripciones a otros agentes: OpenCode, Codex, etc.
- Tiene o puede tener una **GPU local** y/o acceso a un **host remoto con GPU potente**
- Quiere empezar simple y que el sistema crezca solo, auto-configurándose en runtime

---

## Decisiones de diseño ya tomadas

### 1. Arquitectura en capas

```
Claude Code (orchestrador, Opus/Sonnet)
     ↓ MCP protocol
MCP Orchestrator Server  ←→  config.yaml (vivo, recarga sin reinicio)
     │
     ├── LLM Layer
     │     ├── Ollama / LM Studio / llama.cpp  (local)
     │     ├── Ollama en host remoto           (via SSH tunnel o Tailscale)
     │     └── APIs externas directas          (Anthropic, OpenAI, Groq, OpenRouter...)
     │           sin proxy intermedio (sin LiteLLM obligatorio)
     │
     ├── Execution Layer
     │     ├── subprocess local
     │     └── SSH remoto (paramiko)  — opcional, no obligatorio
     │
     ├── Agent Layer
     │     ├── Aider        — edición de ficheros con diffs (no ejecuta)
     │     ├── OpenCode     — agente coding CLI con suscripción propia
     │     ├── Codex CLI    — idem
     │     ├── Smolagents   — ejecución de comandos + retry lógico + summarización
     │     └── cualquier CLI con --message o similar
     │
     └── Queue Layer (Task Spooler — ts)
           ├── cola serie GPU         (slots=1, para LLM local, no saturar VRAM)
           ├── cola paralela          (slots=N, comandos independientes)
           ├── cola coding            (slots=1-2, agentes coding)
           └── mismas colas en remoto via SSH
```

### 2. Config viva (auto-configuración en runtime)

- `config.yaml` es la fuente de verdad: providers, agents, routing rules, queues
- El MCP server **recarga el YAML en cada tool call** — sin reinicio al añadir providers
- Claude puede añadir/modificar/eliminar entradas via herramientas MCP
- Las **API keys nunca van en el YAML** — solo el nombre de la variable de entorno
- Los secrets viven en `~/.config/mcp-orchestrator/.env` o en el keyring del sistema

### 3. Gestión de secrets

```
config.yaml:   api_key_env: "GROQ_API_KEY"    ← solo el nombre
.env file:     GROQ_API_KEY=gsk_xxx            ← el valor real, nunca en git
```

Opción alternativa: keyring del sistema (más seguro si la máquina es compartida).

### 4. Task Spooler para background tasks

- `ts` (task-spooler): binario C sin dependencias, demonio por usuario
- Tareas sobreviven al cierre de Claude — siguen corriendo independientemente
- Múltiples colas con distintos niveles de paralelismo via `TS_SOCKET`
- Timeout nativo via `timeout <segundos> <comando>` wrapeado en el submit
- Output guardado en `/tmp/ts-*` hasta que se consulta o se limpia

### 5. Qué herramienta para qué tarea

| Necesidad | Herramienta elegida | Motivo |
|---|---|---|
| Editar/crear ficheros de código | Aider (`--editor-model`) | SEARCH/REPLACE diffs, fiable con modelos débiles |
| Ejecutar agente coding con suscripción | OpenCode/Codex CLI | Ya tienen modelo propio, Claude solo orquesta |
| Ejecutar comandos y resumir output grande | Smolagents o llamada directa a LLM API | Output no pasa por Claude |
| Ejecutar comandos sin summarización | subprocess directo | Sin overhead |
| Retry lógico si falla un comando | Smolagents | Loop de razonamiento sin framework pesado |
| Tareas en background largas | Task Spooler (ts) | Sobrevive al cierre, colas independientes |
| LLM local | Ollama (directo, sin LiteLLM) | Menos capas |
| LLM remota API | openai / anthropic Python client directo | Sin proxy |

### 6. Enrutado inteligente

Claude define reglas de routing con tags:

```yaml
routing:
  rules:
    - match: {tags: [coding, architecture]}
      agent: opencode_smart        # caro/pesado
      fallback: opencode_cheap     # si falla
      priority: 10

    - match: {tags: [coding]}
      agent: opencode_cheap
      fallback: aider_local        # gratis, local
      priority: 5

  priorities:
    coding:   [aider_local, opencode_cheap, opencode_smart]
    analysis: [groq_fast, local_ollama]
    images:   [openai_images]
```

Cada agente tiene `cost_tier` (0=free/local, 1=cheap, 2=normal, 3=expensive) para que Claude tome decisiones de coste.

---

## Herramientas MCP que exponemos a Claude

### Ejecución

| Tool | Descripción |
|---|---|
| `run_command(command, exec_host)` | Ejecuta comando, devuelve output crudo |
| `run_and_summarize(command, question, exec_host, llm_provider)` | Ejecuta y resume — output NO pasa por Claude |
| `delegate_to_agent(agent, prompt, files, exec_host)` | Llama a Aider/OpenCode/Codex directamente |
| `smart_submit(prompt, tags, background, exec_host)` | Enruta automáticamente según reglas |

### Queue / Background

| Tool | Descripción |
|---|---|
| `submit_task(command, queue, exec_host, label, timeout)` | Encola tarea, retorna job_id inmediatamente |
| `list_tasks(queue, exec_host)` | Lista estado de todas las tareas |
| `get_task_output(job_id, queue, exec_host, summarize, llm_provider)` | Recoge output, opcionalmente resumido |
| `get_task_status(job_id, queue, exec_host)` | Solo el estado: queued/running/finished/failed |
| `cancel_task(job_id, queue, exec_host)` | Cancela o mata tarea |
| `set_queue_slots(queue, slots, exec_host)` | Cambia paralelismo en caliente |

### Auto-configuración

| Tool | Descripción |
|---|---|
| `list_capabilities()` | Muestra providers, agents, rules y queues disponibles |
| `add_provider(name, type, model, tags, ...)` | Añade LLM provider (recarga inmediata) |
| `add_agent(name, command, tags, fallback, ...)` | Añade agente CLI |
| `add_routing_rule(match_tags, agent, fallback, priority)` | Define regla de enrutado |
| `set_priority_order(task_type, ordered_agents)` | Reordena preferencias |
| `store_api_key(env_var, description)` | Guía al usuario para guardar key (Claude nunca ve el valor) |
| `remove_entry(entry_type, name)` | Elimina provider, agente o regla |

---

## Dependencias Python (minimalista)

```
mcp              — SDK MCP
paramiko         — SSH remoto (opcional si no hay hosts remotos)
pyyaml           — config
python-dotenv    — carga .env
ollama           — cliente Ollama local (opcional)
openai           — APIs OpenAI-compatible (Groq, OpenRouter, etc.)
anthropic        — API Anthropic directa
smolagents       — retry lógico y ejecución de comandos con LLM
# task-spooler  — binario del sistema, no pip
```

Sin Redis, sin Celery, sin LangChain, sin LiteLLM (a menos que se quiera como opción).

---

## Estado inicial mínimo (config.yaml de arranque)

```yaml
providers: {}   # Claude añade según necesite

agents:
  aider_local:
    type: cli
    command: "aider --yes --no-auto-commits"
    tags: [coding, local, free]
    cost_tier: 0

routing:
  rules: []
  priorities:
    coding: [aider_local]

queues:
  local:
    gpu:      {socket: "/tmp/ts-gpu.sock",      slots: 1}
    parallel: {socket: "/tmp/ts-parallel.sock", slots: 4}
    coding:   {socket: "/tmp/ts-coding.sock",   slots: 1}
    default:  {socket: "/tmp/ts-default.sock",  slots: 1}
```

---

## Requisitos originales y cómo se cubren

| Requisito | Cómo |
|---|---|
| 1. Delegar tareas de desarrollo (arquitectura → trozos pequeños) | Claude diseña, `delegate_to_agent` o `smart_submit` ejecuta |
| 2. Supervisar trabajo sin polling constante | `get_task_status` + `get_task_output` cuando Claude quiera |
| 3. Delegar ejecución de comandos/scripts, recoger salida | `run_command` o `run_and_summarize` (output no pasa por Claude) |
| 4. Delegación en background, recoger después, timeout | `submit_task` → job_id → consultar más tarde; `timeout` wrapeado |
| 5. Monitorizar qué hace la otra LLM | `list_tasks` + `get_task_output` parcial mientras corre |
| 6. Claude corrige skills/scripts/prompts | `cancel_task` + `submit_task` con prompt corregido; o `smart_submit` con nuevo contexto |

---

## Decisiones adicionales

### 7. Self-restart del MCP server

El servidor puede reiniciarse a sí mismo cuando se instalan nuevas dependencias:

```python
@mcp.tool()
async def install_and_restart(package: str) -> str:
    subprocess.run([sys.executable, "-m", "pip", "install", package])
    os.execv(sys.executable, [sys.executable] + sys.argv)
```

`os.execv` reemplaza el proceso actual — Claude Code reconecta automáticamente
(stdio transport). Flujo normal: `install_dependency` → `restart_server` → continúa.

### 8. Hosts SSH — claves en ~/.ssh del usuario

El config solo guarda `user@host`. Las claves las gestiona el usuario
en `~/.ssh/config` y `~/.ssh/known_hosts` de forma estándar.
Paramiko las respeta automáticamente sin configuración extra en el MCP.

```yaml
hosts:
  local:
    type: local
  gpu_server:
    type: ssh
    user: ubuntu
    host: 192.168.1.100        # o IP Tailscale (100.x.x.x)
  dev_box:
    type: ssh
    user: myuser
    host: dev.example.com
```

Si se usa Tailscale, el MCP no necesita saber nada — la IP es alcanzable
directamente. Para Ollama sobre Tailscale ni siquiera hace falta SSH:
`base_url: "http://100.64.0.5:11434"` funciona directo.

### 9. exec_host independiente por entidad

Cada provider, agente y cola declara su propio host. Permiten combinaciones
como: LLM en `gpu_server`, agente coding en `dev_box`, comandos en `local`:

```yaml
providers:
  ollama_local:
    type: ollama
    base_url: "http://localhost:11434"
    exec_host: local

  ollama_gpu:
    type: ollama
    base_url: "http://100.64.0.5:11434"  # Tailscale directo
    exec_host: local                      # la petición HTTP sale desde local

agents:
  opencode_devbox:
    type: cli
    command: "opencode"
    exec_host: dev_box                    # se ejecuta en el host de desarrollo

queues:
  gpu_server:
    gpu: {socket: "/tmp/ts-gpu.sock", slots: 1}
  dev_box:
    coding: {socket: "/tmp/ts-coding.sock", slots: 2}
```

### 13. CLI knowledge lookup antes de configurar agentes

Cuando el usuario pide añadir un agente, Claude NO debe pedir al usuario
la sintaxis — debe buscarla él mismo. Flujo obligatorio:

1. `lookup_agent_cli(name)` — consulta knowledge.yaml
2. Si no lo conoce → Context7 primero (prioridad), web search si no está
3. Construir comando completo y **mostrarlo al usuario antes de guardar**
4. Tras confirmación → `add_agent(...)` + `register_agent_knowledge(...)`

**Tres ficheros de datos separados:**

```
knowledge.yaml          → en el repo, compartido, base comunitaria
knowledge.local.yaml    → local del usuario, en .gitignore
config.yaml             → configuración activa, en .gitignore
.env                    → secrets, en .gitignore
```

**knowledge.yaml (repo)** — base de conocimiento compartida:
- Sintaxis de CLIs verificada por maintainers y comunidad
- Incluye: command_template, flags, model_env, ejemplos de modelos, docs_url
- Campo `verified: true/false` y `verified_version`
- NO implica que el CLI esté instalado — es solo conocimiento de sintaxis
- CLIs iniciales: aider, opencode, codex, goose, smolagents
- Providers iniciales: ollama, lmstudio, groq, openrouter, anthropic

**knowledge.local.yaml** — aprendizaje automático del usuario:
- Claude guarda aquí lo que aprende buscando en internet/Context7
- Override sobre knowledge.yaml del repo (merge en runtime)
- El usuario puede promoverlo al repo abriendo un PR

**Separación clara:**
- `knowledge.yaml` sabe CÓMO llamar a los CLIs
- `config.yaml` sabe QUÉ tienes configurado y disponible
- Son ortogonales: puedes tener conocimiento de 20 CLIs y solo 1 configurado

**Flujo de contribución comunitaria:**
usuario prueba CLI → Claude aprende → guarda en knowledge.local.yaml
→ usuario verifica → copia a knowledge.yaml → PR al repo
→ otros usuarios lo tienen en la siguiente actualización

### 10. Mecanismo de preguntas del worker al orchestrador — valorar implementar

El proyecto `bassimeledath/dispatch` resuelve bien un problema concreto: cuando
un worker se queda bloqueado, en vez de fallar silenciosamente o alucinar,
escribe una pregunta en un fichero. El orchestrador (Claude) la detecta,
la surfacea al usuario, recibe respuesta, y el worker continúa **sin perder
contexto** — sin reiniciar, sin re-explicar nada.

```
worker se bloquea
  → escribe en /tmp/jobs/abc123.question: "requirements.txt no existe, ¿qué implemento?"
  → MCP detecta fichero y notifica a Claude
  → Claude pregunta al usuario
  → usuario responde
  → MCP escribe respuesta en /tmp/jobs/abc123.answer
  → worker continúa desde donde lo dejó
```

Esto cubre directamente el requisito #2 (supervisión sin polling constante)
y es más elegante que polling puro. El worker señaliza activamente cuando
necesita input, en vez de que Claude tenga que ir a mirar periódicamente.

**Para Opus:** evaluar si implementar este patrón como mecanismo de señalización
estándar en claude-steward. Las herramientas MCP serían:
- `get_worker_questions(job_id)` — ¿tiene alguna pregunta pendiente el worker?
- `answer_worker_question(job_id, answer)` — manda la respuesta al worker

El worker (sea Aider, OpenCode, smolagents, o un script custom) necesitaría
convención de escritura de ficheros — algo que documentar en `knowledge.yaml`.

### 12. Fork vs desde cero

**Decisión: desde cero.**

Los repos existentes (`lambertmt/llama-mcp-server`, `houtini-lm`) resuelven
un problema mucho más estrecho. Hacer fork requeriría reescribir >90% del código
heredando decisiones de diseño incompatibles con esta arquitectura.

Se toman como referencia (no fork) solo dos ideas concretas:
- El **pre-flight token estimator** de `houtini-lm` (~30 líneas)
- El patrón de **think-block stripping** para modelos reasoning (~10 líneas)

---

### 14. GEPA — descartado para el núcleo, mencionado como herramienta futura

GEPA es un optimizador de prompts por evolución genética con reflexión LLM.
Interesante pero no encaja en el núcleo del proyecto porque:
- Necesita dataset de evaluación con métrica cuantificable (trabajo no trivial)
- Está diseñado para uso offline/batch, no runtime
- Añade dependencia pesada (DSPy) sin beneficio inmediato

Uso futuro posible: optimizar offline los prompts que Claude manda a agentes
delegados, usando el MCP Adapter que GEPA ya tiene. No integrar ahora.

### 15. LLMs locales: coste 0 ≠ opción válida

El config debe permitir declarar explícitamente si un provider es apto o no,
independientemente de su coste. Campos obligatorios por provider:

```yaml
providers:
  ollama_local:
    suitable_for: []              # lista vacía = no usar para delegación
    not_suitable_reason: "too slow for current hardware"
    speed_tier: "unusable"        # fast / acceptable / slow / unusable
```

Regla en CLAUDE.md: consultar siempre `suitable_for` y `speed_tier` antes
de elegir provider. `cost_tier: 0` nunca implica preferencia automática.

### 16. CLAUDE.md — skill de delegación

Fichero obligatorio en la raíz del proyecto. Contenido mínimo:
- Árbol de decisión: cuándo delegar vs hacer Claude mismo
- Árbol de decisión: qué recurso elegir (sync vs background, qué cola)
- Regla explícita: coste 0 no implica preferencia
- Cómo escribir instrucciones de calidad para agentes débiles
  (atómicas, con contexto suficiente, sin ambigüedad)
- Tabla de colas y cuándo usar cada una

### 17. Nomenclatura de colas (revisada)

Eje correcto: dónde se ejecuta × qué tipo de recurso consume:

```yaml
queues:
  run_local:    slots: 4   # comandos locales, paralelizable
  run_remote:   slots: 4   # comandos en host remoto, paralelizable
  gpu_local:    slots: 1   # LLM GPU local, serie obligatoria
  gpu_remote:   slots: 1   # LLM GPU remota, serie obligatoria
  agent_local:  slots: 2   # coding agents locales (aider, opencode local)
  agent_remote: slots: 2   # coding agents remotos (opencode en dev_box)
```

Lógica de slots: run_* paralelo (no compiten por GPU),
gpu_* serie (GPU es cuello de botella),
agent_* semi-paralelo (depende de si comparten recursos).

## Lo que falta definir (para Opus)

1. **Estructura de ficheros y módulos** del proyecto Python
2. **Tests** — cómo testear el MCP sin Claude real (mock MCP client)
3. **Instalación y bootstrapping** — cómo arrancar la primera vez, cómo instalar ts en remoto
4. **Seguridad SSH** — gestión de claves, known_hosts, hosts de confianza
5. **Logging y observabilidad** — qué loguea el MCP, dónde, cómo consultarlo
6. **Manejo de errores** — qué pasa si ts no está instalado, si Ollama no responde, si el host remoto no es alcanzable
7. **Streaming parcial** — estrategia para outputs largos en curso (fichero + polling vs notifications/progress)
8. **Primer prototipo** — qué subset mínimo implementar primero para tener algo funcionando
9. **Documentación de uso** — cómo Claude sabe qué tools usar y cuándo (system prompt o CLAUDE.md)

---

## Prompt sugerido para Opus en Claude Code

```
Eres el arquitecto técnico de un proyecto llamado "MCP Orchestrator".

Lee el fichero de briefing adjunto. Contiene todas las decisiones de diseño 
ya tomadas en una sesión de investigación previa.

Tu tarea es hacer una planificación técnica completa:

1. Estructura de módulos y ficheros del proyecto
2. Interfaces entre módulos (qué expone cada uno)
3. Plan de implementación por fases (qué va primero, qué depende de qué)
4. Estrategia de testing sin Claude real
5. Manejo de errores y casos límite para cada capa
6. CLAUDE.md — instrucciones para que Claude sepa usar el MCP correctamente
7. Qué implementar en el prototipo mínimo viable (fase 1)

No implementes nada todavía. Solo planifica y valida que la arquitectura 
es coherente. Si ves problemas o mejoras, señálalos antes de planificar.
```
