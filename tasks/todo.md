## 運用ルール
1. タスクを追加するときはチェックボックス形式で書く
2. 完了したら `[x]` にする
3. セクションが全て完了したら、セクションごと削除してよい

## spec: 残タスク(play_groundから転記・2026-07-12)
- [ ] (上流バグ) codexのサンドボックス昇格承認がmcp経由でクライアントに届かない問題(openai/codex#21982系統)の修正を追い、修正されたら--codex-sandboxの既定(danger-full-access)を見直す
- [ ] (将来リスク) codexのelicitation応答形式が将来MCP標準に修正された場合の再確認(CodexRunner._elicit_resultは両対応済みだがcodex更新時に要確認)
- 背景: --dangerously-skip-permissions を使うとClaude自身の権限確認が無効化され本末転倒、外すとヘッドレスモードはstdinを待たずプロセスが終了することを実機検証で確認済み。詳細はdocs/agent_wrapper_sdk_integration_plan.mdを参照。
