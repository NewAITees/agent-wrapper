## 運用ルール
1. タスクを追加するときはチェックボックス形式で書く
2. 完了したら `[x]` にする
3. セクションが全て完了したら、セクションごと削除してよい

## spec: 最近使ったフォルダUI (2026-09-01)
- [x] `index.html` に `/recent-folders` の選択UIを追加する
- [x] 既存API呼び出しとフォルダ選択動作を維持する
- [x] UI変更を検証する

## spec: orchestrator / utility ロール導入(2026-09-01、ユーザー指示)
- [x] セットアップ画面のデフォルト行/名前候補に `orchestrator` `utility` を追加(ラベルのみ、当面のスコープ)
- [ ] (未決定・要検討) orchestratorの「各タスクからの許可の一次受付」をどう技術的に検知するか。3案を提示しユーザーに聞いたが「作業を依頼しつつ計画はtodoに書いて」との返答で、この場では決定を保留し記録のみ:
  - A案: 今回はUIのプリセット(ラベル)追加に留め、許可ルーティングの実装は別途設計する
  - B案: 各raw-ptyセッションの出力を`rules.check_destructive`相当の正規表現で監視し、検知したらorchestrator(人間)に通知する(旧wrapper.py方式の再利用に近い)
  - C案: 許可が重要なセッションは生ptyではなく既存の`wrapped`方式(ClaudeRunner/CodexRunner、SDK/MCPフックで構造化された許可イベントが取れる)をorchestrator配下に組み込む。opencodeはserveが不安定なため当面対象外
- [ ] orchestratorの役割定義: 各タスク(セッション)への仕事の割り振り(ディスパッチ)をどう行うか(orchestrator自身がLLMセッションとして他セッションのinboxファイルに指示を書き込む想定に近いか要確認)
- [ ] utilityの役割定義: `ollama_client.py`にある既存の一次判定関数群(judge_permission/classify_destructive_match/summarize_chunk/judge_conversational_question等)を、utilityロールのセッション(またはサブエージェント)からどう呼び出せるようにするか
- 背景: orchestratorは元々のagent-wrapperプロジェクトの主眼(ollamaによる一次受付+人間承認)をこの新しいマルチセッションサーバーに引き継ぐ役割として位置づけられている。utilityはその一次受付処理をollamaで担う雑務サブエージェント。

## spec: agent-wrapperサーバー化(2026-08-31〜、調査段階)
- [x] opencode/claude/codexの実際の挙動調査(ネイティブbg/daemon/serve機能の有無)
- [x] docs/agent_server_architecture_options.md にOption A(ネイティブ活用)/B(自前pty)/C(ハイブリッド)の比較をまとめる
- [ ] (要ユーザー確認) A/B/Cのどれを採用するか決定
- [ ] `claude --bg`で既存の承認フック(can_use_tool/PreToolUse)相当が使えるか検証
- [ ] 既存`CodexRunner`のapproval-policy="untrusted"が現行codex 0.149.1で動くか実機確認(deprecated警告・enum変更が判明済み、既存機能が壊れている疑い)
- [ ] opencode serveがこの環境で起動しない原因調査(Bunランタイム起因の可能性、netstatでポート競合ではないことは確認済み)
- 背景: 常駐サーバー化・Web UIでの複数セッション並列管理(planner/worker/reviewerの役割制御、ファイル経由通信、システムプロンプト制御)を目指す。詳細はdocs/agent_server_architecture_options.md参照。

## spec: 残タスク(play_groundから転記・2026-07-12)
- [ ] (上流バグ) codexのサンドボックス昇格承認がmcp経由でクライアントに届かない問題(openai/codex#21982系統)の修正を追い、修正されたら--codex-sandboxの既定(danger-full-access)を見直す
- [ ] (将来リスク) codexのelicitation応答形式が将来MCP標準に修正された場合の再確認(CodexRunner._elicit_resultは両対応済みだがcodex更新時に要確認)
- 背景: --dangerously-skip-permissions を使うとClaude自身の権限確認が無効化され本末転倒、外すとヘッドレスモードはstdinを待たずプロセスが終了することを実機検証で確認済み。詳細はdocs/agent_wrapper_sdk_integration_plan.mdを参照。
