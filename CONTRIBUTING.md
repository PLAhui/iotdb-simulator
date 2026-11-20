# 贡献指南

感谢愿意改进这个项目。以下约定只为让评审和回溯更省力，不是门槛。

## 开始之前

- 先搜一遍 Issues 列表，避免重复劳动；
- 较大的改动（改数据模型、改 API 契约、重构写入流程）请先开 Issue 说清动机与方案，再动手；
- 小修复（错别字、样式、边界判断）可以直接提 PR。

## 开发环境

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
./start.sh 8791
```

没有真实 IoTDB 时，把「写入设置 → dry-run」打开即可走完整流程；需要真库时默认连 `127.0.0.1:6667`（`root/root`）。

## 分支与提交

- 分支：`feat/xxx`、`fix/xxx`、`docs/xxx`、`refactor/xxx`；
- 提交信息用 [Conventional Commits](https://www.conventionalcommits.org/zh-hans/)，主题行不超过 50 字：

```
feat(path): 层级表单支持按层给编码占位提示
fix(sse): 每个连接独立队列，修复多标签页日志跳号
docs(config): 补充 points_per_device 留空的语义
```

- 一个 PR 只做一件事；顺手修的其他问题请拆开，避免评审时互相牵连。

## 代码规范

- Python：PEP 8，4 空格缩进，类型标注尽量补全；函数保持单一职责；
- 前端：单个 `index.html`，不引入构建工具与前端框架；CSS 变量集中在 `:root`，新增样式跟随现有命名（`.card` / `.lv` / `.pv` 前缀）；
- **注释写「为什么」**：踩过的坑、版本兼容、性能取舍必须写清；不要写「这是一个函数」这类复述代码的注释；
- 中文文案统一使用全角标点，界面文案与日志保持同一套术语（层级 / 测点 / 编码范围 / 设备）。

## 测试

```bash
python3 -m unittest discover -s tests -t . -v
```

- 改动 `parser.py` / `engine.py` / `rules.py` 必须补或改用例；
- 改动 `writer.py` 至少用 `dry_run` 跑通一次，并在 PR 描述里贴出规模与耗时；
- 涉及界面的改动，请附 1440×900 与 1024×768 两张截图，并说明是否仍满足「整页不滚动」。

## 提交前检查清单

- [ ] 单元测试全绿
- [ ] `dry_run` 跑通，无异常日志
- [ ] 没有提交 `config.yaml`、真实主机地址、账号口令、内网路径
- [ ] 页面无控制台报错；1440×900 下四个页签无整页滚动、无横向溢出
- [ ] 文档与代码同步（字段、接口、默认值有变就更新 `docs/`）
- [ ] CHANGELOG 的 `Unreleased` 段落已补充

## 报告问题

Issue 里请带上：复现步骤、期望结果、实际结果、`config.yaml` 相关片段（**脱敏**）、浏览器与 Python 版本、控制台报错截图。日志请从底部控制台复制，不要只截一句话。
