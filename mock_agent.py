"""
ラッパーの動作確認用の疑似エージェント。
実際のclaude/codexの代わりに、いくつかの典型的な出力パターンを流す。
"""

import sys
import time

LINES = [
    "作業開始: リファクタリング対象のファイルを確認しています",
    "src/main.py を編集しました",
    "テストを実行しています...",
    # DESTRUCTIVE_PATTERNSに一致しない、純粋にollamaの一次判定だけを通る例。
    "::REQUEST_PERMISSION:: README.mdの内容を確認してよいか",
    "README.mdを読み込みました",
    "::REQUEST_PERMISSION:: npm install lodash を実行してよいか",
    "依存関係を追加しました",
    "rm -rf build/ を実行しようとしています",
    "::REQUEST_STOP:: このAPI設計を根本的に変えるべきか判断してほしい",
    "続きの作業を再開しました",
]


def main() -> None:
    for line in LINES:
        print(line, flush=True)
        time.sleep(1)
        # 権限確認/停止要求の直後は、承認が来るまで次に進まないふりをする
        if "REQUEST_PERMISSION" in line or "REQUEST_STOP" in line or "rm -rf" in line:
            sys.stdin.readline()  # 承認(標準入力)を待つ
    print("全ての作業が完了しました", flush=True)


if __name__ == "__main__":
    main()
