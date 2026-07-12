---
name: delegate
description: 承認ゲート付きAIラッパー(agent-wrapper)でタスクをclaude/codexに委任する。「委任して」「agent-wrapperで実行して」「作業を外に出して」「delegateして」等で使用。仕様が詳細化済みのタスクを、ollama一次受付+人間のダッシュボード承認つきで実エージェントに実行させ、完了後にレビューする定型手順。
---

# /delegate — agent-wrapperによるタスク委任

このスキルは単独リポジトリ化された `agent-wrapper`(https://github.com/NewAITees/agent-wrapper)を利用する。
2026-07-12にplay_groundから切り出した。play_ground側にも当面コピーが残っているが、
`agent-wrapper`コマンドの実体はこちらの新リポジトリに切り替え済み。

参照:
- 設計意図: Windows `C:\analysis2\agent-wrapper\docs\agent_wrapper_usage_workflow.md` / WSL `/mnt/c/analysis2/agent-wrapper/docs/agent_wrapper_usage_workflow.md`
- 操作詳細: Windows `C:\analysis2\agent-wrapper\README.md` / WSL `/mnt/c/analysis2/agent-wrapper/README.md`
- 許可設計: Windows `C:\analysis2\agent-wrapper\docs\agent_wrapper_permission_policy.md` / WSL `/mnt/c/analysis2/agent-wrapper/docs/agent_wrapper_permission_policy.md`

## 位置づけ(最重要)

agent-wrapperは「仕様が固まった作業を安全に外出しする実行装置」。曖昧なタスクを
そのまま投げない。仕様が曖昧なら、委任の前に /spec 等でゴール・完了基準・制約を
確定させること。依頼文はこのスキルを実行するClaude Codeが書く。

## 前提チェック

1. `agent-wrapper --help` が通ること(`uv tool install -e .` でPATH登録済みの前提。未登録なら `C:\analysis2\agent-wrapper\.venv\Scripts\agent-wrapper.exe --help`、WSLでは `/mnt/c/analysis2/agent-wrapper/.venv/Scripts/agent-wrapper.exe`)
2. Ollama API `http://127.0.0.1:11436` が応答し、`gemma4:e4b` がpull済みであること(不可ならすべて人間エスカレーションになる)
3. 対象ディレクトリがgit管理下で、作業ツリーが意図した状態であること(`git status`)
4. ポート28765が空いていること(残存リスナーがあれば停止を提案)

## 手順

### 1. 依頼文(プロンプトファイル)の生成

一時ディレクトリにMarkdownで作成する。必ず含める:

- 対象: リポジトリパス、読むべきファイル/ドキュメント、todo.mdの該当セクション
- 実装項目の具体的な列挙(設計方針・データ構造まで書けるなら書く)
- 制約の定型文(毎回コピーして使う):
  - 既存テストを壊さない。新機能にはテストを追加する
  - 完了条件: テスト全件成功+lint/型チェック(**スコープを明示** — リポジトリ全体にかけない)
  - **git commit は行わない**(人間レビュー後にコミット)
  - 既存のdocstring・設計コメントを削除しない
  - 要求以上の機能・抽象化を追加しない。dangerously系フラグ禁止
  - 完了時に実装内容・設計判断・テスト結果・残課題を報告して終了
- 中断からの再開の場合: 「作業ツリーに前回の途中実装がある。まずgit status/diffで把握し引き継ぐこと」

### 2. デタッチ起動(教訓: サブエージェントの子にしない)

長寿命プロセスをサブエージェントのバックグラウンドジョブとして起動すると、
ジョブのタイムアウトでプロセスツリーごと強制終了される(実績あり)。必ず独立プロセスにする:

```powershell
Start-Process -FilePath "<対象または実行環境のpython/agent-wrapper>" `
  -ArgumentList '--agent','codex','--prompt-file','<依頼文パス>' `
  -WorkingDirectory '<対象ディレクトリ>' -WindowStyle Hidden `
  -RedirectStandardOutput '<scratch>\wrapper_stdout.log' `
  -RedirectStandardError '<scratch>\wrapper_stderr.log'
```

- `--agent claude`(claude-agent-sdk/Claudeサブスク)か `--agent codex`(codex mcp-server/ChatGPTサブスク)を選ぶ
- **`--codex-sandbox` は指定しない**(既定=danger-full-accessは意図的な設計判断・2026-07-11ユーザー決定)。
  安全側に見えても `workspace-write` を足してはならない: mcp経由では権限昇格の承認が
  クライアントに届かない上流バグ(openai/codex#21982系統)があり、人間がダッシュボードで
  承認しても実行基盤に拒否されて作業が停止する(2026-07-12に実運用で発生)。
  承認ゲート(全コマンドのelicitation承認)はサンドボックス設定と無関係に常時有効。
  理由詳細: docs/agent_wrapper_permission_policy.md §8。指定するのはユーザーが明示的に
  「信頼できないコードを扱うのでサンドボックスを付けて」と指示した場合のみ
- **ラッパー自身のコード(agent-wrapperリポジトリ)を改修させる場合**は、実行中プロセスが
  編集途中の壊れたコードを踏まないよう、安定コミットのgit worktreeからコードを読むランチャー方式にする
  (sys.path先頭に安定worktreeを挿入し、cwdだけ対象に向ける。agent-wrapperリポジトリの.venvのpythonで実行)
- 起動後、ポート28765のLISTENを確認する

### 3. 監視はしない(トークン節約方針・2026-07-11ユーザー指示)

**監視サブエージェントは起動しない。** 監視1回あたりClaudeトークンを5〜7万消費する一方、
その成果物はダッシュボードでユーザーが見ている情報とほぼ重複するため費用対効果が低い。

- ラッパーはデタッチ独立プロセスなので、監視なしでも安全に動く
- 進行状況・承認対応はユーザーがダッシュボードで直接行う
- 完了(stopped)はユーザーの合図(「終わったよ」等)を待つ。Claude Code側から途中の
  ポーリングもしない
- 例外的に状態確認が必要な場合のみ、Claude Code自身が `/status` を1回だけcurlする
  (サブエージェントは立てない)
- いかなる場合も `/approve` `/deny` `/respond` をAIが呼ばない(承認は人間の役割)

### 4. 人間への案内

ダッシュボードURL(`http://localhost:28765/`、LAN側URLとQRは起動ログにある)を伝え、
承認待ちが上がったら内容を確認して 承認/承認+メッセージ/説明要求/却下 で応答してもらう。
「rm -rf検知」等の見出しでも実体がファイル書き込みのことがある(全文走査の安全側誤検知)ため、
判断に迷う承認要求はこのClaude Codeセッションが中身を調べて説明する。

### 5. 完了後のレビュー(エージェントの完了報告を鵜呑みにしない)

1. テスト・lint・型チェックを**自分の手で再実行**する(スコープ明示)
2. `git diff` 全体をレビューする。特に: docstring/設計コメントの不当な削除、
   テストが検知できない種類の破壊(例: HTML内JSの構文エラー)、要求外の変更
3. UI変更はヘッドレスブラウザ等で実際に描画・操作確認する
4. 問題があれば自分で修正するか、継続依頼文で再委任する
5. 人間のy/n承認を得てコミットする

## 失敗パターン(実績)

- 監視エージェントの子としてラッパー起動 → 60分でツリーごとkill(§2で回避)
- ラッパー自身の改修中に編集途中コードで起動 → 最初の承認で自爆(§2の安定worktree方式で回避)
- 完了報告に「テスト全通過」とあってもJS構文エラーで画面が白い/知見コメントが消えている(§5で検出)
- 委任側Claudeが善意で `--codex-sandbox workspace-write` を追加 → 昇格承認バグで
  apply_patch破損・spawn EPERM・承認しても拒否、が連鎖(§2の指定禁止で回避)
