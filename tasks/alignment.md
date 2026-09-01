# Alignment - 用語・概念の共有定義

このファイルはユーザーとAIの間で用語・概念の解釈を統一するためのもの。
AIは全セッションでこのファイルを参照し、解釈のずれを防ぐこと。

---

## Terms（事前定義）

### harness
- **意味**: agent-wrapperが起動・管理する対象のAIコーディングツール本体。現状は`mock`/`claude`/`codex`、拡張検討中に`opencode`を追加。
- **NG解釈**: agent-wrapper自身をharnessと呼ぶ(誤り、agent-wrapperはharnessを包む側)。
- **OK解釈**: 「どのharnessを使うか」＝「claude/codex/opencodeのどれを子プロセス/子セッションとして起動するか」。

### role（役割）
- **意味**: セッションに事前割り当てする作業上の立場。例: `planner`(計画立案)/`worker`(実装)/`reviewer`(レビュー)。
- **NG解釈**: roleがharnessを決める(誤り、roleとharnessは独立の軸。例えばplanner役をclaudeでもcodexでも担わせられる)。
- **OK解釈**: roleは主にシステムプロンプトの差し込み内容とinboxディレクトリの読み書き先を決めるメタデータ。

### inbox（共有ディレクトリ通信）
- **意味**: role間の連携を、直接の会話やAPI呼び出しではなく、共有ディレクトリ内のファイルの読み書きだけで行う規約。例: `.agent-server/inbox/<role>.md`。
- **NG解釈**: GitHub Issue/PR経由の連携も含む(誤り、現時点のスコープはファイル方式のみ。GitHub連携は将来の拡張候補として別途検討)。
- **OK解釈**: 「plannerがworker向けのタスクをinboxファイルに書き、workerがそれを読んで実行し、結果を別のinboxファイルに書く」という一方向的なファイルベースのやり取り。

### wrapped セッション
- **意味**: 既存の`ClaudeRunner`/`CodexRunner`のように、SDK(`claude-agent-sdk`)やMCP(`codex mcp-server`)のフック機構を通じて、agent-wrapperが全ツール呼び出しを横取りし、`rules.check_destructive`→ollama一次判定→人間承認という3層ゲートを必ず通す方式のセッション。
- **NG解釈**: CLIのバックグラウンド機能(`claude --bg`等)を使えばwrappedになる(誤り、`--bg`はCLIネイティブのプロセス管理であり、SDKフックとは別物。フックが刺さるかは別途検証が必要)。
- **OK解釈**: wrappedは「agent-wrapperが安全性を保証できる」セッション種別。

### native-bg セッション
- **意味**: 各CLIが標準で持つバックグラウンド/デーモン機能(`claude --bg`+`claude agents`/`attach`/`logs`/`stop`、`codex app-server daemon`+`remote-control`、`opencode serve`)をagent-wrapperが薄くラップして使う方式のセッション。
- **NG解釈**: native-bgもwrappedと同じ強度でollama一次判定・強制エスカレーションが効く(未確認。CLIごとに承認フック統合の可否が異なる可能性が高い)。
- **OK解釈**: native-bgは実装コストが低い代わりに、安全機構の統合レベルがharnessごとに異なりうる方式。

### raw-pty セッション
- **意味**: ConPTY等で各harnessを素の対話モードのまま起動し、Web UI(xterm.js等)にキーストローク/出力をそのまま中継する方式。承認はCLI自身の対話プロンプトに人間が直接答える。
- **NG解釈**: raw-ptyでもollama自動承認やrules.check_destructiveによる強制エスカレーションが効く(誤り、raw-ptyはこれらを経由しない設計)。
- **OK解釈**: raw-ptyは安全機構より「とにかくそのツールを素の状態で使いたい」場合向け(例: opencodeのTUIをそのまま使う)。

### Approval Broker（承認ブローカー）
- **意味**: 各harness固有の構造化承認イベントを共通`ApprovalRequest`へ正規化し、機械ルール、utility AI、人間の順で裁定して応答を元のharnessへ返す中央コンポーネント。
- **NG解釈**: orchestrator LLM自身にOS権限や承認APIを直接持たせ、自由文だけで無制限に許可させる。
- **OK解釈**: orchestratorは作業配分と説明整理を担当し、最終的な許可能力はポリシー制約付きApproval Brokerだけが持つ。

