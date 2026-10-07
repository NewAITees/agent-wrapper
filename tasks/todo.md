## 運用ルール
1. タスクを追加するときはチェックボックス形式で書く
2. 完了したら `[x]` にする
3. セクションが全て完了したら、セクションごと削除してよい

## spec: wrapped入力キュー・API認証（2026-10-07、承認済み）
- [x] FR-21/FR-22と既存のセッション・runner・承認処理を確認する
- [x] 新規テストのRedを確認し、Claude専用の有界FIFOを実装する
- [x] 承認待ち後のターン境界配信、orchestrator限定idle待機、停止と残件破棄を実装する
- [x] 対象POSTの任意Bearer認証とREADMEを追加する
- [x] 境界・認証・承認待ち・件数・文字数の変異を回帰テストが検出することを確認する
- [x] 全tests・Ruff format/check・mypyを実行する（Git操作・依存追加なし）
- [ ] 全tests成功の完了基準を満たす（既存12件が一時ディレクトリACL拒否。TEMPをrepo内へ移しても再現）
- 最終検証: 313件中301 passed / 12 failed、206 subtests passed、pytest cache警告2件。新規25件は全件成功。Ruff checkは成功（探索アクセス拒否警告あり）、formatは36 files成功、mypyは24 source files成功。
- 変異検証: HTTP境界検査を無効化、compare_digestを常時true化、承認Future待ちを除去、件数・文字数上限を拡大した実装に対し、該当回帰テストがAssertionErrorで失敗することを確認（実行時patch、終了時に自動復元）。
- 判断: 絶対ターン上限と人間が終了を選んだ会話上限は従来の停止を維持。正常終了・明示停止で残件破棄、終了済みセッションは再利用しない。
- 未確認: 実Claude/Ollama・AITuberランチャー接続は指定どおり未実行。既存12件のアクセス拒否の詳細原因は未確定。

## spec: orchestrator MCP dispatch（2026-10-06、承認済み）
- [x] Red: 偽SessionManagerによるlist/send/read境界テスト、ClaudeRunnerのMCP受け渡しテスト、system prompt確認を追加して失敗を確認する
- [x] orchestration.pyにセッション操作関数と3つのSDK MCPツールを実装し、自己送信・入力済wrapped・文字数等を制約する
- [x] orchestratorのclaude wrappedセッションにだけMCPサーバーを渡し、MCPツール呼び出しを既存3層ゲートと説明関数へ接続する
- [x] orchestratorのsystem promptにツールと未起動セッションへの初回指示ルールを追記する
- [x] 指定されたテストだけ実行し、ruff check/mypy、差分・行番号を確認して完了報告する（コミット・pushしない）
- 検証記録: 対象テストの全実行は164 passed / 11 failed（既存テストがWindows `%TEMP%` のACLでアクセス拒否）。ACL依存クラスを除いた再実行は153 passed / 23 deselected / 81 subtests passed。`ruff check`、`mypy agent_wrapper`、`git diff --check`は成功。

## spec: orchestratorによる安全な承認一次受付（2026-10-06、承認済み）
- [ ] 実物のApprovalBroker/SessionManagerを使う規則テストを先に追加し、未実装で失敗させる
- [ ] orchestration MCPへpending一覧と制約付き応答を追加し、監査ログを記録する
- [ ] 2ツールを3層ゲート・人間向け説明・orchestrator限定配線へ接続する
- [ ] orchestrator system promptへ自律承認可能範囲と人間エスカレーション規則を追記する
- [ ] 指定された全テスト・ruff format/check・mypyを実行し差分をレビューする（コミット・pushなし）

## spec: AITuberへのpush通知
- [x] `AituberPusher`の非同期送信と単体テストを追加する
- [x] Approval Broker新規要求・セッション終了・inbox更新の通知フックを追加する
- [x] READMEに環境変数・イベント・受信仕様・失敗時挙動を記載する
- [x] 指定されたpytest・ruff・mypyを実行し差分をレビューする
- [x] 完了結果と未確認事項を報告する(コミットしない)

## fix: コミット前安全レビュー指摘 (2026-09-01、承認済み)
- [x] Claude role制限が`allowed_tools`経由でApproval Brokerを迂回しないようPreToolUseへ統合する
- [x] planner/reviewerのMarkdown例外と、それ以外の書き込み拒否を回帰テストで固定する
- [x] runner開始直後の`stop()`でも非同期処理が開始・継続しないようにする
- [x] OpenCode内部`ses_...`と画面セッション名を対応付け、`stop_one()`でpendingをキャンセルする
- [x] OpenCodeの未検証表示、検証用ポート変更、未完成スモークの型エラーを整理する
- [x] pytest・ruff format/check・mypyを通し、再レビュー後にコミット対象を提示する(212 passed、15 subtests)

