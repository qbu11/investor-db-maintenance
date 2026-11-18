# investor-db-maintenance

MiraclePlus 投资人数据库的增量维护流水线：按 `updated_at` 从投资人 API 拉增量，合并进本地 CSV，重建检索发布，跑 113 条回归评测，门禁通过才切换线上指针；任何一步失败自动恢复同步前的 CSV。

只依赖 Python 标准库（3.10+）。

## 一条命令

```bash
python3 scripts/cron_investor_sync.py --mode update
```

顺序：

```
API 增量 → CSV 备份与合并 → 重建 release → 回归评测 → 门禁 → 切换 current.json
```

失败自动回滚 CSV，指针不变。

## 快速开始

### 1. 准备数据

仓库不带真实数据。把快照放到 `output/`，或用 `--investors` / `--organizations` 指向外部目录：

```text
output/investors.csv          # 必需列：source_user_id, name, org_id, last_updated
output/organizations.csv      # 必需列：org_id, chinese_name, last_updated
```

字段口径与 `scripts/build_investor_search_v2.py` 一致；完整列见 `scripts/sync_investors_api.py` 里的 `INVESTOR_FIELDS` / `ORG_FIELDS`。

### 2. 配置凭证

```bash
cp deploy/investor-sync.env.example .env
# 填入 INVESTOR_API_CLIENT_ID / INVESTOR_API_CLIENT_SECRET
```

`.env` 已被 `.gitignore` 排除。

### 3. 干跑

```bash
python3 scripts/sync_investors_api.py --dry-run --max-pages 40
```

看 `changed_total`、`metadata_only_total`、`conflicts`、`skipped_new` 是否符合预期。

### 4. 首次同步与基线发布

```bash
# 第一次建议只补空值
python3 scripts/cron_investor_sync.py --mode fill

# 建立第一版基线（门禁此时没有上一版可比）
python3 scripts/run_investor_release.py
```

之后每天用 `--mode update`。

## 合并策略

| 情况 | 行为 |
|---|---|
| API `updated_at` ≤ 本地 `last_updated` | 跳过 |
| 本地空、API 非空 | 填入 |
| 本地非空、API 不同 | `fill` 保留本地并记 conflict；`update` 覆盖 |
| API 为空 | 永不清空本地 |
| 姓名、机构名、`org_id` | 只报告冲突，不改写 |
| 投资人所属机构 | 不改写（API 的 fund 关联与本地快照存在差异） |
| 本地不存在的新记录 | 默认跳过；`--include-new` 才新增 |
| 只有 `last_updated` 变化 | 写时间戳，不触发发布 |

## cron

```cron
17 3 * * * cd /srv/investor-db && set -a && . /etc/investor-sync.env && set +a && /usr/bin/flock -n /tmp/investor-db-cron.lock /usr/bin/python3 scripts/cron_investor_sync.py --mode update >> /var/log/investor-db/sync.log 2>&1
```

脚本内部还有 `research/.cron_sync.lock`，锁超过 6 小时自动清理。示例见 `deploy/`。

## 发布与回滚

每次发布写入新目录 `releases/v<N>_<YYYYMMDD_HHMM>`，不覆盖旧版本；门禁默认拿当前生效版本的 `eval_report.json` 做基线。

```bash
python3 scripts/run_investor_release.py            # 构建 + 评测 + 门禁 + 发布
python3 scripts/run_investor_release.py --dry-run  # 构建 + 评测 + 门禁，不切换指针
python3 scripts/manage_release.py status           # 当前指针与最近历史
python3 scripts/manage_release.py rollback         # 回滚到上一版
```

门禁不通过时 cron 会把 CSV 恢复到同步前备份，退出码 24。

## 退出码

| 码 | 含义 |
|---:|---|
| 0 | 成功或无变化 |
| 2 | 参数错误 |
| 24 | 回归门禁不通过 |
| 25 | 评测/数据失败 |
| 26 | manifest 完整性失败 |
| 28 | IO 失败 |
| 30 | API 失败 |
| 31 | 数据/状态文件失败 |
| 40 | 锁被占用 |

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试全部使用合成数据，不需要 API 凭证和真实快照。

## 文档

- [增量同步与发布手册](doc/投资人数据库增量同步与发布手册.md)
- 标签表：`doc/投资人数据库规范标签表_v2.csv`
- 场景词：`doc/investor_search_terms_v2.csv`
- 验收查询：`doc/investor_search_acceptance_queries_v3.csv`
- 发布策略：`doc/investor_search_release_policy_v3.json`

## License

MIT
