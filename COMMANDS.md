# MCP Orchestrator — Comandos de referencia

## OpenCode (CLI — recomendado)

### Sin servidor (proceso propio)
```bash
opencode run "tu prompt aquí"
```

### Contra servidor headless existente
```bash
opencode run --attach http://localhost:4096 "tu prompt aquí"
```

---

## OpenCode (API REST directa)

Servidor headless en `http://localhost:4096`. Arrancar con:
```bash
opencode --headless
```

> **Seguridad:** sin configurar `OPENCODE_SERVER_PASSWORD`, el servidor queda expuesto sin autenticación.
> Se recomienda siempre arrancarlo con password, especialmente si escucha en red:
> ```bash
> OPENCODE_SERVER_PASSWORD=tu_password opencode --headless
> ```
> O exportar la variable antes de arrancar para no exponerla en el historial de comandos.

### Crear sesión
```bash
curl -s -X POST http://localhost:4096/session \
  -H "Content-Type: application/json" \
  -d '{}'
```

### Crear sesión y capturar ID
```bash
SESSION_ID=$(curl -s -X POST http://localhost:4096/session \
  -H "Content-Type: application/json" \
  -d '{}' | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
```

### Enviar mensaje a una sesión
```bash
curl -s -X POST "http://localhost:4096/session/$SESSION_ID/message" \
  -H "Content-Type: application/json" \
  -d '{
    "modelID": "deepseek-v4-flash",
    "providerID": "opencode-go",
    "parts": [{"type": "text", "text": "tu prompt aquí"}]
  }'
```

### Listar sesiones existentes
```bash
curl -s http://localhost:4096/api/session
```

---

## OpenCode — Selección automática de modo (attach vs local)

Variable `OPENCODE_URL` en `.env`:
- **Vacía o no definida** → usar `opencode run` directo, sin ninguna comprobación
- **Con valor** (ej. `http://localhost:4096` o `http://192.168.1.x:4096`) → intentar **conexión TCP** al host:puerto con timeout corto (~0.5s):
  - Conecta → `opencode run --attach $OPENCODE_URL`
  - No conecta → `opencode run` sin attach (fallback silencioso)

> No usar ping (ICMP) — localhost siempre responde aunque el puerto esté cerrado.
> El overhead de un TCP connect es ~1-50ms, despreciable frente al tiempo de respuesta de opencode.
> Se comprueba en cada llamada porque el servidor puede haberse caído entre invocaciones.

### Por qué usar modo servidor (headless/serve)

Mantener el proceso arrancado en segundo plano (`opencode --headless` / `jcode serve`) mejora el rendimiento de dos formas:
1. **Arranque en frío del agente** — cada llamada sin servidor relanza el binario, inicializa el runtime y carga la configuración (~1s en opencode, ~14ms en jcode pero con overhead real por llamada)
2. **Arranque en frío de los MCP servers** — los MCP servers configurados se arrancan junto con el agente y se mantienen vivos entre llamadas; sin servidor persistente, cada invocación los relanza desde cero

Esto es especialmente relevante cuando el MCP orchestrator encadena muchas llamadas seguidas.

---

## OpenCode — Permisos

> **Pendiente investigar** cómo configurar permisos para directorios externos.

Por defecto OpenCode solo opera dentro del directorio de trabajo del proyecto:
- ✅ Crear, leer, modificar y borrar ficheros dentro del proyecto → automático, sin preguntar
- ❌ Escribir en directorios externos (`/tmp`, etc.) → `auto-rejecting`, falla sin preguntar

Para el MCP tener en cuenta: las tareas deben operar siempre dentro del directorio del proyecto, o bien investigar cómo ampliar los permisos (`external_directory`).

---

## Aider

Instalado via `aider-install`. Usa Groq como provider (OpenAI-compatible).

