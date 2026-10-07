# wrapped入力キュー・API認証 検証記録

全tests: 301 passed / 12 failed (合計313), 206 subtests passed。新規25件は全成功。
既存12失敗は一時ディレクトリへのアクセス拒否。pytest cache警告2件。TEMP変更でも再現。詳細原因は未確定。
Ruff check成功（探索アクセス拒否警告あり）、format 36 files成功、mypy 24 source files成功。

## 新規テスト名

### tests/test_wrapped_input_queue.py

- test_running_inputs_queue_in_fifo_order_with_positions: PASS
- test_invalid_inputs_do_not_enter_queue: PASS
- test_codex_running_input_is_explicitly_unsupported: PASS
- test_stop_rejects_inputs_and_clears_pending_queue: PASS
- test_completed_session_rejects_inputs: PASS
- test_concurrent_producers_cannot_exceed_capacity: PASS
- test_removing_capacity_is_detected: PASS
- test_removing_length_limit_is_detected: PASS
- test_http_fifo_positions_and_full_conflict: PASS
- test_http_empty_and_long_inputs_are_conflicts: PASS
- test_http_codex_running_input_is_conflict: PASS
- test_http_raw_pty_preserves_response_and_input: PASS

### tests/test_claude_input_queue.py

- test_fifo_instructions_precede_automatic_reply: PASS
- test_approval_wait_holds_instruction_until_resolved: PASS
- test_only_orchestrator_waits_and_resumes: PASS
- test_stop_cancels_idle_immediately: PASS
- test_mock_sdk_receives_initial_task_then_instruction_and_closes_queue: PASS
- test_plan_approval_holds_new_instruction: PASS
- test_bypassing_approval_wait_is_detected: PASS

### tests/test_api_token.py

- test_missing_wrong_empty_and_duplicate_credentials_are_rejected: PASS
- test_correct_token_and_unset_or_empty_token_allow_both_routes: PASS
- test_get_does_not_require_token: PASS
- test_authenticated_requests_still_obey_browser_boundaries: PASS
- test_mutation_auth_comparison_is_detected: PASS
- test_mutation_boundary_removal_is_detected: PASS

