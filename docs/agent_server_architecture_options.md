# agent-wrapper サーバー化 — アーキテクチャ比較 (2026-08-31時点の調査)

## 背景・要望

現状の`agent-wrapper`は「CLIから一発起動→単一エージェント(mock/claude/codex)→単一Flaskダッシュボード」というプロトタイプ。
これを次のように拡張したい:

- 常駐サーバー化し、起動後はWeb UIから作業できる
- tmux的に複数shellを並列で走らせ、それぞれでopencode/claudecode/事前設定したharnessを起動する
- システムプロンプトを、既定のまま使うか独自のものを差し込むか、セッションごとに細かく制御する
- 複数shellには`planner`/`worker`/`reviewer`のような役割を事前に割り当てる
- 役割間は直接会話させず、ファイル(将来的にはGitHubも検討)経由でのみ連携させる

このドキュメントは、実装に入る前に「土台をどう作るか」の2方式(+ハイブリッド案)を比較するための資料。**まだどれを採用するか決定していない。**

## 共通要件(方式に依らず必要なもの)

1. 複数セッションを常駐サーバーが並列管理し、Web UIで一覧・個別操作できる
2. harness選択: `mock` / `claude` / `codex` / `opencode`
3. システムプロンプト制御: セッションごとに「既定」または「独自プロンプト注入」
4. 役割メタデータ(`planner`/`worker`/`reviewer`等)をセッションに付与
5. 役割間通信は共有ディレクトリのファイル方式のみ(例: `.agent-server/inbox/<role>.md`)。GitHub連携は次段階
6. 既存資産(`rules.py`の危険操作検知、`ollama_client.py`の一次判定、`SharedState`の承認キュー、`notifier.py`のntfy通知)をどこまで引き継げるか

## 調査で判明した各harnessのネイティブ機能

### Claude Code CLI (`claude`, v2.1.251)

- `--bg`/`--background`: セッションをバックグラウンド起動しidを返す
- `claude agents`: バックグラウンドセッション一覧
- `claude attach <id>` / `claude logs <id>` / `claude stop <id>` / `claude rm <id>` / `claude respawn`: アタッチ・ログ閲覧・停止・削除・再起動
- `--agents '{"reviewer": {"description": "...", "prompt": "..."}}'`: **役割ごとのカスタムシステムプロンプト/ツール制限をJSONで定義**
- `--append-system-prompt <prompt>`: 既定システムプロンプトへの追記
- `--permission-mode <acceptEdits|auto|bypassPermissions|manual|dontAsk|plan>`
- 既存の`ClaudeRunner`(`agent_wrapper/runners/claude_runner.py`)は上記CLIフラグ経由ではなく、`claude-agent-sdk`の`ClaudeAgentOptions`(`can_use_tool`コールバック + `PreToolUse`フック)を直接使う方式。SDKにも`agents: dict[str, AgentDefinition]`(prompt/tools/permissionMode等)と`system_prompt`フィールドがあり、CLIの`--agents`とほぼ同じ概念がSDK側にも存在する。

### Codex CLI (`codex-cli`, v0.149.1)

- `codex app-server daemon` + `codex remote-control`(共に実験的): ローカル常駐デーモン、WebSocket(`ws://`/`wss://`)経由のリモート制御、`capability-token`/`signed-bearer-token`による認証
- `codex agents --remote <addr>`: daemon上の全エージェントセッションを閲覧(リモートTUIとしても接続可能)
- `codex mcp-server`(既存の`CodexRunner`が使用中)の`codex`ツールに`base-instructions`(デフォルト指示の完全置換)・`developer-instructions`(developer roleメッセージとして注入)が存在 → システムプロンプト制御の手段としては使える
- **重大な既知の問題**: `codex mcp-server`自体に非推奨(deprecated)警告が出ている。かつ`approval-policy`のenumが現行スキーマでは`on-request`/`never`のみで、既存`codex_runner.py`が使っている`"untrusted"`は現行バージョンのJSON Schemaに存在しない。**既存の`--agent codex`が現行codexバージョンに対して実際に動くかは別途実機検証が必要**(今回のサーバー化とは独立した既存バグの疑いとして`tasks/todo.md`に切り出す)。

### opencode (`opencode-ai`, v0.5.29)

- `opencode agent`設定(JSON `opencode.json`の`agent.<name>`、またはMarkdown `~/.config/opencode/agents/*.md`): `prompt`(システムプロンプトファイル)、`permission: {read/edit/bash: allow|ask|deny}`、`model`、`temperature`等を役割ごとに定義可能
- `opencode serve`: OpenAPI準拠のHTTPサーバー。`GET /event`(SSE)で権限確認等のイベント配信、`POST /session/:id/permissions/:permissionID`で人間の応答を送信。TUIも同じAPIのクライアントとして動く設計
- ヘッドレス実行(`opencode run --auto`)は「`--auto`で全承認 or 個別に確認」の二択で、外部からの承認仲介はドキュメント上`serve`のAPI経由が本来ルートと推測される
- **既知の環境問題**: このマシンでは`opencode serve`が起動時に`Failed to start server`で毎回失敗する(ポート競合ではないことをnetstatで確認済み、原因未特定)。また、npmのWindows向けグローバルシム(`opencode.cmd`/`opencode.ps1`)が壊れており、実体の`node_modules/opencode-ai/node_modules/opencode-windows-x64/bin/opencode.exe`を直接叩く必要がある

## Option A: 各harnessのネイティブセッション管理を土台にしたオーケストレーション層

### 概要

agent-wrapperは生のptyやプロセス管理を自前で持たず、各CLIが既に持つバックグラウンド/デーモン機能を子プロセスとして起動・監視する「薄いオーケストレーション層」に徹する。agent-wrapper側の責務は:

