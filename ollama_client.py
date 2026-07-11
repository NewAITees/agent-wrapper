"""
一次受付のローカルモデル(ollama)との連携。
- 定期チェックインの要約
- 権限確認の合図が来たときの、ルールで白黒つかない場合の一次判定
- 「大きな方針決定っぽいか」の判定
"""

import re

import requests

OLLAMA_URL = "http://localhost:11434/api/generate"
DEFAULT_MODEL = "gemma4:e4b"  # 手元の環境に合わせて config.yaml で上書きする想定

_DESTRUCTIVE_TOKEN_RE = re.compile(
    r"""(?ix)
    \brm\s+-rf\b|
    \bgit\s+reset\s+--hard\b|
    \bgit\s+push\s+.*--force\b|
    \bdrop\s+table\b|
    \bdrop\s+database\b|
    \bdel\s+/f\s+/s\s+/q\b|
    \bformat\s+[a-zA-Z]:|
    \btruncate\s+table\b|
    >\s*/dev/sd[a-z]\b|
    \bnpm\s+i(?:nstall)?\b|
    \byarn\s+add\b|
    \bpnpm\s+add\b|
    \bpip3?\s+install\b|
    \buv\s+add\b|
    \buv\s+pip\s+install\b|
    \bcargo\s+(?:install|add)\b|
    \bgo\s+(?:install|get)\b|
    \bgem\s+install\b|
    \bbrew\s+install\b|
    \bapt(?:-get)?\s+install\b|
    \bchoco\s+install\b|
    \bwinget\s+install\b|
    \bgit\s+clone\b|
    \bdocker\s+pull\b|
    \bdocker\s+run\b
    """
)
_WRITER_PREFIX_RE = re.compile(
    r"""(?is)^\s*(?:echo|printf|cat|tee|write-output|set-content|add-content|out-file|@'|@")"""
)
_EMBEDDED_TEXT_HINT_RE = re.compile(
    r"(?is)(?:write|save|append|set-content|out-file|tee|heredoc|here-string|ファイル)"
)


def _generate(model: str, prompt: str, timeout: int = 30) -> str:
    resp = requests.post(
        OLLAMA_URL,
        json={"model": model, "prompt": prompt, "stream": False},
        timeout=timeout,
    )
    resp.raise_for_status()
    return str(resp.json().get("response", "")).strip()


def summarize_chunk(log_chunk: str, model: str = DEFAULT_MODEL) -> str:
    prompt = (
        "以下はコーディングエージェントの直近の作業ログです。"
        "何が起きたかを日本語で一行、20〜40文字程度で要約してください。"
        "前置きや説明は不要で、要約本文だけを返してください。\n\n"
        f"{log_chunk}"
    )
    try:
        return _generate(model, prompt)
    except Exception as e:
        return f"(要約失敗: {e})"


def judge_permission(
    context: str,
    model: str = DEFAULT_MODEL,
    l1_facts: str | None = None,
    approved_plan: str | None = None,
) -> dict[str, str]:
    """ルールで白黒つかない権限確認を一次判定する。不明時はescalate。"""
    facts = l1_facts or "対象パス: 不明"
    plan = approved_plan or "(承認済み計画なし)"
    prompt = (
        "コーディングエージェントが次の操作について許可を求めています。\n"
        "L0(ALLOW): 外部送信を伴わない読み取り・検索・一覧、git status/diff/log、"
        "ログや設定の確認。\n"
        "L1(ALLOW): cwd配下の通常ファイルの編集・新規作成、テスト、lint、型チェック、"
        "フォーマット、ビルド、ローカルサーバー起動、差分生成。"
        "ファイルを書き込むという理由だけでESCALATEしてはいけません。\n"
        "ESCALATE: cwd外・ユーザープロファイル・システム領域への書き込み、.env・"
        "credential・secret・鍵などへの操作、外部へのデータ送信、スケジューラ登録等の"
        "自動実行の仕掛け、その他判断がつかないもの。\n"
        "以下の機械判定事実を推測で変更せず考慮してください。対象パス不明なら、"
        "操作内容だけで安全性が明白な場合を除きESCALATEしてください。\n"
        f"機械判定事実:\n{facts}\n"
        f"承認済みの実装計画:\n{plan}\n"
        "この操作が計画の範囲内ならL0/L1基準に従いALLOW、"
        "計画から明らかに外れる場合はESCALATEしてください。"
        "計画承認はrulesによる強制確認やL3判定を上書きしません。\n"
        "出力は ALLOW か ESCALATE のどちらか一語のみです。\n\n"
        f"操作内容: {context}"
    )
    try:
        result = _generate(model, prompt).strip().upper()
        decision = (
            "allow" if "ALLOW" in result and "ESCALATE" not in result else "escalate"
        )
    except Exception as e:
        decision = "escalate"
        result = f"error: {e}"
    return {"decision": decision, "raw": result}


