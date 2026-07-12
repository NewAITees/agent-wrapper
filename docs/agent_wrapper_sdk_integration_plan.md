# agent_wrapper: SDK統合による権限確認の作り替え 計画書

作成日: 2026-07-08(最終更新: 2026-07-09)
対象: `agent_wrapper/`(AIエージェント・ラッパー プロトタイプ)
ステータス: Phase 1〜4完了。`--agent claude`(claude-agent-sdk)・`--agent codex`(codex mcp-server + MCP elicitation)とも実機動作確認済み。

## 1. 背景

### 1.1 今のアーキテクチャ

`agent_wrapper/wrapper.py` の `AgentWrapper` は、`claude`/`codex`/`mock` のいずれであっても共通の仕組みで動く設計になっている。

1. `subprocess.Popen(cmd, stdout=PIPE, stdin=PIPE, ...)` でエージェントを起動する
2. `_read_loop` スレッドが標準出力を1行ずつ読み、`rules.check_destructive()` / `rules.parse_agent_signal()` に通す
3. 破壊的パターンに一致するか、`::REQUEST_STOP::`/`::REQUEST_PERMISSION::` マーカーがあれば、`SharedState` を `waiting_human` にして `_wait_for_approval()` で `threading.Event` を待つ
4. `::REQUEST_PERMISSION::` の場合は `ollama_client.judge_permission()` に一次判定させ、ALLOWなら `stdin` に `"y\n"` を書き込んで自動継続する
5. ダッシュボード(`dashboard.py`)の承認ボタンで `approve()`/`deny()` が呼ばれ、`stdin` に `"y\n"`/`"n\n"` を書き込み、`Event.set()` する

この設計は `mock_agent.py`(単純にprint/readlineするだけの疑似エージェント)には完全に機能する。しかし実際の `claude`/`codex` CLIに対しては機能しないことが判明した。

### 1.2 実際に試して分かった問題

今回のセッションで、実機検証により以下が判明した。