### permission request / specification question
- **意味**: `permission request`は具体的な副作用操作の実行可否、`specification question`は仕様・優先順位・スコープを決める意味判断。別のイベント種別・承認規則で扱う。
- **NG解釈**: どちらも画面上の`y/n`らしい文字列として同じ自動承認ロジックへ渡す。
- **OK解釈**: 操作権限はtool/action/resourceを根拠に判定し、仕様質問は選択肢・影響・推奨案を整理して、委任済み範囲外なら人間へ上げる。

### Approval Broker 実装対応 / Implementation Mapping
- **役割 / Responsibility**: 複数セッションの構造化承認要求をUUID単位で保持し、一度きりの応答、期限切れ、キャンセルを管理する。
- **親 / Parent**: agent-wrapper server
- **含むもの / Contains**: 共通要求・応答、セッション別pending、harness adapter境界、HTTP一覧・応答API。
- **実装 / Implementation**:
  - Components: `ApprovalBroker`, `SharedState`, `OpenCodePermissionAdapter`
  - Files: `agent_wrapper/approval.py`, `agent_wrapper/approval_adapters.py`, `agent_wrapper/wrapper.py`, `agent_wrapper/server.py`
  - State: `pending / resolved / expired / cancelled`
  - API: `GET /approvals`, `POST /approvals/{request_id}/respond`
- **指示に使える表現 / Human Labels**: 承認ブローカー、中央承認キュー、一次受付
- **曖昧になりやすい表現 / Ambiguous Labels**: orchestrator(作業配分役であり、Brokerそのものではない)

### 実行・承認画面 / Runtime and Approval Screen
- **役割 / Responsibility**: 人間の最終判断と複数AIセッションの操作状況を、同じ画面で構造的に提示する。
- **親 / Parent**: 画面・操作 / Frontend
- **含むもの / Contains**: 人間承認レール、オーケストレーター端末、役割別端末グリッド。
- **画面上の位置・利用者からの見え方 / Human View**:
  - Desktop: 左に人間承認レール、右上にオーケストレーター、右下に4端末を2行2列。
  - Mobile: 人間承認レール、オーケストレーター、役割別端末の順に縦積み。
- **実装 / Implementation**:
  - Components: `approval-rail`, `orchestrator-stage`, `role-terminal-grid`
  - Files: `agent_wrapper/server_static/index.html`
  - State: Approval Brokerのpending要求、PTYセッション一覧
  - API: `GET /approvals`, `POST /approvals/{request_id}/respond`, WebSocket PTY
- **指示に使える表現 / Human Labels**: 左の承認フロー、人間の判断、上のオーケストレーター、下の4ターミナル、2行2列
- **曖昧になりやすい表現 / Ambiguous Labels**: ターミナル領域（オーケストレーターを含むか明示する）

### harness承認窓口の現在の検証状態
- **Claude**: 現行CLI `2.1.251` の通常の`--help`では`--permission-prompt-tool`を確認できなかった。実装は検証済みのAgent SDK `PreToolUse/can_use_tool`方式を正本とする。role制限はSDKの`allowed_tools`へ渡さない（一致操作が`can_use_tool`を迂回するため）。PreToolUseで明示拒否し、通過した操作も必ずrules→Ollama→人間の共通ゲートへ送る。planner/reviewerはMarkdownだけ書き込み例外とする。
- **Codex**: 既存MCP方式には非推奨警告・enum変更リスクが残る。現行app-serverの実機検証が終わるまで移行しない。
- **OpenCode**: 1.18.25でproject-local pluginのロードと`bash: ask`による内部permission要求生成を確認済み。`opencode run`は要求を即時auto-rejectし、TUIの簡易ConPTY検証は端末能力照会に応答できず入力未成立だったため、`permission.asked`のplugin配送とBroker往復は未確認。UIも「Broker未検証」と表示する。OpenCode内部の`ses_...` IDと画面上のrole名は別物なのでpluginが両方を送り、SessionManagerが対応を保持して停止時に内部ID側のpendingもキャンセルする。`tool.execute.before`への置換やOpenCode権限の一律`allow`化は採用していない。詳細: `docs/opencode_approval_research.md`。

