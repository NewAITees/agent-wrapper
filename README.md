# Agent Wrapper プロトタイプ

Claude CodeやCodexをサブプロセスとして起動し、ollamaによる一次受付を通して、削除系操作や大きな方針決定だけを人間に通知する仕組みの試作。

## セットアップ(最初に1回だけ)

どのプロジェクトのVSCodeターミナルからでも `agent-wrapper` コマンドで起動できるように、uvのtool機能でPATHに登録する。

```bash
cd C:\analysis2\play_ground
uv tool install -e .
```

初回はuvが「toolのshimディレクトリをPATHに追加してよいか」を聞いてくる場合がある(`uv tool update-shell` で後からでも追加できる)。インストール後は新しいターミナルで `agent-wrapper --help` が通ることを確認する。コードを変更した場合、`-e`(editable)なので基本的には再インストール不要。依存関係を追加したときだけ `uv tool install -e . --reinstall` で入れ直す。

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
   agent-wrapper --agent claude
   ```
   または毎回 `--ntfy-topic <トピック名>` を付けてもよい。

優先度は破壊的操作/停止要求が urgent(5、音あり)、権限確認/方針決定が default(3)、定期チェックインが min(1、サイレント)にマッピングされる。

## 実際のClaude Code / Codexに繋ぐとき

`main.py` の `AGENT_COMMANDS` にある `claude` と `codex` のコマンドは仮のものなので、お使いのバージョンに合わせて調整してほしい。ヘッドレスモード(`claude -p`)を前提にしているが、対話モードで動かしたい場合は標準入出力のやり取りが変わるため、`wrapper.py` の `_send_to_stdin` まわりを見直す必要があると思う。

エージェント自身に意思表示させるための合図は、CLAUDE.md・AGENTS.mdに次のようなルールとして書いておくとよい。

```text
権限確認が必要な操作の前には、行の先頭に「::REQUEST_PERMISSION:: 」を付けて内容を一行で書くこと。
設計判断や不可逆な選択について相談したいときは、「::REQUEST_STOP:: 」を付けて内容を書き、
人間の回答を待つこと。
```

### パッケージインストール・外部コード取得は常に人間確認

`npm install` / `pip install` / `uv add` / `cargo install` / `git clone` / `docker pull` / `docker run` などは、
`rules.py` の `DESTRUCTIVE_PATTERNS` に含めてあるため、エージェントが `::REQUEST_PERMISSION::` で自己申告しても
ollamaの一次判定を経由せず、`rm -rf` などと同じく問答無用で人間の承認待ちになる。ウイルスや信頼できないコードの
インストール・取得を自動承認させないための安全弁で、新しいパッケージマネージャに対応させたい場合はここにパターンを足す。

## ollamaとの連携

`ollama_client.py` は `http://localhost:11434` にリクエストを送る前提になっている。手元でollamaが起動していて、`--ollama-model` で指定したモデル(既定は `gemma4:e4b`)がpull済みであれば動く。モデルに接続できない場合は、安全側に倒れて人間へのエスカレーションになるようにしてある。

## 既知の制限

- `wrapper.py` の `SharedState.approve_event` は単一の `threading.Event` なので、権限確認待ちと定期チェックインの`major_decision`判定が同時にwaiting_humanになると、1回の承認が両方を同時に解除してしまう競合状態がある。デフォルトの `--checkin-interval 600`(10分)なら起きにくいが、短い間隔で使うと再現しうる。
- 定期チェックインの「大きな方針決定かどうか」の判定精度は、実際のログで試しながらプロンプトを調整していく必要がある。