- セッション作成時のharness別コマンド組み立て(役割→システムプロンプト→CLIフラグの変換)
- 役割メタデータとinboxディレクトリの管理
- 可能な範囲でのollama一次判定+人間承認ゲートの統合
- 複数harnessをまたいだ統一Web UI(一覧・ログ・承認操作)

### harnessごとの実現方針(案)

- **claude**: `claude --bg --agents '<role定義json>' --permission-mode dontAsk` 等で子プロセス起動し、`claude logs <id>`をポーリングしてダッシュボードに流す。**課題**: `--bg`はCLIプロセスとして独立するため、現行`ClaudeRunner`が使うSDKの`can_use_tool`フックがそのまま使えるかは未検証。使えない場合、承認は「`--permission-mode manual`にして`claude attach`させ、人間が直接答える」か、別途ファイルベースで一次判定結果を注入する仕組みが要る。
- **codex**: `codex app-server daemon`を常駐させ、`remote-control`のWebSocketプロトコルでセッション作成・監視。承認要求がプロトコル上どう飛んでくるかは未調査(実験的機能のため公式ドキュメントが薄い可能性が高い)。
- **opencode**: `opencode serve` + SSEの`permission.*`イベント + `POST /session/:id/permissions/:id`で、claude/codexランナーと同じ`_gate()`(rules.check_destructive→ollama判定→人間承認)を統合できる可能性が高い。ただし現環境の起動失敗を先に解決する必要がある。

### メリット

- 各ツールの継続的な進化(worktree分離、resume、respawnなど)にタダ乗りできる
- 自前実装量が少なく、保守コストが低い
- ユーザーが素のCLI(`claude agents`等)を併用しても状態が食い違わない

### デメリット

- 3ツールで機能の成熟度・安定度・APIの粒度がバラバラ(codex/opencodeの該当機能は実験的〜ドキュメント未整備)
- 現行の「rules.check_destructiveで問答無用のエスカレーション」「ollama一次判定」「ntfyプッシュ通知」という一貫した安全モデルを、全harnessで同じ強度で維持できるか不透明。特にclaudeの`--bg`で既存フックが刺さるかが最大の未知数
- 各CLIのマイナーバージョンアップでコマンド仕様が変わるリスクが現実にある(今回codexの`approval-policy`のenumが実際に変わっていたことが判明済み)

## Option B: 自前pty多重化方式(当初案)

### 概要

ConPTY(Windows)をNode.js/Python等から扱うライブラリ(例: `pywinpty`)で各harnessを素の対話モードのまま起動し、Web UI上のxterm.jsに中継する。tmuxのペインのように複数セッションを並べて表示する。役割・システムプロンプトは起動時の引数/環境変数/一時CLAUDE.md等の注入で制御する。

### メリット

- harness非依存で作り方が統一される(claude/codex/opencode/mockすべて同じpty中継の仕組みで扱える)
- 素の対話モードは各CLIのバージョンアップでも比較的壊れにくい
- 実装の見通しが立てやすく、動作も予測しやすい

### デメリット

- 危険操作の自動エスカレーション・ollama一次判定・ntfy通知といった現行の安全機構を、pty越しの出力パースだけで作り直すのは技術的難易度が高く、しかも「人間が直接ターミナルを操作している体験」と「裏で自動介入する」という2つのUXが衝突しやすい
- tmux的な「複数ペインを同時に見る」体験を自分で作り込む必要がある(レイアウト、リサイズ、スクロールバック等)
- 各CLI自身の高度な機能(claudeのworktree分離実行、codexのdaemon経由の永続化・リモート認証など)を使わず、独自に再実装することになる

## ハイブリッド案(C案・検討に値する)

harnessごと・セッションごとに以下の3モードを選べるようにする:

1. **wrapped**: 既存の`ClaudeRunner`/`CodexRunner`と同じSDK/MCPフック方式(ollama一次判定+強制承認あり)。claude/codexで技術的に成立する場合のみ
2. **native-bg**: 各CLIのネイティブバックグラウンド/デーモン機能を薄くラップ(Option A)。承認は「ゆるめ(`--permission-mode acceptEdits`等)」か「ファイル経由の一次判定注入」
3. **raw-pty**: 生の対話ptyをそのままWeb UIに中継(Option B)。opencodeや手動作業向け。承認は完全にCLI自身の対話プロンプトに委ね、ollama/rulesは経由しない

セッション作成時にモードを選択できるようにし、まずopencodeは`raw-pty`、claude/codexは当面`wrapped`(現行資産をそのまま複数並列化)から始めて、`native-bg`は検証が進んでから追加する、という段階導入も可能。

## 未解決の調査ポイント

- [ ] `claude --bg`起動時、現行`ClaudeRunner`が使う`can_use_tool`/`PreToolUse`フック相当のものがCLIフラグ経由で使えるか(使えないなら`--bg`は承認ゲート統合不可)
- [ ] 既存`CodexRunner`の`approval-policy="untrusted"`が現行codex 0.149.1で実際に動くか(動かない場合、現行`--agent codex`は実質壊れている)の実機確認
- [ ] `opencode serve`がこの環境で起動しない原因(Bunランタイムの問題か設定不備か)
- [ ] `codex app-server daemon`のプロトコルで承認要求(exec approval)がリモートクライアントにどう届き、どう応答するか(実験的機能のためドキュメントが薄い)

## 次のアクション候補(未着手・要承認)

1. `claude --bg`の承認フック統合可否を検証する小さなプロトタイプを書く
2. 既存`CodexRunner`が現行codexバージョンで動くかの実機確認(壊れていれば別issueとして切り出す)
3. `opencode serve`起動失敗の原因調査を再開する
4. 上記を踏まえてA/B/Cのどれ(または部分的な組み合わせ)を採用するか決定する
