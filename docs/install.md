# Installation

Choose one method below, then verify your environment once ([Requirements](#requirements)).

## skills CLI

```bash
npx skills add t-0hmura/reaction-draw
```

Update:

```bash
npx skills update
```

## GitHub CLI

```bash
gh skill install t-0hmura/reaction-draw reaction-draw
```

Update:

```bash
gh skill update
```

## Claude Code plugin

```bash
claude plugin marketplace add t-0hmura/reaction-draw
claude plugin install reaction-draw@t-0hmura
```

Update:

```bash
claude plugin marketplace update t-0hmura
claude plugin update reaction-draw@t-0hmura
```

## Codex plugin

```bash
codex plugin marketplace add t-0hmura/reaction-draw
codex plugin add reaction-draw@t-0hmura
```

Update:

```bash
codex plugin marketplace upgrade t-0hmura
codex plugin add reaction-draw@t-0hmura
```

## Antigravity plugin

```bash
agy plugin install https://github.com/t-0hmura/reaction-draw
```

Update: Re-run the same command.

## Manual clone

```bash
git clone https://github.com/t-0hmura/reaction-draw ~/reaction-draw
mkdir -p ~/.claude/skills && ln -s ~/reaction-draw/skills/reaction-draw ~/.claude/skills/reaction-draw                  # Claude Code
mkdir -p ~/.agents/skills && ln -s ~/reaction-draw/skills/reaction-draw ~/.agents/skills/reaction-draw                  # Codex
mkdir -p ~/.gemini/config/skills && ln -s ~/reaction-draw/skills/reaction-draw ~/.gemini/config/skills/reaction-draw    # Antigravity
```

Update:

```bash
git -C ~/reaction-draw pull
```

## Requirements

Requires Python 3.9+ with RDKit, and Playwright with Chromium or Google Chrome. From the installed skill directory (containing `SKILL.md`), run:

```bash
python scripts/draw_figure.py --check-env
```

`ENVIRONMENT OK` means the environment is ready. Otherwise, install any missing dependencies it reports.

On Windows, a venv works. From the skill directory in PowerShell:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install rdkit playwright
.venv\Scripts\python -m playwright install chromium
.venv\Scripts\python scripts\draw_figure.py --check-env
```
