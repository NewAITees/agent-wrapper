## 運用ルール
1. タスクを追加するときはチェックボックス形式で書く
2. 完了したら `[x]` にする
3. セクションが全て完了したら、セクションごと削除してよい

## spec: Approval Broker統合の残課題(2026-09-01、codexレビューで判明)
- [x] role名のパストラバーサル/任意ファイル書き込み脆弱性を修正(`validate_role_name`, server.py)。修正済み・テスト済み
- [ ] (重大) `SessionManager.start()`が常にraw-PTYの`Session`を直接生成しており、Approval Broker/`SharedState`/adapterへ実際には未接続。`/approvals`は本番では常に空、UIの承認レールから実ターミナルへ応答できない
- [ ] (重大) `ApprovalBroker._expire_locked()`が要求をexpiredにするだけでcallbackを呼ばないため、期限切れ時にrunner(wrapper.py/runners/base.py)が永久に待機し続ける。`SharedState.snapshot()`も期限切れ後に表示だけ`running`に戻るため二重に不整合
- [ ] (高) `stop_all()`がBrokerのpending要求をcancelしない。同名セッションを再起動すると前セッションの要求が残り得る(`SharedState.cancel_session()`はあるが`SessionManager`が呼べる構造になっていない)
- [ ] (中) `ApprovalRequestInput.expires_at`にnaive datetimeが渡るとBrokerの比較で`TypeError`になる。timezone検証/正規化が必要
- [ ] (中) `build_argv()`の`name`引数が既定値なしの必須位置引数になり、旧シグネチャ`build_argv(harness, source, model)`の呼び出しは`TypeError`になる(このリポジトリ内の呼び出しは全て更新済みだが、外部利用者がいる場合は破壊的変更)
- [ ] (中) HTTP API(`/approvals`, `/approvals/<id>/respond`)の実レスポンス・エラー系(400/409)・二重応答・別セッションからの応答拒否・期限切れ要求への応答、を検証する結合テストが無い(現状はBroker単体のユニットテストとHTML文字列存在確認のみ)
- 背景: このApproval Broker機能はユーザーが別のAIエージェントに並行して実装を依頼しているもの。上記はcodexにread-onlyレビューを依頼して判明した。パストラバーサルのみClaudeが直接修正し、残りは実装元のAIエージェントでの対応を想定して記録のみ行う。

## docs: Repository contributor guide (2026-09-01)
- [x] 既存AGENTS.mdがないことを確認する
- [x] 構造・開発コマンド・規約・テスト・PR・安全要件を200〜400語で整理する
- [x] リポジトリ直下にAGENTS.mdを作成する

## spec: 承認フロー左・ターミナル右の操作画面 (2026-09-01、承認済み)
- [x] 実行画面を左の人間承認レールと右のターミナル領域に分ける
- [x] 承認要求の内容・リスク・セッション・状態を表示する
- [x] 承認・説明要求・却下をApproval Broker APIへ送る
- [x] 承認要求がない場合も一次受付から人間判断までの流れを表示する
- [x] モバイルでは承認レールを上、ターミナルを下へ配置する
- [x] 既存のセットアップ・PTY・WebSocket操作を維持する
- [x] オーケストレーターを最上段、他4ターミナルを下段の2行2列に構造化する
- [x] ブラウザ表示とpytest/ruff/mypyを検証する

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

## research: 承認一次受付と人間エスカレーション (2026-09-01)
- [x] 既存のClaudeRunner/CodexRunner/SharedState/権限ポリシーを確認する
- [x] Claude Code・Codex・OpenCodeの構造化された承認窓口を公式資料と現行実装で比較する
- [x] raw-PTY出力監視を主経路にしない統一Approval Broker案を整理する
- [x] ApprovalRequest共通スキーマと状態遷移を仕様化する
- [ ] OpenCode V2のpermission evaluate hookまたはserver SSEを使う最小アダプターを実機検証する
- [ ] 現行Codexでapp-serverの承認イベントを検証し、mcp-server依存の移行可否を決める
- [x] 複数セッションを対象にrequest_id単位の競合・重複応答テストを追加する(再接続はtransport実装時に追加)

## spec: Approval Broker基盤実装 (2026-09-01、承認済み)
- [x] 共通ApprovalRequest/ApprovalResponseと状態遷移を実装する
- [x] SharedStateをセッション別Brokerビューとして接続する
- [x] ClaudeRunner/CodexRunnerの既存承認フローをBroker経由で維持する
- [x] serverに承認一覧・request_id指定応答APIを追加する
- [x] permission requestとspecification questionを別種別として記録する
- [x] 重複応答・別セッション応答・期限切れ・always拒否をテストする
- [x] OpenCode beta APIをtransportから隔離する構造化イベント変換境界を追加する
- [x] pytest/ruff format/ruff check/mypyを通す

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
