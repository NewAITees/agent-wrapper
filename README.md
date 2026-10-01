# Agent Wrapper プロトタイプ

Claude CodeやCodexをサブプロセスとして起動し、ollamaによる一次受付を通して、削除系操作や大きな方針決定だけを人間に通知する仕組みの試作。

## セットアップ(最初に1回だけ)

どのプロジェクトのVSCodeターミナルからでも `agent-wrapper` コマンドで起動できるように、uvのtool機能でPATHに登録する。

```bash
cd <このリポジトリのパス>
uv tool install -e .
```

初回はuvが「toolのshimディレクトリをPATHに追加してよいか」を聞いてくる場合がある(`uv tool update-shell` で後からでも追加できる)。インストール後は新しいターミナルで `agent-wrapper --help` が通ることを確認する。コードを変更した場合、`-e`(editable)なので基本的には再インストール不要。依存関係やエントリポイント(`[project.scripts]`)を追加したときだけ `uv tool install -e . --reinstall` で入れ直す。

## サーバー版(複数ターミナルをWebで並列操作)

`agent_wrapper/server.py`(Option B, 自前ConPTY+WebSocket方式。詳細は
`docs/agent_server_architecture_options.md`と`tasks/alignment.md`を参照)は、
claude/codex/opencodeを好きな組み合わせでWeb UI上に並べて動かせる常駐サーバー。

```bash
agent-wrapper-server
```

これだけでブラウザが自動で開き、セットアップ画面(作業フォルダ選択→ターミナル構成→起動)
が表示される。承認ゲート(ollama一次判定・強制人間承認)は未統合で、各harness自身の
対話プロンプトに人間が直接答える方式(raw-pty)。既存の`agent-wrapper`(承認ゲート付き)
とは別系統のツールとして併存している。

## 動かし方

対象プロジェクトのディレクトリに `cd` してから起動する。

```bash
agent-wrapper --agent mock --checkin-interval 10
```

起動すると、ターミナルに次の3つが表示される。

1. このPC上で見る用の `http://localhost:28765/`
2. LAN内のスマホ/Macから見る用のURL(自動検出したLAN IP)
3. そのURLを埋め込んだASCII QRコード — スマホのカメラで読み取ればそのままダッシュボードが開く

疑似エージェントは次の流れを順番に流すようになっている。
1. ollamaが安全と判定して自動承認する権限確認(README確認)
2. ルールエンジンが問答無用でエスカレーションする権限確認(npm install)
3. 削除コマンド(rm -rf)
4. 停止要求(::REQUEST_STOP::)

1はollamaが動いていれば人間の操作なしで自動承認され、ログに「(ollama自動承認: ...)」と出る。2〜4ではそれぞれ画面が「承認待ち」に変わることを確認できる。

## LAN内のスマホ/Macから見るためのファイアウォール設定

Windows Defenderファイアウォールは初期状態では外部からの新規ポートへの接続をブロックする。**管理者権限のPowerShell**で以下を一度実行しておく(このリポジトリのAIエージェント作業では管理者権限を持たないため、これは手動で実行する必要がある)。

```powershell
netsh advfirewall firewall add rule name="AgentWrapperDashboard" dir=in action=allow protocol=TCP localport=28765
```

ポート `28765` は一般的な開発用ポート(3000/5000/8000/8080等)と衝突しにくいよう、動的/私設ポート域に近い値を選んである。設定後、スマホ/MacとこのPCが同一LAN(同じWi-Fi)にいる状態で、表示されたQRコードを読み取るかURLを直接開いて疎通を確認すること。

## ntfyによるプッシュ通知

