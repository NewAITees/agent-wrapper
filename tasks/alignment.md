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