### Con Groq (qwen3-32b)
```bash
cd ~/Dropbox/claude/repos/mcp-orchestrator && source .env
aider --model groq/qwen/qwen3-32b \
      --openai-api-key $GROQ_API_KEY \
      --openai-api-base $GROQ_API_BASE \
      --message "tu prompt aquí" \
      --yes --no-git
```
> Nota: muestra chain-of-thought en la salida. Pendiente investigar cómo suprimirlo.

### Con Gemini 2.5 Flash (recomendado — free tier, sin chain-of-thought)
```bash
cd ~/Dropbox/claude/repos/mcp-orchestrator && source .env
aider --model gemini/gemini-2.5-flash \
      --api-key gemini=$GEMINI_API_KEY \
      --message "tu prompt aquí" \
      --yes --no-git
```

### Con OpenCode Go (deepseek-v4-flash)
```bash
cd ~/Dropbox/claude/repos/claude-steward && source .env
aider --model openai/deepseek-v4-flash \
      --openai-api-key $OPENCODE_API_KEY \
      --openai-api-base https://opencode.ai/zen/go/v1 \
      --no-show-model-warnings \
      --no-stream \
      --message "tu prompt aquí" \
      --yes --no-git
```

> `--no-show-model-warnings` evita que abra el navegador con la doc del modelo desconocido.
> `--no-stream` reduce el tiempo de ~33s a ~18s habilitando prompt cache entre las 2 llamadas que hace aider.
> La lentitud vs opencode/smolagents es estructural: aider siempre hace mínimo 2 llamadas LLM (ask + apply).

> Gemini 2.5 Pro y 3.x requieren billing activado en Google AI Studio (no disponibles en free tier).

### Con Ollama (local — sin dependencia de APIs externas)
```bash
cd ~/Dropbox/claude/repos/mcp-orchestrator && source .env
aider --model ollama/$OLLAMA_MODEL \
      --message "tu prompt aquí" \
      --yes --no-git
```

> Requiere Ollama arrancado (`ollama serve`). Modelo configurado en `OLLAMA_MODEL` del `.env`.
> Muestra chain-of-thought si el modelo lo tiene (ej. qwen3).
> La variable `OLLAMA_API_BASE` no es necesaria si Ollama corre en `localhost:11434` (defecto),
> pero se puede apuntar a otra máquina: `OLLAMA_API_BASE=http://192.168.1.x:11434`.

---

## smolagents

Instalado en `venv/` con `smolagents[toolkit,litellm]`. Script de prueba: `test_smolagents.py`.

### Con Groq (qwen3-32b) — 6.5s, 2 steps
```bash
cd ~/Dropbox/claude/repos/mcp-orchestrator
venv/bin/python test_smolagents.py groq
```

### Con Gemini 2.5 Flash (más rápido) — 3.2s, 1 step
```bash
cd ~/Dropbox/claude/repos/mcp-orchestrator
venv/bin/python test_smolagents.py gemini
```

### Con GitHub Models / DeepSeek-V3-0324 — 5.6s, 1 step
```bash
cd ~/Dropbox/claude/repos/mcp-orchestrator
venv/bin/python test_smolagents.py github
```

### Con Ollama (local — sin dependencia de APIs externas) — ~130s en HW modesto
```bash
cd ~/Dropbox/claude/repos/mcp-orchestrator
venv/bin/python test_smolagents.py ollama
```

### Con OpenCode Go (deepseek-v4-flash) — ~6.3s, 1 step
```bash
cd ~/Dropbox/claude/repos/claude-steward
venv/bin/python test_smolagents.py opencode
```

> **Endpoint:** `https://opencode.ai/zen/go/v1` (OpenAI-compatible).
> Modelos disponibles: `deepseek-v4-flash`, `deepseek-v4-pro`.
> **No confundir con Zen** (`opencode.ai/zen/v1`) que tiene modelos distintos (sin deepseek).

> El tiempo varía enormemente según el HW. En equipos con GPU potente puede ser competitivo
> con las APIs cloud. Útil cuando se requiere privacidad total o no hay acceso a internet.
> Apuntar a otra máquina cambiando `OLLAMA_API_BASE` en `.env`.

