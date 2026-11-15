# インストール方法

以下のいずれかの方法でインストールしてから、[必要な環境](#必要な環境)を 1 回確認してください。

## skills CLI

```bash
npx skills add t-0hmura/reaction-draw
```

更新:

```bash
npx skills update
```

## GitHub CLI

```bash
gh skill install t-0hmura/reaction-draw reaction-draw
```

更新:

```bash
gh skill update
```

## Claude Code プラグイン

```bash
claude plugin marketplace add t-0hmura/reaction-draw
claude plugin install reaction-draw@t-0hmura
```

更新:

```bash
claude plugin marketplace update t-0hmura
claude plugin update reaction-draw@t-0hmura
```

## Codex プラグイン

```bash
codex plugin marketplace add t-0hmura/reaction-draw
codex plugin add reaction-draw@t-0hmura
```

更新:

```bash
codex plugin marketplace upgrade t-0hmura
codex plugin add reaction-draw@t-0hmura
```

## Antigravity プラグイン

```bash
agy plugin install https://github.com/t-0hmura/reaction-draw
```

更新: 同じコマンドを再実行します。

## 手動でのクローン

```bash
git clone https://github.com/t-0hmura/reaction-draw ~/reaction-draw
mkdir -p ~/.claude/skills && ln -s ~/reaction-draw/skills/reaction-draw ~/.claude/skills/reaction-draw                  # Claude Code
mkdir -p ~/.agents/skills && ln -s ~/reaction-draw/skills/reaction-draw ~/.agents/skills/reaction-draw                  # Codex
mkdir -p ~/.gemini/config/skills && ln -s ~/reaction-draw/skills/reaction-draw ~/.gemini/config/skills/reaction-draw    # Antigravity
```

更新:

```bash
git -C ~/reaction-draw pull
```

<a id="requirements"></a>

## 必要な環境

Python 3.9 以上、RDKit、Playwright、および Chromium または Google Chrome が必要です。インストールしたスキルのディレクトリ（`SKILL.md` がある場所）で以下を実行してください。

```bash
python scripts/draw_figure.py --check-env
```

`ENVIRONMENT OK` と表示されれば準備完了です。不足している項目が表示された場合は、指示に従ってインストールしてください。
