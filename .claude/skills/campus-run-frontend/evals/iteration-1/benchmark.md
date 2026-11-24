# Skill Benchmark: campus-run-frontend

**Model**: <model-name>
**Date**: 2026-11-05T06:49:40Z
**Evals**: 0, 1, 2 (3 runs each per configuration)

## Summary

| Metric | With Skill | Without Skill | Delta |
|--------|------------|---------------|-------|
| Pass Rate | 100% ± 0% | 67% ± 14% | +0.33 |
| Time | 288.2s ± 14.2s | 290.6s ± 107.3s | -2.5s |
| Tokens | 122552 ± 12941 | 123454 ± 41169 | -902 |

## Analyst notes

- 带 skill 的三组都通过了「思维链」相关断言；不带 skill 的三组因为没有 PROJECT_CHAIN.md 自动失败。这部分的 +33% 有评测设计偏差，不完全等于模型能力差距。
- 核心任务完成类断言（文案来源、计划具体性、页面实现、索引同步）在两组都基本通过：仓库里的 AI_SPEC/UI_SPEC/静态稿已经很完整，基线不靠 skill 也能做得不错。
- skill 明显改善的是流程闭环：规格同步（eval-0 升 UI_SPEC 并记 DC-03）、设计令牌纪律（eval-2 基线写入了硬编码 rgba）、自带检查脚本（eval-2 基线只能自建检查）、项目记忆（eval-0/eval-2）。
- 时间：with skill 288±14s，without 291±107s。skill 没有增加平均耗时，但显著降低了方差——基线在简单文案任务上 167s，在规划/新增页面上 353s。
- Token：with skill 123k±13k，without 123k±41k。平均几乎相同，skill 更稳定。
- 每个 eval 每种配置只跑了 1 次；stddev 是跨 3 个 eval 的，不是重复运行的方差。不要把它当成统计显著性。
- 范围纪律存在取舍：eval-0 基线更克制（只改按钮与生成器，不升 UI_SPEC 版本）；skill 把「其他不要动」扩展成了规格同步。是否更好取决于用户此刻想要「最小改动」还是「规格不留债」。
