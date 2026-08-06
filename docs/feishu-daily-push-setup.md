# 飞书每日推送配置

本仓库现在提供三类自动推送：

| 内容 | 执行时间 | 工作流 | 默认数量 |
| --- | --- | --- | --- |
| 行业实践文章 | 每天美西时间 08:00（自动适配 PST/PDT） | `daily_feishu_digest` | 5 篇 |
| 顶会论文 | 每天美西时间 08:00（自动适配 PST/PDT） | `daily_feishu_digest` | 3 篇 |
| arXiv 每日论文 | 每天美西时间 08:00（自动适配 PST/PDT） | `daily_feishu_digest` | 10 篇 |

行业实践文章来自仓库已有的文章库，采用稳定轮换，避免每天只推最新几篇。顶会论文从 KDD、WWW、CIKM、RecSys、WSDM、SIGIR、ECIR 近年论文中筛选搜广推相关标题并轮换。arXiv 从 cs.IR、cs.CL、cs.LG 最近 7 天的新论文中按搜广推关键词相关性和发布时间排序。三者都不需要付费模型。

## 数量与选取规则

`5 + 3` 只是为了让单次飞书卡片适合通勤阅读而设置的默认数量，并不是质量阈值，也不是仓库限制，可以通过 `INDUSTRY_LIMIT` 和 `CONF_LIMIT` 修改。

- 行业文章：原始数据只有公司、标题、标签、日期和链接，没有阅读量、收藏量、人工评分等质量字段。因此当前不会假装给文章做质量排名，而是按文章标识生成稳定顺序，再根据日期轮换；符合标签过滤条件的文章机会均等，同一天重复执行会得到相同结果。
- 顶会论文：先限制会议和年份，再根据标题中的推荐、搜索、广告、排序、CTR、检索等关键词计算相关性；排序时优先较新的年份，其次是关键词相关性，然后按日期轮换。它衡量的是“与搜广推的相关程度和新近程度”，不是引用量、最佳论文奖或真实研究质量。
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

本仓库本身没有自动发现新的行业文章；新文章仍由仓库 Issue/数据维护流程加入。每日任务的含义是每天从已收录的行业文章中选取内容推送。arXiv 则会每天实时抓取新论文。
