# 飞书每日推送配置

本仓库现在提供三类自动推送：

| 内容 | 执行时间 | 工作流 | 默认数量 |
| --- | --- | --- | --- |
| 行业实践文章 | 每天美西时间 08:00（自动适配 PST/PDT） | `daily_feishu_digest` | 5 篇 |
| 顶会论文 | 每天美西时间 08:00（自动适配 PST/PDT） | `daily_feishu_digest` | 3 篇 |
| arXiv 每日论文 | 每天美西时间 08:00（自动适配 PST/PDT） | `daily_feishu_digest` | 10 篇 |

行业实践文章会实时读取 Netflix、Uber、Spotify、GitHub、Pinterest、Airbnb 等工程博客 RSS，并按发布日期取最新内容。顶会论文从 KDD、WWW、CIKM、RecSys、WSDM、SIGIR、ECIR 论文中优先选择最新年份和搜广推相关内容。arXiv 从 cs.IR、cs.CL、cs.LG 最近 7 天的新论文中按搜广推关键词相关性和发布时间排序。GitHub Models 使用工作流自带的 `GITHUB_TOKEN` 生成中文标题和中文摘要，不需要额外付费模型 Secret。

## 数量与选取规则

`10 + 5 + 3` 只是为了让单次飞书卡片适合通勤阅读而设置的默认数量，并不是质量阈值，也不是仓库限制，可以通过 `ARXIV_LIMIT`、`INDUSTRY_LIMIT` 和 `CONF_LIMIT` 修改。

- 行业文章：只从公开工程博客 RSS 中选择发布日期最新的文章；日期相同时再比较搜广推、机器学习、LLM、推理和数据平台关键词。所有实时来源都不可用时直接失败，不再静默回退到旧文章。
- 顶会论文：先限制会议和年份，再根据标题中的推荐、搜索、广告、排序、CTR、检索等关键词计算相关性；优先最新年份，其次是关键词相关性，不再按日期轮换到旧论文。它衡量的是“相关程度和新近程度”，不是引用量或获奖情况。
- arXiv：先限定分类和最近 7 天，再按标题及摘要中的检索、推荐、排序、广告、CTR、个性化、LLM 等关键词加权；同分时优先发布时间更近的论文。默认 10 篇是飞书卡片的阅读上限，可通过 `ARXIV_LIMIT` 修改。

如果要做真正的质量精选，需要增加可靠信号，例如 Semantic Scholar 引用量、会议奖项、GitHub 热度或人工评分；当前仓库没有这些数据。

## 一次性配置

1. Fork 本仓库到自己的 GitHub 账号，并保留 Git LFS 数据。把本地已配置版本推到你的 fork（将 `<你的账号>` 替换为 GitHub 用户名）：

   ```bash
   git remote rename origin upstream
   git remote add origin https://github.com/<你的账号>/Algorithm-Practice-in-Industry.git
   git add .
   git commit -m "feat: add daily Feishu digests"
   git push -u origin main
   ```

2. 在飞书桌面端进入目标群：`群设置` → `群机器人` → `添加机器人` → `自定义机器人`。建议把安全设置中的关键词设为 `日推`（三类消息标题都包含它），然后复制 Webhook 地址。
3. 在你的 GitHub 仓库进入 `Settings` → `Secrets and variables` → `Actions` → `New repository secret`，添加：

   - `FEISHU_URL`：飞书机器人 Webhook，必填。多个群可用英文逗号分隔多个 Webhook。
   - `DEEPSEEK_API_KEY`：仅在手动运行原仓库的 LLM 增强版 `arxiv_daily_full` 时需要；每天 08:00 的三类日推不需要它。

4. 进入 `Actions`，手动运行 `daily_feishu_digest` 验证三类飞书消息。定时任务只会在默认分支上的 workflow 生效。

Webhook 等同于群机器人的发送凭证，不要写入代码、Issue 或日志。本实现只从 GitHub Actions Secret 读取。

## 可调参数

在 `.github/workflows/push_conf_daily.yml` 的 `env` 中可以调整：

- `INDUSTRY_LIMIT`：每天行业文章数量。
- `INDUSTRY_FEEDS`：可选的工程博客 RSS JSON 映射；不设置时使用仓库默认来源。
- `CONF_LIMIT`：每天顶会论文数量。
- `ARXIV_LIMIT`：每天 arXiv 论文数量。
- `ARXIV_LOOKBACK_DAYS`：arXiv 新论文回看天数，默认 7 天以覆盖周末和发布延迟。
- `ARXIV_CATEGORIES`：arXiv 分类，默认 `cs.IR,cs.CL,cs.LG`。
- `CONF_START_YEAR`：顶会论文最早年份。
- `CONFS`：会议缩写，英文逗号分隔。
- `INDUSTRY_TAGS`：可选，行业标签过滤，例如 `推荐,搜索,广告`。

原仓库带翻译和 LLM 摘要的增强版仍可手动运行 `arxiv_daily_full`，但需要额外配置 `DEEPSEEK_API_KEY`。

## 本地安全预览

预览命令不会向飞书发消息：

```bash
python -m paperBotV2.industry_practice.daily_push --dry-run --date 2026-08-05
python -m paperBotV2.conf_summary.daily_push --dry-run --date 2026-08-05
```

第二条命令需要 `paperBotV2/conf_summary/data/results.json` 已由 Git LFS 下载，而不是一个 LFS 指针文件。

运行测试：

```bash
python -m unittest discover -s tests -v
```

## 注意

GitHub Models 的中文摘要基于文章或论文提供的标题与摘要生成，不等同于阅读全文后的深度评审。公开 RSS 或 arXiv API 暂时不可用时，工作流会失败并保留错误日志，不会静默发送过期的英文替代内容。