def explain_operation(context: str, model: str = DEFAULT_MODEL) -> str:
    """
    人間が承認するかどうか判断できるよう、操作内容を日本語で説明する。
    ALLOW/ESCALATEの判定は行わない(判定はjudge_permission/ルールエンジン側の責務)。
    """
    prompt = (
        "コーディングエージェントが次の操作を行おうとしています。"
        "人間が承認するかどうか判断できるよう、何をする操作で、"
        "何のために行うか(文脈から推測できる場合)、影響・リスク・注意点を"
        "含め、日本語で2〜4文で簡潔に説明してください。"
        "許可すべきかどうかの判定は不要です。説明文だけを返してください。\n\n"
        f"操作内容: {context}"
    )
    try:
        return _generate(model, prompt)
    except Exception as e:
        return f"(説明生成に失敗しました: {e})"


def classify_destructive_match(context: str) -> dict[str, str]:
    """
    destructive検知文字列が「実行コマンドそのもの」か「本文データとして埋め込まれた
    文字列」かを保守的に分類する。判断不能な場合は command_self に倒す。
    """
    match = _DESTRUCTIVE_TOKEN_RE.search(context)
    if not match:
        return {
            "classification": "command_self",
            "headline": "実行コマンド自体が破壊的",
        }

    prefix = context[: match.start()]
    lowered = context.lower()
    if (
        _WRITER_PREFIX_RE.search(prefix) or _EMBEDDED_TEXT_HINT_RE.search(context)
    ) and (
        "'" in context
        or '"' in context
        or "set-content" in lowered
        or "out-file" in lowered
    ):
        return {
            "classification": "embedded_text",
            "headline": "誤検知の可能性あり(コマンド本文中の文字列に一致)",
        }
    return {
        "classification": "command_self",
        "headline": "実行コマンド自体が破壊的",
    }


def judge_conversational_question(
    text: str, model: str = DEFAULT_MODEL
) -> dict[str, str]:
    """
    エージェントが会話文で(ツール呼び出しではなく)実行の可否等について
    人間の返答を求めているかを一次判定する。judge_permission()と同じ考え方:
    質問でなければnot_question、質問で内容が明らかに安全・自明な続行確認なら
    allow、少しでもリスクや不確実性があれば(判断がつかない場合も含め安全側で)
    escalateとする。

    注意: ここでのallowはあくまで会話上の相槌への一次対応であり、実際の
    ツール実行の安全性はPreToolUseフック/can_use_toolによる
    rules.check_destructive()・judge_permission()の判定で別途独立して
    担保される(ここでのallowがツール実行のAllowに直結するわけではない)。
    """
    prompt = (
        "コーディングエージェントが次のように発言しました。"
        "まずこれが実行の可否や次の一歩について人間の返答を求めている質問かどうかを"
        "判断してください。\n"
        "質問でなければ(単なる完了報告・状況説明であれば)、NOT_A_QUESTION とだけ"
        "答えてください。\n"
        "質問であれば、その内容を読んで、破壊的でなく明らかに安全で自明な続行確認で"
        "あれば ALLOW、少しでもリスクや不確実性があれば ESCALATE と答えてください。\n"
        "『必要な変更をすべて実行してよいですか』のように対象範囲を特定できない"
        "包括的な確認には自動でyと答えず、必ず ESCALATE としてください。\n"
        "出力は NOT_A_QUESTION / ALLOW / ESCALATE のいずれか一語のみです。\n\n"
        f"{text}"
    )
    try:
        result = _generate(model, prompt).strip().upper()
        if "NOT_A_QUESTION" in result:
            decision = "not_question"
        elif "ALLOW" in result and "ESCALATE" not in result:
            decision = "allow"
        else:
            decision = "escalate"
    except Exception as e:
        decision = "escalate"
        result = f"error: {e}"
    return {"decision": decision, "raw": result}


def judge_is_major_decision(context: str, model: str = DEFAULT_MODEL) -> bool:
    prompt = (
        "次の発言や状況は、設計方針や大きな意思決定に関わる相談でしょうか。"
        "単なる進捗報告や軽微な確認であれば NO、"
        "方針・設計・不可逆な選択に関わる相談であれば YES とだけ答えてください。\n\n"
        f"{context}"
    )
    try:
        result = _generate(model, prompt).strip().upper()
        # 明確に「NO」とだけ言っている場合を除き、安全側(人間に聞く)に倒す。
        # モデルがYES/NO以外の形式で答えた場合も、ここでTrueになる。
        return not ("NO" in result and "YES" not in result)
    except Exception:
        # ollamaに問い合わせられない場合は安全側(人間に聞く)に倒す
        return True