- `claude -p "<prompt>" --dangerously-skip-permissions` は動くが、`--dangerously-skip-permissions` を付けるとClaude自身の権限確認が完全に無効化されるため、**このラッパーが横から判定したい「権限確認」自体が発生しなくなる**。安全網はルールエンジンの正規表現と、エージェントが自発的に出す `::REQUEST_PERMISSION::`/`::REQUEST_STOP::` マーカー(CLAUDE.md経由でエージェントに指示する必要がある、LLMの追従性に依存する弱い仕組み)だけになる。
- `--dangerously-skip-permissions` を外すと、Claudeは権限確認が必要な場面で**プレーンテキストで確認を求めるが、stdinからの入力を待たずにそのままプロセスが終了する**(実機で確認済み: `jobs`/`tasklist`のいずれにもプロセスが残っていなかった)。つまりヘッドレス(`-p`)モードでは、単純なstdin/stdoutのやり取りでは人間の承認を待つ仕組みそのものが成立しない。
- `--input-format stream-json --output-format stream-json --permission-prompt-tool stdio` という、権限確認を構造化JSON(`sdk_control_request`/`control_response`)でやり取りする本来のプロトコルが存在する(Anthropic公式ドキュメント: <https://code.claude.com/docs/en/agent-sdk/permissions>)。ただし実際に試したところ、プロンプトをCLI引数で渡したままこのモードにすると、初期のユーザーメッセージをstdin経由でJSON形式で送信する必要があるらしく、それをしないとプロセスがハングしたままになることを実機で確認した(4行の`system`/`hook_*`イベントが出た後、何も進まなくなった)。
- `--permission-prompt-tool` はこのマシンにインストールされているバージョンの `claude --help` には出てこない(ドキュメントと実装にズレがある可能性。Claude Code は更新が非常に速いため、都度確認が必要)。

### 1.3 結論

生のstdin/stdout(テキストでもJSON Linesでも)を自前でハンドシェイクするのは、Anthropic公式の `claude-agent-sdk` Python パッケージが既にやっていることの再発明であり、CLIの仕様変更に追従し続けるメンテナンスコストが高い。**公式SDKの `canUseTool` コールバックを使う方式に作り替えるべき**、というのが今回のセッションでの結論。

## 2. 目標

- `--dangerously-skip-permissions` 相当のフラグを使わずに、Claude/Codexそれぞれの権限確認を横取りする
- 横取りした権限確認の中身(ツール名・引数)を、既存の `rules.check_destructive()` と `ollama_client.judge_permission()` にそのまま(または軽い変換をかけて)渡し、判定ロジック自体は再利用する
- ALLOWでない場合は、既存の `SharedState`/ダッシュボード/ntfy通知の仕組みで人間に確認を求め、承認/却下をコールバックの戻り値として返す
- `mock` エージェントでの動作確認(pytest/手動デモ)は今まで通り、subprocess+正規表現の経路を維持してよい(SDK不要な軽量な動作確認として引き続き有用)

## 3. 調査結果: 各SDKの実際のAPI形状

### 3.1 claude-agent-sdk (Python, 安定版寄り)

- PyPI: `claude-agent-sdk` (確認時点で 0.2.99 まで存在、`requires-python >=3.10`)
- SDKは内部で `claude` CLIをサブプロセスとして起動し、stdin/stdoutで制御プロトコル(JSON Lines)をやり取りする。**つまり課金/ログインは通常の`claude` CLIと同じ(Claude Code サブスクリプション)経路になるはず** — ただし確認済みではないので、実装に着手する際に最初に検証すること(Anthropic APIの従量課金に切り替わってしまうと運用コストが変わる)。

具体的なAPI(Anthropic公式ドキュメント <https://code.claude.com/docs/en/agent-sdk/python> より):

```python
from claude_agent_sdk import query, ClaudeAgentOptions, ClaudeSDKClient
from claude_agent_sdk.types import (
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
    AssistantMessage,
    TextBlock,
    ToolUseBlock,
)

async def can_use_tool(
    tool_name: str,
    input_data: dict,
    context: ToolPermissionContext,
) -> PermissionResultAllow | PermissionResultDeny:
    # ここに rules.check_destructive() 相当のチェック + ollama_client.judge_permission() を差し込む
    ...
    return PermissionResultAllow(updated_input=input_data)
    # または
    return PermissionResultDeny(message="...", interrupt=False)

options = ClaudeAgentOptions(
    permission_mode="default",      # bypassPermissions/dontAskは使わない(canUseToolを必ず通す)
    cwd="/path/to/target/project",  # ラップ対象のプロジェクトディレクトリ
    can_use_tool=can_use_tool,
)

async for message in query(prompt="タスク内容", options=options):
    if isinstance(message, AssistantMessage):
        for block in message.content:
            if isinstance(block, TextBlock):
                ...  # ここで state.append_log() 相当、::REQUEST_STOP::的な自由記述の検知もできる
            elif isinstance(block, ToolUseBlock):
                ...  # ツール呼び出しのログ表示用
```

重要な仕様:
- `can_use_tool` コールバックは、`allowed_tools` や `permission_mode` (`acceptEdits`/`bypassPermissions`など)で自動承認された呼び出しには**呼ばれない**。`permission_mode="default"` にして `allowed_tools` を空(または最小限)にしておけば、ほぼ全てのツール呼び出しでコールバックが呼ばれるはず(要検証)。
- `PermissionResultAllow(updated_input=...)` で引数を書き換えて承認することもできる(今回は使わない想定だが、将来的に「危険な引数だけ削って許可する」といった拡張も可能)。
- `PermissionResultDeny(message=..., interrupt=True)` で、却下と同時にセッション自体を止めることもできる。

### 3.2 openai-codex (Python, ベータ版)【調査完了・2026-07-09】

#### 3.2.1 Python SDK(`openai-codex` パッケージ)には承認コールバックが存在しない

- PyPI: `openai-codex` (2026-07-09時点でも **0.1.0b3、まだベータ**、変化なし)
- 隔離venv(`uv venv` + `uv pip install --prerelease=allow`)に実際にインストールし、`dir()`/`inspect.signature()` でパッケージ本体を直接調査した(ドキュメントの記載が薄く、二次情報を信用できなかったため)。
  ```python
  from openai_codex import Codex, Sandbox

  with Codex() as codex:
      thread = codex.thread_start(model="gpt-5.4")
      result = thread.run("タスク内容")
  ```
- **結論: `claude_agent_sdk` の `can_use_tool`/`PreToolUse` に相当する、個別ツール呼び出しごとの承認コールバック機構は一切存在しない。**
  - `ApprovalMode` は `deny_all` / `auto_review` の2値のみ(スレッド/ターン開始時に一括指定するだけで、実行中の動的な判定は不可)
  - `TurnHandle.stream()` が返す `Notification` の全種類(`openai_codex.models` の29種)を確認したが、`Approval`/`Review`/`Grant`/`Deny`/`Reject`/`Permission`/`Ask` に該当する型が**1つも存在しない**(進捗通知・出力デルタ・エラー等の観測用イベントのみ)
  - `Codex`/`CodexConfig` のコンストラクタにもコールバック的な引数はない
  - つまりPython SDK経由では、`deny_all`(一律拒否、個別に見直す手段なし)か `auto_review`(内部モデルによる自動判定、外部から介入不可)の二択しかなく、どちらも「危険なら人間に聞く」という本プロジェクトの要件を満たせない。
- **注意**: `--prerelease=allow` を付けた `uv run --with`/`uv pip install` は、対象プロジェクトの `uv.lock` を意図せず書き換えることがある(実際に発生し、`git checkout -- uv.lock` で復元した)。この種の一時的なパッケージ調査は、対象リポジトリの外の隔離venv(`uv venv <name>` を別ディレクトリに作り、そこに `uv pip install --python <venv>/bin/python ...`)で行うこと。

#### 3.2.2 Codex CLI自体には `PermissionRequest` フックが存在する(Python SDKとは別物)

WebSearchで見つけた情報がClaude Codeのフック仕様(`permission_mode` の値等)と酷似していたため幻覚を疑い、`curl` で生HTMLを直接取得し文字列検索で検証した(<https://developers.openai.com/codex/hooks>)。結果、**本物であることを確認した**。

- Codex CLI(`codex` コマンド自体、ベータPython SDKとは独立)には、`hooks.json`(または `config.toml` のインライン `[hooks]` テーブル)で登録できる `PermissionRequest` フックが存在する。配置場所は `~/.codex/hooks.json`、`<repo>/.codex/hooks.json` など。
- フックは**外部コマンド(スクリプト)として登録**し、Codexが標準入力でJSONを渡す(`tool_name`、`tool_input`(`Bash`/`apply_patch`は`command`、MCPツールは全引数)、`turn_id`、`permission_mode` 等)。フック側は標準出力に次のJSONを返す:
  ```json
  {
    "hookSpecificOutput": {
      "hookEventName": "PermissionRequest",
      "decision": { "behavior": "allow" }
    }
  }
  ```
  または
  ```json
  {
    "hookSpecificOutput": {
      "hookEventName": "PermissionRequest",
      "decision": { "behavior": "deny", "message": "Blocked by repository policy." }
    }
  }
  ```
  複数フックが一致した場合、1つでもdenyなら拒否。何も決定を返さなければCodexの通常の承認フローに委ねられる(非対話実行(`codex exec`)でこれがハングするか拒否になるかは未検証)。
- `matcher` は `tool_name` に対して適用され、`Bash`・`apply_patch`(`Edit`/`Write`のエイリアスも可)・MCPツール名(`mcp__server__tool`)に対応。
- **重要な制約**: 非管理下(non-managed)のフックは、初回実行前にCLIの `/hooks` コマンドで人間が明示的に信頼(trust)する必要がある。信頼はフック定義のハッシュに対して記録されるため、フックスクリプトを変更するたびに再度信頼付与が必要になる。CLIには信頼確認を省略する `--dangerously-bypass-hook-trust` フラグが存在するが、**このプロジェクトでは使わない**(2026-07-09にユーザーがルールとして確定: dangerously系フラグは技術的性質を問わず一律使用しない。確認・承認プロセスの省略はプロジェクトのコンセプト「勝手に判断せずollamaと人間の承認をとる」に反する。`tasks/alignment.md` 参照)。代替として、**セットアップ時に人間が一度 `codex` の `/hooks` でフック定義をレビュー・信頼する手順を正式な手順とする**(これ自体がhuman-in-the-loopでありデザインに合致する)。

#### 3.2.3 アーキテクチャ上の含意

Codex統合は、Claude統合(`ClaudeSDKClient` を使った同一プロセス内の非同期コールバック)とは根本的に異なる形になる。

- `codex exec` をサブプロセスとして起動する(元の `AgentWrapper`/`AGENT_COMMANDS["codex"]` に近い形に戻る)
- `PermissionRequest` フック用の外部スクリプト(例: `agent_wrapper/codex_hooks/permission_request.py`)を別途用意し、`hooks.json` に登録する
- このスクリプトは**Codexによって都度・別プロセスとして起動される**ため、既に起動しているダッシュボード(Flask/`SharedState`)とプロセス境界を越えてやり取りする必要がある。案:
  - ダッシュボード側に `/codex/permission-request` のような新規APIエンドポイントを生やし、フックスクリプトがそこにPOSTして人間の判断が出るまでポーリングする(HTTPベースのブリッジ)
  - `rules.check_destructive()`/`ollama_client.judge_permission()` はスクリプト内で直接呼べる(プロセスを跨がない判定はそのまま再利用可能)ので、明確にESCALATEが必要な場合のみHTTPブリッジを使う設計にできる
- 未検証: `codex exec`(非対話)実行時、フックが何も返さない場合の「通常の承認フロー」が実際にどう振る舞うか(claudeのヘッドレスモードと同様にハングする可能性がある)

### 3.3 まとめ表

| | claude | codex |
|---|---|---|
| 公式Python SDK | `claude-agent-sdk` (安定版寄り) | `openai-codex` (ベータ、承認コールバックなし) |
| 承認コールバックの存在 | 確認済み(`can_use_tool` + `PreToolUse`) | Python SDKにはなし。**Codex CLI自体の`PermissionRequest`フックでのみ可能** |
| 実装の確度 | 高い、実機検証済み | 中程度。フック仕様は実機検証済みだが、プロセス間ブリッジの実装・非対話実行時の挙動が未検証 |
| 実装の形 | 同一プロセス内の非同期コールバック(`ClaudeSDKClient`) | 別プロセスの外部スクリプト + HTTPブリッジ(要新規設計) |

## 4. 新アーキテクチャ案

### 4.1 全体像

`AgentWrapper` を「エージェント種別ごとに異なる実装を持つ」形に分割する。

```
agent_wrapper/
  runners/
    __init__.py
    base.py          # 共通インターフェース(状態更新・通知・判定ロジックの呼び出し方を規定)
    mock_runner.py    # 今のsubprocess+正規表現方式(mock_agent.py用、変更なし)
    claude_runner.py  # 新規: claude_agent_sdk統合
    codex_runner.py   # 新規: openai-codex統合(次回、API調査後)
  rules.py            # 変更なし(判定ロジックは共通で再利用)
  ollama_client.py     # 変更なし
  wrapper.py           # SharedStateはそのまま維持。AgentWrapperは「runnerを選んで動かす」薄い層にする
```

### 4.2 判定ロジックの再利用方法

**Phase 1の実機検証で判明した通り、`can_use_tool` 単体ではなく `PreToolUse` フックと組み合わせる必要がある**(Claude自身が「安全」と自己判断した呼び出しは `can_use_tool` を経由しないため)。

現状の `rules.check_destructive(line: str)` は1行のテキストを受け取る設計。SDK統合後は `tool_name: str` と `tool_input: dict` を受け取ることになるので、次のような薄い変換層を挟む。

```python
def describe_tool_call(tool_name: str, tool_input: dict) -> str:
    """rules.check_destructiveに渡すための文字列表現を組み立てる。"""
    if tool_name == "Bash":
        return tool_input.get("command", "")
    # Write/Edit/Read等はfile_path、WebFetch等はurlなど、ツールごとに関連しそうな値を拾う
    return f"{tool_name}: {tool_input}"
```

これを次の2箇所に配置する(Phase 1で実機確認済みの構成):

```python
async def pre_tool_use_hook(input_data: dict, tool_use_id: str | None, context) -> dict:
    """全てのツール呼び出しで無条件に発火。rules.check_destructiveを強制適用する層。"""
    if input_data.get("hook_event_name") != "PreToolUse":
        return {}
    tool_name = input_data["tool_name"]
    text = describe_tool_call(tool_name, input_data.get("tool_input", {}))
    match = rules.check_destructive(text)
    if match.matched:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": match.reason,
            }
        }
    # 破壊的パターンに一致しなければcan_use_toolでollama判定へ回す
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
        }
    }


async def can_use_tool(tool_name, input_data, context):
    """PreToolUseフックがaskを返したものだけがここに来る。ollama一次判定+人間エスカレーション。"""
    text = describe_tool_call(tool_name, input_data)
    judged = ollama_client.judge_permission(text, model=ollama_model)
    if judged["decision"] == "allow":
        return PermissionResultAllow(updated_input=input_data)
    # ダッシュボード承認待ち(4.3節のasyncio.Event連携)
    ...
```

`rules.DESTRUCTIVE_PATTERNS`(npm install/git clone/rm -rf等)と `ollama_client.judge_permission()` は**変更せずそのまま**使い回せる。

### 4.3 SharedState/ダッシュボード/ntfyとの統合

`can_use_tool` は `async def` である一方、既存の `SharedState.approve_event` は `threading.Event`。以下のいずれかで橋渡しする。

- **案A(推奨)**: `asyncio.Event` を使い、`can_use_tool` コールバック内で `await` する。ダッシュボードのFlask(`/approve`)は別スレッドで動いているので、`loop.call_soon_threadsafe(event.set)` で非同期側に通知する。
- 案B: `can_use_tool` 内で同期的に `threading.Event.wait()` を呼ぶ(asyncioのイベントループをブロックするので、他の非同期処理と共存できない。単一セッションしか動かさない今回の用途なら許容できるかもしれないが、素直に案Aの方が安全)。

`state.set_waiting()`/`notifier()`/`state.append_log()` の呼び出し方自体は今と変えなくてよい。

### 4.4 main.py / dashboard.py への影響

- `main.py`: `--agent claude` のときは `claude_runner.run(prompt, state, notifier, ollama_model)` のような非同期エントリポイントを `asyncio.run()` で起動する形に変える。Flask(`app.run()`)は同期なので、SDKのasyncioループは別スレッドで回す(`threading.Thread(target=lambda: asyncio.run(...))`)。
- `dashboard.py`: 変更不要(SharedStateのインターフェースを変えなければ、ダッシュボード側は今のままでよい)。

## 5. 移行の進め方(フェーズ分け)

### Phase 1: claude-agent-sdk の最小プロトタイプ(隔離した検証)【完了・2026-07-08】

**成果物**: `agent_wrapper/sdk_prototype/probe.py`(リポジトリ内、git管理下。ユーザーの明示的な要望により使い捨てスクラッチパッドではなくここに置いた)

**実施内容と結果**:

1. `sfw uv add claude-agent-sdk==0.2.113` で追加(設計時点の0.2.99から更新あり、`requires-python >=3.10`)。
2. **課金経路を確認**: `SystemMessage(subtype='init')` に `'apiKeySource': 'none'` が含まれ、`ANTHROPIC_API_KEY` を一切設定しない状態で実行できた。さらに `RateLimitEvent(rate_limit_type='five_hour', overage_disabled_reason='org_level_disabled')` が返り、Claude Codeサブスクリプションの5時間レート制限の枠内で動作していることを確認した。**Anthropic API従量課金ではなく、既存の `claude` CLIログイン(サブスクリプション)経由で動く**。
3. **`can_use_tool` 単体では不十分と判明**(重要な設計転換点): 最初にReadツールで検証したところ、`can_use_tool` が0回しか呼ばれず、無害ファイルも拒否したかったファイルも両方読み取られてしまった。原因を切り分けるため:
   - Bashツール(`echo`コマンド)でも再検証したが、やはり0回しか呼ばれなかった。
   - GitHub Issue [#912](https://github.com/anthropics/claude-agent-sdk-python/issues/912)(ドキュメント改善済み)を発見: **`can_use_tool` はClaude自身が「確認が必要」と内部判断した呼び出しにしか発火しない**。単純なコマンドのような「Claudeが安全と自己判断した操作」はそもそも確認フローに入らず、`can_use_tool` を経由せず直接実行される。つまり `can_use_tool` は「Claudeより厳しく判定したい」という `rules.py` の目的には単体では合わない。
4. **解決策: `PreToolUse` フックとの組み合わせ**。公式ドキュメント([hooks](https://code.claude.com/docs/en/agent-sdk/hooks))によると、`PreToolUse` フックはマッチした全てのツール呼び出しで**無条件に**発火し、戻り値の `hookSpecificOutput.permissionDecision` で `"allow"`/`"deny"`/`"ask"` を直接指定できる。`"ask"` を返すと `can_use_tool` に処理が渡る。これにより次の3層構造が実現できることを実機で確認した:
   - `PreToolUse` フックが `rules.check_destructive()` 相当の判定を行う → 一致すれば即 `"deny"`(`can_use_tool` を経由せず確実にブロック)
   - 曖昧なケースは `"ask"` を返す → `can_use_tool` に処理が渡る → ここで `ollama_client.judge_permission()` + ダッシュボード承認 相当の処理を挟める
   - 該当しなければ `"allow"`(安全な操作はスルー)
5. **実機での最終検証結果**(`echo PROBE_ALLOW_OK` / `echo PROBE_ASK_ME` / `echo PROBE_DENY_ME` の3コマンドを実行させた):
   - `PreToolUse` フックは3回とも正しく呼ばれた
   - `ALLOW`コマンドはフックで直接 `allow` され実行された
   - `ASK`コマンドはフックで `ask` を返し、`can_use_tool` が1回呼ばれ(ここでAllowを返し)実行された
   - `DENY`コマンドはフックで `deny` を返し、`can_use_tool` を経由せずブロックされた。Claude側には `tool_use_result` としてエラーが返り、Claudeはそれを正しく認識して「拒否されました」とユーザーに報告した
   - `ResultMessage.permission_denials` にも `echo PROBE_DENY_ME` の呼び出しが記録されており、SDK側でも拒否が正式に記録されていることを確認した
6. **累計コスト**: 4回の実行で約$0.50(サブスク利用)。ユーザーからは「$10程度までは都度確認不要」との許可を得た。

**Phase 2への申し送り**: `describe_tool_call()` は `can_use_tool` だけでなく `PreToolUse` フックの `tool_input` からも呼べる形にする必要がある。`rules.check_destructive()` は主に `PreToolUse` フック側に置き、`ollama_client.judge_permission()` は `can_use_tool` 側(またはフックの`ask`分岐)に置く設計が実機で妥当と確認された。

### Phase 2: agent_wrapper への組み込み【完了・2026-07-09】

- `agent_wrapper/runners/` を新設し、`tool_describe.py`(`describe_tool_call()`, `GATED_TOOLS`)と `claude_runner.py`(`ClaudeRunner`)を実装
- `rules.py`/`ollama_client.py` は既存の判定関数(`check_destructive`/`judge_permission`)を変更せず再利用。`ollama_client.py`には新関数を2つ追加:
  - `explain_operation()`: ALLOW/ESCALATE判定はせず、人間向けに操作内容を日本語で説明するだけの関数(ダッシュボードに生の理由タグだけでなく説明文も出すため)
  - `judge_conversational_question()`: Claudeがツール呼び出しではなく会話文で確認(y/n等)を求めてきた場合に、`judge_permission()`と同じ考え方でnot_question/allow/escalateを一次判定する関数(下記参照)
- `can_use_tool` は `wrapper.py` の `_handle_line()` と1対1対応させた: `rules.check_destructive()` 一致→(ollamaの判定を経由せず)`explain_operation()`で説明の上で必ず人間の承認待ち、不一致→`judge_permission()`の一次判定→ALLOWならそのまま続行/ESCALATEなら同様に人間の承認待ち。**「危険な操作でもAIが独断で拒否せず最終判断は人間に委ねる」という既存の設計思想を壊さないこと**が実装中に一度指摘・修正された(tasks/lessons.md参照)
- `SharedState` はそのまま。承認待ちは `ClaudeRunner` 専用の `asyncio.Event` を使い(mock方式の `threading.Event` とは分離)、`approve()`/`deny()` は `loop.call_soon_threadsafe()` で橋渡しする
- 既存の `mock` 経路は一切変更していない
- テスト: `tests/test_claude_runner.py` に `_can_use_tool`/`_pre_tool_use_hook`/`_converse` を直接呼ぶユニットテストを追加(実際のSDK起動なし)
- **`query()` から `ClaudeSDKClient` への切り替えを実装当初の想定より前倒しで実施**。理由は下記「実機スモークテストでの発見」を参照
- **実機スモークテスト**(`agent_wrapper/sdk_prototype/smoke_claude_runner.py`)で `ClaudeRunner` を実際に起動し、`PreToolUse`→`can_use_tool`→ollama→ツール実行→セッション正常終了、という一連の流れがエンドツーエンドで動作することを確認した

#### 実機スモークテストでの発見: グローバルCLAUDE.mdの混入と会話質問への対応

最初のスモークテストで、起動したClaudeがこのマシンの `~/.claude/CLAUDE.md`(操作者個人のグローバル指示、AI運用5原則)を一時ディレクトリ起動時にも引き継いでしまい、Bashツールを実際に呼ぶ前に「y/nで確認してください」と**プレーンテキストで**尋ねて止まった(ツール呼び出し自体が発生しないため `PreToolUse`/`can_use_tool` が一切機能しない)。

`setting_sources=["project"]` を指定して対処を試みたが、**この設定だけでは `~/.claude/CLAUDE.md` の混入を防げないことが実機で判明した**(原因未確定。ドキュメント上 `setting_sources` は主に `settings.json`(許可/拒否ルール・フック)の読み込み制御であり、ユーザーレベルの `CLAUDE.md` はこれとは別経路で常に読み込まれる可能性がある。要追加調査)。

そこでユーザーの提案により、`query()`(一発投げっぱなし)から `ClaudeSDKClient`(対話継続可能)に切り替え、Claudeが会話文で確認を求めてきた場合に短い返答("y"等)を送り返す仕組み(`ClaudeRunner._converse()`, 最大5ターンで打ち切り)を実装した。

**設計の修正(重要)**: 最初の実装(`ollama_client.answer_conversational_question()`)は、質問かどうかだけを判定し、質問であれば内容に関わらず一律で肯定的な返答を生成する作りだった。ユーザーから「ollamaは内容を把握してyと回答してるんだよね？適当に相槌で入って答えられると困る。一次受付って言ったよね」と指摘され、これは`judge_permission()`が実際にALLOW/ESCALATEを判定しているのと不整合であり、「一次受付」の役割を果たせていないと判明。`ollama_client.judge_conversational_question()`に置き換え、`judge_permission()`と同じ構造(not_question/allow/escalate、判断がつかない場合も安全側=escalateに倒す)にした。`ClaudeRunner._converse()`もこれに合わせ、escalateの場合は`explain_operation()`で内容を説明した上で`state.set_waiting("conversational_escalated", ...)`により人間の承認待ちにし、その結果("y"/却下メッセージ)をClaudeに送り返すよう修正した(`_wait_for_approval()`を`_can_use_tool`と共通で再利用)。

**この会話応答レイヤーはツール実行の安全性には直接関与しない**(あくまで「進めてよいか」という相槌への一次対応)。実際のツール実行の安全性は、この応答とは独立して `PreToolUse`/`can_use_tool` 側の `rules.check_destructive()`/`judge_permission()` が別途担保する(会話でallow=yと答えても、それだけで危険なツール実行が素通りするわけではない、という二重の安全網)。

再実機テストの結果、この会話応答レイヤーが正しく機能し、CLAUDE.md混入によるy/n質問を吸収した上で、その後の実際のBashツール呼び出しは想定通り `PreToolUse`→`ask`→`can_use_tool`→`judge_permission()`(ALLOW)→自動続行、という経路を通ることを確認した。CLAUDE.md混入の根本原因は未解決だが、実用上はこの多層防御で問題なく動作する。

### Phase 3: codex_runner.py【完了・2026-07-09】

**最終的に採用したのはPermissionRequestフックでもPython SDKでもなく、`codex mcp-server` + MCP elicitation方式**。経緯:

1. `openai-codex` Python SDKには承認コールバックが存在しないことを確認(隔離venvでの実機調査。3.2.1節)
2. Codex CLIの`PermissionRequest`フック(hooks.json)を実機検証したところ、**`codex exec`(非対話)はセッションヘッダに`approval: never`と表示され、`-c approval_policy="untrusted"`を渡しても強制的に承認なしへ上書きされる**ことが判明。承認要求自体が生成されないため、フックは3回のプローブで一度も発火しなかった(検証時の一時ファイルは`C:\tmp\codex_hook_test`、検証後に削除してよい)。claudeヘッドレスモードと同型の制約
3. 代替として`codex mcp-server`方式を検証(ユーザー承認済み。合格基準は「MCP経由でもフルエージェントとして動くか」= ファイル書き込み・シェル実行がローカルで実際に行われるか、が最重要)
   - `codex mcp-server`の`codex`ツールは`approval-policy`(untrusted/on-failure/on-request/never)を正式な引数として受け付ける
   - untrustedで実行すると、シェル実行(`exec-approval`: コマンド配列+cwd付き)とファイル変更(`patch-approval`: 差分内容付き)の**両方でMCP elicitationがクライアントに届く**
   - **重要な発見**: codexのelicitation応答はMCP標準(action/content)ではなく、トップレベルの`{"decision": "approved"|"denied"}`を読む独自形式(codex-rs/mcp-server/src/exec_approval.rsの`ExecApprovalResponse`。**codex自身のソースにTODOとして「ElicitResultに準拠していない」と明記**されており、パース失敗時は保守的にdenied扱い)。MCP標準準拠の応答だけを返した初回プローブでは全てdenyされ、ファイルが作られなかった。`ElicitResult`は`extra="allow"`なので`model_validate({"action": "accept", "decision": "approved"})`で同梱して解決
   - 合格基準1・2とも達成: 承認後にファイルが実際に作成され(`probe_hello.txt`)、`mkdir`も実際に実行された(`probe_dir`実在確認)
4. 本実装: `agent_wrapper/runners/codex_runner.py`(`CodexRunner`)。`mcp==1.28.1`(公式SDK)を依存に追加。あわせて`runners/base.py`(`ApprovalRunnerBase`)を新設し、ClaudeRunner/CodexRunnerで3層ゲート(`_gate`)・会話一次受付(`_conversational_reply`)・approve/deny配線を共通化した(設計ドキュメント4.1節の当初計画通り)
5. 実機スモークテスト(`agent_wrapper/sdk_prototype/smoke_codex_runner.py`)で、elicitation→ollama一次判定(ALLOW)→自動承認→codexが実際にファイル作成・`mkdir`実行→セッション正常終了、というエンドツーエンドを確認済み

補足事項:
- Windowsサンドボックス(`[windows] sandbox="unelevated"`)内で読み取り系コマンドが拒否されるケースがあるが、その場合codexは承認付き再実行(=elicitation経由)に切り替えるため、むしろ全てゲートを通る方向に働く
- codexが送る独自通知(`codex/event`)は`mcp` SDKの型検証を通らず警告ログが出るが、elicitation/ツール応答の処理には影響しない
- codexも`~/.codex/AGENTS.md`(操作者のグローバル指示)を読み込む。会話一次受付(`_conversational_reply`)がこれを吸収する(claude側と同じ構図)

### Phase 4: ドキュメント・テスト整備

- README.mdの「実際のClaude Code / Codexに繋ぐとき」節を全面的に書き直す
- `tasks/todo.md`/`tasks/lessons.md` を更新する

## 6. 未解決の疑問・リスク

- ~~課金経路~~ **解決済み(Phase 1で確認)**: サブスクリプション経由(`apiKeySource: 'none'`、`five_hour`レート制限)で動作する。
- ~~`can_use_tool` で全ツール呼び出しを拾えるか~~ **解決済み(Phase 1で確認)**: `can_use_tool` 単体では拾えない(Claude自身が安全と判断した呼び出しはスルーされる)。`PreToolUse` フックとの組み合わせが必須。4.2節参照。
- ~~非同期(asyncio)とFlask(WSGI, 同期)の統合~~ **解決済み(Phase 2で確認)**: `loop.call_soon_threadsafe()` による橋渡しを実機スモークテストで確認。デッドロック等は発生しなかった。
- **既知の制限として既に記録済みの `threading.Event` 共有バグ**(`tasks/lessons.md` 参照)は、`ClaudeRunner` でも `asyncio.Event` を使う形で同種の制限(承認待ちは1件のみを想定)を引き継いでいる。複数のツール呼び出しが同時にaskへ倒れた場合、片方への承認がもう片方の待機も解除してしまう可能性がある。「保留中のリクエストをキュー/リストで持つ」設計への変更は未着手。
- ~~`query()`と`ClaudeSDKClient`のどちらを使うか~~ **解決済み(Phase 2で確認)**: `ClaudeSDKClient`を採用。会話文での確認質問に`ollama_client.judge_conversational_question()`(not_question/allow/escalate)で一次判定し応答する仕組みが必要になったため。
- **`~/.claude/CLAUDE.md`(ユーザーレベル)が`setting_sources=["project"]`でも混入する**: Phase 2の実機スモークテストで確認(上記参照)。`setting_sources=[]`(完全隔離)にした場合に防げるかは未検証。根本原因・完全な対処法は未解決。現状は`ClaudeRunner._converse()`の会話応答レイヤーが実用上の緩和策になっている。
- ~~codexの承認コールバックAPI~~ **解決済み(2026-07-09、Phase 3)**: Python SDKには存在しない。`PermissionRequest`フックも`codex exec`では発火しない(承認が強制的にneverになるため)。最終的に**`codex mcp-server` + MCP elicitation**で実装した(Phase 3節参照)。
- ~~Codex CLIの`PermissionRequest`フックが非対話実行でどう振る舞うか~~ **解決済み(2026-07-09)**: `codex exec`は承認ポリシーを強制的にneverに上書きするため、フックは一切発火しない(3回のプローブで実機確認)。フック方式は放棄。
- ~~`--dangerously-bypass-hook-trust`フラグの要否~~ **解決済み(2026-07-09)**: 使わない。dangerously系フラグは種類を問わず使わないことがユーザーのルールとして確定(`tasks/alignment.md` 参照)。なお最終的に採用したMCP方式ではフック信頼付与自体が不要になった。
- **codexのelicitation応答形式がMCP標準に準拠していない件**: codex側ソースにTODOとして明記されているため、**将来のcodexバージョンで標準形式(action/content)に修正された場合、現在の`decision`同梱方式が動かなくなる可能性がある**。`CodexRunner._elicit_result()`はaction(標準)とdecision(独自)の両方を返しているため両対応だが、codex更新時は要再確認。

## 7. 参考リンク

- <https://code.claude.com/docs/en/agent-sdk/permissions>
- <https://code.claude.com/docs/en/agent-sdk/python>
- <https://code.claude.com/docs/en/agent-sdk/hooks>
- <https://github.com/Roasbeef/claude-agent-sdk-go/blob/main/docs/cli-protocol.md> (Go実装だが、stdin/stdoutの生プロトコルの参考になる)
- <https://developers.openai.com/codex/agent-approvals-security>
- <https://developers.openai.com/codex/sdk>
- <https://developers.openai.com/codex/hooks> (Codex CLIの`PermissionRequest`フック仕様。生HTML確認済み)
- PyPI: `claude-agent-sdk`, `openai-codex`
- GitHub Issue: <https://github.com/anthropics/claude-agent-sdk-python/issues/912> (`can_use_tool`が"ask"判定時のみ発火する仕様)
