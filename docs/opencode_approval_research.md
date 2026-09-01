# OpenCode Approval Broker 調査記録

最終更新: 2026-09-01
対象: OpenCode 1.18.25 / Windows / `ollama/gemma4:e4b`

## 目的

OpenCodeが発行する権限要求をagent-wrapperのApproval Brokerへ渡し、人間の応答をOpenCodeへ一度だけ返す経路を検証する。本書は確認済みの事実と未確認事項を分離し、未完了の接続を完成扱いしないための記録である。

## 確認済み

- OpenCode 1.18.25には安定版プラグインのイベント購読と`client.permission.reply()`が存在する。
- プロジェクトローカルの`.opencode/plugins/*.js`は、`opencode run`とTUIの両方で読み込まれた。生成プラグインの起動ログを実機で確認した。
- role別agent定義の`bash: ask`は反映され、`Set-Content smoke_ok.txt ...`でOpenCode内部のpermission要求が生成された。
- OpenCode内部のセッションIDは`ses_...`であり、agent-wrapper上の表示名`worker`とは一致しない。初期スモークテストは`worker`だけを検索していたため、応答対象の照合方法が誤っていた。
- Brokerの外部イベント受付は`POST /approval-events`、人間応答は共通`ApprovalRequest.request_id`と実際の`session_id`を使用する。

## 実機検証結果

### `opencode run`

`bash: ask`の要求生成直後に、OpenCode自身が`permission requested ...; auto-rejecting`を出力して拒否した。プラグインの`permission.asked`ハンドラーからBrokerへ要求は届かなかった。`--interactive`を追加しても、標準入出力をパイプ接続した実行では同じ結果だった。

### OpenCode TUI + ConPTY

TUI、role、モデル、プロジェクトローカルプラグイン、入力画面の起動までは確認した。ただし簡易スモークテストはxterm.jsのような端末エミュレーターではないため、OpenCodeが送る端末能力照会に応答できず、入力を成立させられなかった。したがって、TUIで`permission.asked`がプラグインへ配送されるかは未確認である。

## 現在の結論

- OpenCodeのApproval Broker接続は**未完了**である。
- プラグインのロードとOpenCode内部のpermission要求生成は確認済みだが、その二つを結ぶ`permission.asked`配送は実証できていない。
- `opencode run`は現在の設定ではヘッドレスE2Eの入口として利用できない。
- ブラウザE2Eは着手したが完了しておらず、調査結果には含めない。

## 採用していない案

テストを容易にする目的で`permission.asked`を`tool.execute.before`へ置き換え、OpenCode側の権限を`allow`にする案を一時提案したが、実装していない。この案はプラグイン未読込時にフェイルオープンとなり、`ROLE_PERMISSIONS`の`deny/ask`も迂回し得るため、現行の安全モデルを壊す。ユーザーの明示的な別判断なしに採用しない。

## 次回の検証条件

本番コードの権限方式を変更せず、テスト方法だけを決める。検証成功の条件は、(1) OpenCode由来の要求がBrokerに登録される、(2) 人間承認後に`once`応答が返る、(3)対象ツールだけが再開する、(4)拒否時には副作用が発生しない、の4点とする。