## spec: Approval Broker統合の残課題(2026-09-01、codexレビューで判明) — 解消済み
- [x] role名のパストラバーサル/任意ファイル書き込み脆弱性を修正(`validate_role_name`, server.py)
- [x] `SessionManager.start()`がclaude/codexを既定で`wrapped`(`WrappedSession`)方式にするよう修正され、Approval Broker/`SharedState`へ実接続された(実機検証済み: reviewerロールでWriteをSDKレベルで拒否・plan_approvalの承認/却下がAPI経由で実際に機能することを2026-09-01に確認)
- [x] `ApprovalBroker._expire_locked()`が期限切れ時にcallbackを呼ぶよう修正され、`test_expired_request_cannot_be_approved`等で検証済み
- [x] `stop_one()`/`stop_all()`が`approval_broker.cancel_session()`を呼ぶよう修正済み(Claudeが実装)
- [x] `ApprovalRequestInput.expires_at`のnaive datetime正規化が実装され、`test_naive_expiration_is_normalized_to_utc`で検証済み
- [x] `build_argv()`の`name`引数に`"worker"`既定値を追加し後方互換を維持(Claudeが実装)
- [x] `ApprovalHttpIntegrationTests`で実HTTPサーバー相手の結合テスト(200/409等)が追加済み
- 背景: このApproval Broker機能はユーザーが別のAIエージェントに並行して実装を依頼しているもの。上記はcodexのread-onlyレビューで判明した課題群で、2026-09-01中に全て解消されたことを再確認した。

## spec: 機械的テスト実行API `/run-tests` (2026-09-01、実装済み) — 解消済み
- [x] `POST /run-tests`を追加する(`SessionManager.cwd`に対しsubprocess実行)
- [x] 決め打ちコマンド(`uv run pytest -q`)のみをsubprocessで直接実行し、AIを介さない
- [x] 終了コード・stdout/stderr・失敗テスト名(`parse_pytest_failures`)を構造化JSONで返す
- [x] tester/reviewerロールがこの結果を参照できる(ROLE_SYSTEM_PROMPTSで`/run-tests`を案内済み)
- 背景: テストのライフサイクルは「設計(人間/planner)→実装(tester)→実行(機械的、AI不要)」の3段階に分けるべきという2026-09-01の議論に基づく。Claudeが実装し、実サーバーで`/run-tests`が実際にpytestを実行して構造化結果を返すことを実機検証済み(2026-09-01)。

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
- [x] utilityの役割定義: `POST /utility/judge`(body: `mode`(permission/summarize/conversational)+`text`)を追加し、`ollama_client.judge_permission`/`summarize_chunk`/`judge_conversational_question`をraw-ptyセッション(utilityロール等)からもHTTP経由で呼べるようにした(agent-wrapper-server自体のworkerセッションに実装を委譲し、2026-09-01に実装・pytest 204件/ruff/mypy通過を確認済み)。呼び出し元(utilityロール実体)からの実利用はまだスコープ外。
- 背景: orchestratorは元々のagent-wrapperプロジェクトの主眼(ollamaによる一次受付+人間承認)をこの新しいマルチセッションサーバーに引き継ぐ役割として位置づけられている。utilityはその一次受付処理をollamaで担う雑務サブエージェント。

## research: 承認一次受付と人間エスカレーション (2026-09-01)
- [x] 既存のClaudeRunner/CodexRunner/SharedState/権限ポリシーを確認する
- [x] Claude Code・Codex・OpenCodeの構造化された承認窓口を公式資料と現行実装で比較する
- [x] raw-PTY出力監視を主経路にしない統一Approval Broker案を整理する
- [x] ApprovalRequest共通スキーマと状態遷移を仕様化する
- [ ] OpenCode V2のpermission evaluate hookまたはserver SSEを使う最小アダプターを実機検証する
- [ ] OpenCode 1.18.25のTUIで、安定版`permission.asked`がproject-local pluginへ配送され、Broker承認後に`once`で再開することを実証する。現状はplugin loadとpermission生成まで確認済みだが配送は未確認。詳細: `docs/opencode_approval_research.md`
- [ ] OpenCode本番コードを変えず、ブラウザにも依存しない検証方法をユーザーと合意してから実装する
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

## fix: AITuber承認通知の機密除外
- [x] `summarize_approval` のテストを先に追加し、機密入力を含めて失敗を確認する
- [x] push通知の要約関数を実装し、Approval Brokerから使用する
- [x] READMEに承認通知の内容制限とresult_arrivedの読み上げ注意を追記する
- [x] 指定pytest・ruff・mypyを実行して差分をレビューする
- [x] 完了結果と未確認事項を報告する（コミットしない）
- 停止条件: 既存tests/test_orchestration.py::test_sdk_server_exposes_three_toolsはMCPツールが3件だけであることを厳密に要求しており、新規2ツール追加と両立しない。既存テスト変更は禁止のため、修正可否の判断が必要。
