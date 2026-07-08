"""
一次受付のローカルモデル(ollama)との連携。
- 定期チェックインの要約
- 権限確認の合図が来たときの、ルールで白黒つかない場合の一次判定
- 「大きな方針決定っぽいか」の判定
"""

import requests

OLLAMA_URL = "http://localhost:11434/api/generate"
DEFAULT_MODEL = "gemma4:e4b"  # 手元の環境に合わせて config.yaml で上書きする想定


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


def judge_permission(context: str, model: str = DEFAULT_MODEL) -> dict[str, str]:
    """
    ルールエンジンで白黒つかなかった権限確認について、
    ollamaに一次判定させる。判断がつかない場合はescalateにする。
    """
    prompt = (
        "コーディングエージェントが次の操作について許可を求めています。"
        "破壊的でなく、明らかに安全で自明な操作であれば ALLOW、"
        "少しでもリスクや不確実性があれば ESCALATE とだけ判定してください。"
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
        "何に影響するかを日本語で1〜2文、簡潔に説明してください。"
        "許可すべきかどうかの判定は不要です。説明文だけを返してください。\n\n"
        f"操作内容: {context}"
    )
    try:
        return _generate(model, prompt)
    except Exception as e:
        return f"(説明生成に失敗しました: {e})"


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