---

## Claude CLI

Instalado en `~/.local/bin/claude`. Usa el modelo configurado en la cuenta de Anthropic.

### Modo no-interactivo (una sola orden)
```bash
claude -p "tu prompt aquí"
```

### Con directorio de trabajo específico
```bash
claude -p "tu prompt aquí" --cwd /ruta/al/proyecto
```

> `-p` lanza Claude en modo print — responde y termina, sin sesión interactiva.
> ~6.5s para tareas simples. Mismo binario que se usa en local, apuntable a máquina remota via SSH.
> Candidato natural para el MCP orchestrator: delega tareas a Claude como subagente.

---

## Codex CLI

Instalado via Homebrew. Usa OpenAI por defecto (gpt-5.5).

### Uso básico (solo respuesta, sin ficheros)
```bash
codex exec --skip-git-repo-check "tu prompt aquí"
```

### Con permisos de escritura (crear/modificar/borrar ficheros)
```bash
codex exec --skip-git-repo-check -s workspace-write "tu prompt aquí"
```

> Sin `-s workspace-write` el sandbox es `read-only` y no puede escribir ficheros.
> Con `workspace-write` tiene acceso al directorio de trabajo, `/tmp` y `$TMPDIR`.
> Modo `danger-full-access` elimina todas las restricciones del sandbox.

### Modos de sandbox disponibles
| Modo | Acceso |
|---|---|
| `read-only` | Solo lectura (defecto) |
| `workspace-write` | Escritura en workdir + /tmp |
| `danger-full-access` | Sin restricciones |

### Notas
- Ejecuta tareas multi-paso de forma autónoma sin pedir confirmación (`approval: never` en `exec`).
- `--skip-git-repo-check` necesario fuera de un repositorio git.
- Muestra cada comando shell que ejecuta antes de lanzarlo.
- ~7s para tareas simples con gpt-5.5.

---

## jcode

Instalado via script oficial. Soporta múltiples providers con OAuth y API key.

### Sin servidor (proceso propio)
```bash
jcode run "tu prompt aquí"                                    # provider por defecto
jcode -p gemini -m gemini-2.5-flash run "tu prompt aquí"
jcode -p ollama -m qwen3:8b run "tu prompt aquí"
```

### Con servidor persistente
```bash
jcode serve       # arrancar servidor en background
jcode connect     # conectar cliente al servidor existente
```

### Configurar provider por defecto
En `~/.jcode/config.toml`:
```toml
[provider]
default_provider = "copilot"
default_model = "gpt-4.1"
```

### Notas de compatibilidad
- **Copilot + `jcode run`**: falla con HTTP 400 "model not supported" aunque funcione en TUI. Bug conocido.
- **Groq**: el system prompt de jcode (~11k tokens) supera el límite free tier de Groq (6k TPM). No viable en free tier.
- **Gemini**: funciona pero tiene rate limit estricto en free tier.
- **Ollama**: funciona con prompts simples (~2min con qwen3:8b en HW modesto). Tareas más complejas pueden agotar el timeout — el system prompt de jcode es grande y exige más al modelo local. Competitivo en GPU potente.

---

## Comparativa de rendimiento (smolagents — tarea: primos hasta 50)

| Provider | Modelo | Tiempo | Steps | Notas |
|---|---|---|---|---|
| Gemini | gemini-2.5-flash | ~3.2s | 1 | Recomendado, free tier |
| GitHub Models | DeepSeek-V3-0324 | ~5.6s | 1 | Free con cuenta GitHub |
| Groq | qwen/qwen3-32b | ~6.5s | 2 | Chain-of-thought visible |
| Ollama (local) | qwen3:8b | ~130s | 1 | Depende del HW, sin APIs externas |
| OpenCode Go | deepseek-v4-flash | ~6.3s | 1 | opencode.ai/zen/go/v1, coste $0 (incluido en plan) |