---

## Misalignment Log（事後記録）

### 2026-08-31「あぁそれだとだめじゃないかな / ウェブで操作することを想定してるし / 色々と動かないところがあるから今回作ってるんだし」
- **ユーザーの意図**: サーバー化はWeb UIからの操作を大前提としており、かつ各harness(claude/codex/opencode)の公式自動化窓口(daemon/serve/mcp-server)自体が不安定・実験的・破壊的変更されがちだからこそ、agent-wrapperという自前のラッパーを作る意味がある。
- **AIの誤った解釈**: docs/agent_server_architecture_options.md のOption A(各CLIのネイティブbg/daemon機能を土台にする)を「車輪の再発明を避けられる有力案」として提示した。しかしネイティブ機能(`claude attach`/`claude agents`等)はローカル端末前提のCLIであり、Web UIから使うには結局こちらで橋渡し層を書く必要がある上、その橋渡し対象(codex app-server/opencode serve/codex mcp-server)自体が実験的・非推奨・起動不良と不安定要素だらけで、依存先として不適切だった。
- **正しい理解**: 主軸はOption B(自前ConPTY+WebSocket+xterm.js的なWebネイティブのpty中継)。既存の`ClaudeRunner`/`CodexRunner`(SDK/MCPフック方式)は、技術的に安定して動く範囲でのみ「高セキュリティ自動化モード」として別途残す。各harnessの実験的daemon/serve機能を安全機構やセッション管理の土台には据えない。
- **今後の対処**: 今後の設計提案では「Web UIから直接操作できるか」「依存先が公式に安定サポートされているか(experimental/deprecated表記がないか)」を最初のフィルタとして必ずかける。[[agent-server-design]]

### 2026-08-31「そういえば基本コードの実装はopencode/chatgptにやらせて TOKENもったいないので」
- **ユーザーの意図**: トークン(Claude利用コスト)を節約するため、基本的なコード実装作業は(agent-wrapperで統合中の)opencode+ChatGPTバックエンドに担当させ、Claude(私)は設計・計画・レビュー・仕上げ的な小作業に集中してほしい。
- **AIの誤った解釈**: なし(明確な今後の方針指示)。ただし進行中だった小さな仕上げ作業(server.py統合の残り: pyprojectエントリポイント・旧prototype削除・ruff/mypy・簡単なテスト)は、ユーザー確認の上でこの回はClaudeが完結させた。
- **正しい理解**: 次に新しい実装タスクが発生したら、まずopencode(またはcodex等の他ハーネス)に振れないか検討し、Claudeは丸ごと自分で書く前に一度その選択肢を提示する。
- **今後の対処**: 実装量がまとまったタスクを始める前に「これはopencodeに振るべきか」を自問し、判断が割れる場合はユーザーに確認する。

### 2026-09-01「今実装しないといけないものって何かない？todoから拾ってきて実際に実装させてみて」
- **ユーザーの意図**: 「実装させてみて」は使役形であり、「(opencode等に)実装させる」という意味。上記2026-08-31の方針(基本実装はopencodeに振る)の再確認・具体的な適用指示だった。
- **AIの誤った解釈**: 「させてみて」を字面通り「(自分が)試しに実装してみて」と誤読し、`/run-tests`(機械的テスト実行API)をClaude自身が直接実装してしまった。ユーザーから「さっき私はtodoから適当な項目でまだ実装されてないものを実装をこっちに依頼してって言ったよね」と指摘されて発覚。
- **正しい理解**: 日本語の使役形「〜させて」「〜させてみて」は、話者自身ではなく第三者(この文脈ではopencode)に行為を行わせる指示。実装系のタスクでこの表現が出たら、実装主体は自分ではなくopencode等の委譲先だと解釈する。
- **今後の対処**: 「実装させて」「作らせて」等の使役表現を見たら、まず委譲先(opencode)への依頼だと仮定する。すでに自分で実装してしまった場合は、動作検証済みなら無理に作り直さずユーザーに確認する(今回は既存実装を残す判断になった)。