`notifier.py` は `--ntfy-topic`(または環境変数 `AGENT_WRAPPER_NTFY_TOPIC`)が指定されていれば、[ntfy.sh](https://ntfy.sh) 経由でMac/iPad/Androidに通知を送る。

**注意**: ntfy.shの公開トピックは、トピック名さえ知っていれば誰でも購読できてしまう(トピック名が実質的な合言葉になる)。他人に推測されない、十分長いランダムな文字列を使い、**このリポジトリ(README.mdやコード)には実際のトピック名を書き込まないこと**。git管理外の場所(環境変数、または `.gitignore` 済みのローカル設定)に置く。

セットアップ手順:
1. 自分専用のトピック名を生成する
   ```bash
   python -c "import secrets; print('agentwrap-' + secrets.token_urlsafe(16))"
   ```
2. スマホ/Macに ntfy アプリ(iOS/Android/Web: https://ntfy.sh )をインストールし、生成したトピック名を「購読(Subscribe)」する
3. 環境変数に設定してから起動する(シェルのプロファイルに書いておくと毎回入力しなくて済む)
   ```bash
   export AGENT_WRAPPER_NTFY_TOPIC="<自分で生成したトピック名>"
   agent-wrapper --agent claude --prompt "READMEを読んで要約して"
   ```
   または毎回 `--ntfy-topic <トピック名>` を付けてもよい。

優先度は破壊的操作/停止要求が urgent(5、音あり)、権限確認/方針決定が default(3)、定期チェックインが min(1、サイレント)にマッピングされる。

## 実際のClaude Codeに繋ぐとき(`--agent claude`)

`--agent mock`/`--agent codex` はサブプロセス+標準出力の文字列解析(`wrapper.py`)で動くが、
`--agent claude` だけは仕組みが異なり、公式の `claude-agent-sdk` 経由で実際の `claude` バックエンドに
接続する(`agent_wrapper/runners/claude_runner.py` の `ClaudeRunner`)。認証は既存の `claude` CLIログイン
(Claude Codeサブスクリプション)をそのまま使う(`ANTHROPIC_API_KEY` は不要、従量課金にはならない)。

```bash
agent-wrapper --agent claude --prompt "READMEを読んで要約して"
# または
agent-wrapper --agent claude --prompt-file ./task.md
```

`--prompt` か `--prompt-file` のどちらかが必須(未指定だとエラーで起動しない)。ヘッドレスで一度きりの
指示を渡す形式で、CLAUDE.md・AGENTS.mdに `::REQUEST_PERMISSION::`/`::REQUEST_STOP::` のようなマーカーを
書いてもらう必要は**ない**(このマーカー方式は `--agent mock` 専用)。`--agent claude` では次の仕組みで
自動的に全てのツール呼び出しを横取りする:

1. `Bash`/`Write`/`Edit` の呼び出しは、Claude自身の判断に関わらず必ず `PreToolUse` フックで捕捉される
2. `rules.check_destructive()` に一致すれば、`ollama_client.explain_operation()` で内容を説明した上で必ず人間の承認待ちになる(ollamaの判定は経由しない)
3. 一致しなければ `ollama_client.judge_permission()` の一次判定を経て、安全ならそのまま続行、そうでなければ同様に人間の承認待ちになる
4. Claudeがツール呼び出しではなく会話文で「進めてよいですか」のように尋ねてきた場合も、`ollama_client.judge_conversational_question()` が内容を読んでALLOW/ESCALATEを判定し、危険そうならここでも人間の承認待ちになる(内容を評価せず機械的に「y」と答えるわけではない)

### パッケージインストール・外部コード取得は常に人間確認

`npm install` / `pip install` / `uv add` / `cargo install` / `git clone` / `docker pull` / `docker run` などは、
`rules.py` の `DESTRUCTIVE_PATTERNS` に含めてあるため、`--agent mock` では `::REQUEST_PERMISSION::` の自己申告
経由でも、`--agent claude` では `PreToolUse`/`can_use_tool` 経由でも、ollamaの一次判定を経由せず`rm -rf` などと
同じく問答無用で人間の承認待ちになる。ウイルスや信頼できないコードのインストール・取得を自動承認させないための
安全弁で、新しいパッケージマネージャに対応させたい場合はここにパターンを足す。

## 実際のCodexに繋ぐとき(`--agent codex`)

`--agent codex` は `codex mcp-server` をMCPクライアントとして起動する方式で動く
(`agent_wrapper/runners/codex_runner.py` の `CodexRunner`)。認証は既存の `codex` CLIログイン
(ChatGPTサブスクリプション)をそのまま使う。

```bash
agent-wrapper --agent codex --prompt "READMEを読んで要約して"
```

`--agent claude` と同様に `--prompt` か `--prompt-file` が必須。仕組み:

1. codexが承認の必要な操作(シェルコマンド実行、apply_patchによるファイル変更)を行おうとするたびに、
   MCP elicitation(承認要求)がラッパーに構造化データとして届く(`approval-policy=untrusted` を指定)
2. 以降はclaude側と共通のゲート(`runners/base.py`): `rules.check_destructive()` 一致→説明つきで必ず人間の
   承認待ち / 不一致→`judge_permission()` 一次判定→安全なら続行、そうでなければ人間の承認待ち
3. 会話文での確認質問にも `judge_conversational_question()` で一次受付する(claude側と同じ)

### OSサンドボックス(--codex-sandbox)

既定は `danger-full-access`(サンドボックスなし)。**承認ゲート(全コマンドの実行前承認)は
この設定と無関係に常に有効**で、これが一次防壁。サンドボックスは承認後の補助壁だが、
Windowsでは読み取りすら誤ブロックする不安定さに加え、サンドボックス起因の失敗を
昇格付きで再実行する承認要求がmcp経由で届かない上流バグ(openai/codex#21982系統)が
あるため、既定オフとした(経緯と判断: docs/agent_wrapper_permission_policy.md 8節)。
信頼できないコードを扱うタスクでは `--codex-sandbox workspace-write` を指定すること。

注意: `codex exec`(非対話モード)は承認ポリシーを強制的に無効化するためこの用途には使えない
(実機検証済み)。また、codexが送る独自通知(`codex/event`)がMCP SDKの型検証を通らず警告ログが
出ることがあるが、動作には影響しない。詳細は `docs/agent_wrapper_sdk_integration_plan.md` 3.2節を参照。

## ollamaとの連携

`ollama_client.py` は `http://localhost:11434` にリクエストを送る前提になっている。手元でollamaが起動していて、`--ollama-model` で指定したモデル(既定は `gemma4:e4b`)がpull済みであれば動く。モデルに接続できない場合は、安全側に倒れて人間へのエスカレーションになるようにしてある。

## 既知の制限

- `wrapper.py` の `SharedState.approve_event` は単一の `threading.Event` なので、権限確認待ちと定期チェックインの`major_decision`判定が同時にwaiting_humanになると、1回の承認が両方を同時に解除してしまう競合状態がある(`--agent mock`)。デフォルトの `--checkin-interval 600`(10分)なら起きにくいが、短い間隔で使うと再現しうる。
- `--agent claude`(`ClaudeRunner`)も同種の制限を引き継いでいる: 承認待ちは1件のみを想定しており、複数のツール呼び出し/会話確認が同時にaskへ倒れた場合、片方への承認がもう片方の待機も解除してしまう可能性がある。
- `--agent claude` は `setting_sources=["project"]` を指定しているが、動作確認したところこの操作者個人のグローバル `~/.claude/CLAUDE.md` が(理由未確定のまま)ラップ対象のセッションに混入することがある。これ自体はエージェントが会話文で確認を求めてくる原因になるだけで、実際のツール実行の安全性(`PreToolUse`/`can_use_tool`)には影響しないが、根本原因は未解決(`docs/agent_wrapper_sdk_integration_plan.md` 参照)。
- 定期チェックインの「大きな方針決定かどうか」の判定精度は、実際のログで試しながらプロンプトを調整していく必要がある。

## AITuber連携(push)

`AGENT_WRAPPER_AITUBER_URL` にAITuberの受信先(例: `http://127.0.0.1:18767/event`)を設定すると、agent-wrapperは次のイベントをバックグラウンド送信します。未設定の場合は送信しません。任意の `AGENT_WRAPPER_AITUBER_TOKEN` を設定すると `X-Aituber-Token` ヘッダで送信します。

承認要求の内容(コマンドの引数・パス全体・URL・秘密情報)は送らず、送るのは操作の種類と先頭の1語のみです(配信での読み上げによる漏洩防止)。

- `approval_pending`: 新しい承認要求
- `session_stopped`: セッション終了
- `result_arrived`: `.agent-server/inbox/` 内のファイル新規作成・更新(ファイル名は読み上げ対象になりうる)

AITuber側は `POST /event` で `{"type":"...","session":"...","message":"..."}` を受信します。`message` は最大300文字です。受信側で `AITUBER_PUSH_TOKEN` を設定している場合は同じ値を `X-Aituber-Token` で渡す必要があり、成功応答は `202` です。

送信はタイムアウト3秒のバックグラウンド処理です。HTTPエラーや接続失敗はwarningに記録し、承認・セッション管理には影響しません。この通知は承認ゲートの代替ではありません。
