# Sarashina 帖（個人の情報サイト・非公式）

SB Intuitionsの国産AI「Sarashina」の情報を個人で集めている、非公式の情報サイトです。
データの更新は、ローカルLLMで動くSarashinaが担当します。

## 構成

```
data/models.json     モデル総合データベースの元データ
updater/             自動更新（Mac miniで実行）
spark/               Sarashinaの起動スクリプト（DGX Sparkで実行）
data/papers.json     「論文手帖」の論文と解説
data/hf_stats.json   Hugging Faceのダウンロード数・いいね数（updater/hf_stats.py が作る）
data/chat.json       「さらとしなの茶話」の会話（updater/chat.py が1日6回追加）
data/paper_stats.json 論文の引用件数（updater/paper_stats.py がSemantic Scholarから作る）
data/kokusan.json    「国産AI歌合」の名寄せ（国産モデル一覧）と各番の判定
data/events.json     年譜の「出来事」（設立・発表・サービス開始など）と、
                     同じ時期の海外の動き（world：OpenAI・Anthropic・Google）
templates/           ページのひな形（_base.css が3ページ共通のデザイン）
scripts/build.py     データを検証して docs/ にページを生成
docs/                公開されるページ（GitHub Pagesはここを配信）
```

- モデルを1件足すと、モデル目録と年譜の両方に自動で反映されます。
- 色は `templates/_base.css` の `:root` の変数だけで決まります。差し色は `--brand` を変えると全体が変わります。
- サイト名・ナビ・奥付は `scripts/build.py` の `SITE_NAME` / `NAV` / `FOOTER` で一括管理しています。
- 発表を伴うモデル公開は `events.json` の `models` にidを入れると、年表で出来事の下にまとめて表示されます。
- 確認できない項目は `null`（ページ上は「未確認」）のままにします。推測で埋めません。

## 自動更新の仕組み

```
Mac mini  updater/update.py（毎朝 launchd で実行）
  1. 集める  Hugging Face API / SB Intuitionsブログ / Googleニュース
  2. 下書き  → DGX SparkのSarashina2.2-3B（名前・分類・要約だけ）
  3. 検証    scripts/build.py（通らなければ止めてSlackに通知）
  4. 提案    ブランチ作成 → プルリクエスト → Slackに通知
人         スマホでプルリクエストを確認してマージ → GitHub Pagesに公開
```

- 日付・パラメータ数・ライセンスはHugging Face APIから機械的に取得し、LLMには書かせない。
- Sarashinaが書いた項目には `drafted_by` が付き、ページに「✍ Sarashinaが下書き」と表示される。
- 一度提案したもの（マージしなかったものも含む）は `~/.sarashina-site/seen.json` に記録し、二度は出さない。
- Sparkが応答しないときは更新をスキップしてSlackに知らせる。自動で再起動はしない。
- Hugging Faceの数字は `updater/hf_stats.py` がAPIからそのまま取り込む。LLMを通さないので、プルリクエストにはせず main に直接コミットする。
- 論文の引用件数は `updater/paper_stats.py` がSemantic Scholarの公式APIから取り込む（Google Scholarは公式APIがなく、機械的な取得が規約で禁止されているため使わない。ページにはGoogle Scholarへのリンクだけを置く）。
- Googleニュース経由の出典URLは転送用の長いURLになる。マージ前に元記事のURLへ差し替えると読みやすい。

## セットアップ

### DGX Spark（1回だけ）

```bash
# DGX Sparkで実行
cd ~/sarashina-site/spark
VLLM_IMAGE=<手元のvLLMイメージ> ./serve-sarashina.sh start
./serve-sarashina.sh status
cat ~/.sarashina_api_key   # この値をMac miniの .env に書く
```

### Mac mini（1回だけ）

```bash
# Mac miniで実行
cd ~/sarashina-site
cp updater/.env.example updater/.env   # Sparkのアドレス・APIキー・Slack Webhookを書く
gh auth status                         # プルリクエスト作成に gh を使う
python3 updater/update.py --dry-run    # まずは提案内容の確認だけ
```

問題なければ毎朝の実行を登録する。

```bash
# Mac miniで実行（REPO_PATH を実際の場所に書き換えてから）
sed "s|REPO_PATH|$HOME/sarashina-site|" updater/jp.sarashina-site.update.plist \
  > ~/Library/LaunchAgents/jp.sarashina-site.update.plist
launchctl load ~/Library/LaunchAgents/jp.sarashina-site.update.plist
```

## GitHub Pagesの設定（最初の1回）

リポジトリの Settings → Pages → Source を「Deploy from a branch」、Branch を `main` / `/docs` にします。

このサイトは個人が運営する非公式の情報サイトで、SB Intuitions株式会社とは関係ありません。

## さらとしなの茶話（遊びのページ）

Mac miniが1日6回、番組表（朝：天気・季節／午前：生成AI／昼：ローカルLLM／午後：論文コーナー／夕方：よもやま／夜：生成AI）に沿って見出しを1つ拾い、「さら」と「しな」の雑談を作って `docs/chat.html` に追加する。
台本は、DGX SparkのGLMがあらすじと下書きを書き、Sarashina2.2-3Bがさらとしなの口調に言い直し、最後にGLMが事実を確かめる。
GLMが使えないとき・確認が通らないときは、Sarashinaが台本から書く（各回の `made` に担当が残る）。遊びのページなので人の承認は挟まず、main に直接pushする。

- 事件・事故・災害・政治などの見出しは話題にしない（`updater/chat.py` の `AVOID`）
- 見出しにない事実は話さないよう指示し、形の崩れた会話（話者・行数・長さ・URL）は捨てる
- 止めるときは `updater/.env` に `CHAT_ENABLED=0`
- GLMとSarashinaはどちらも手元のDGX Sparkで動かしていて、APIの利用料はかからない
- GLMの接続先は `updater/.env` の `GLM_BASE_URL` / `GLM_API_KEY`（書かなければSarashinaだけで動く）
- 作り方の聞き比べは `python3 updater/chat_compare.py`（サイトには書き込まない）
- ページの「声で聴く」は、閲覧者の端末に入っている日本語の音声で読み上げる（Web Speech API、サーバー不要）

```bash
# Mac miniで実行：1回分を作って表示するだけ
python3 updater/chat.py --dry-run
# 1日6回の実行を登録（REPO_PATH を書き換えてから）
sed "s|REPO_PATH|$HOME/sarashina-site|" updater/jp.sarashina-site.chat.plist \
  > ~/Library/LaunchAgents/jp.sarashina-site.chat.plist
launchctl load ~/Library/LaunchAgents/jp.sarashina-site.chat.plist
```
