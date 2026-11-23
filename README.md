# 实验板空间效应分析（本地应用）

针对微孔板实验的本地数据分析应用：联合估计**板间截距、板内行/列效应、边缘效应与处理差异**，
显式识别设计完全混杂，坏孔只标记不删除，分析结果以不可变快照保存。全部数据为虚构合成数据。

## 设计底线（详见 [DESIGN.md](DESIGN.md)）

1. **显式参数模型，不做二维平滑**——平滑会把真实处理效应和空间趋势一起抹掉。
2. **先 SVD 判秩再给估计**：零空间向量翻译成"处理 T 与行效应完全混杂"这类具体诊断；
   不可估的对比不给数值，只给混杂说明。
3. **方法选择随快照冻结**：对照的行/列/边缘覆盖决定 OLS / Huber 稳健模型；
   覆盖不足时处理差异必须由独立验证板确认。
4. **坏孔只屏蔽、不删记录**：翻转标记生成新分析版本，原始 `wells` 表永不更新。
5. **追加板不回头改旧分析**：旧快照的板集合、阈值 k、结论全部保持不变。

## 快速开始

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 方式一：网页应用（默认 http://127.0.0.1:8000，SQLite 存于 data/app.db）
.venv/bin/python -m app.server
# 或 ./run.sh

# 方式二：命令行
.venv/bin/python -m scripts.cli init-db      # 导入三个合成场景
.venv/bin/python -m scripts.cli list
.venv/bin/python -m scripts.cli analyze A    # 对场景 A 建分析
```

网页打开后：点"导入全部三个场景" → "创建分析快照"，再切换
**设计诊断 / 板面图 / 残差图 / 坏孔复核 / 逐处理对比**五个标签页。
板面图上点任意孔位可翻转坏孔标记，立即生成新版本并可在"坏孔复核"页对比两版差异。

## 三个合成场景（固定随机种子，可复现）

| 场景 | 布局 | 预期表现 |
|---|---|---|
| A 随机布局 | 2 校准板 + 1 验证板，对照遍布行列与边缘 | 满秩；处理/边缘/行列效应恢复真值；独立验证通过 |
| B 整行同处理 | 每行单一处理 | 秩缺陷；处理与行效应完全混杂，所有处理对比不可估、无数值 |
| C 边缘对照缺失 | 对照只在内部 6×8，T5 占据全部周边，无验证板 | T5 与边缘效应完全混杂不可估；其余可估；标注"未独立验证" |

## 核对与测试

```bash
.venv/bin/python scripts/validate_scenarios.py     # 三场景端到端核对（退出码反映结果）
.venv/bin/python -m unittest discover -s tests     # 26 个单元/API/存储测试
```

## 目录结构

```
app/
  stats.py      统计核心：设计矩阵、SVD 判秩、零空间诊断、OLS/Huber/中位数抛光、验证
  synthetic.py  三场景合成数据生成（真值见文件顶部常量）
  storage.py    SQLite 持久层（wells 只追加；analyses 只插入不更新）
  analysis.py   分析编排：坏孔建议、方法选择、验证、快照落库、版本派生
  server.py     标准库 HTTP 服务与 JSON API
  web/          离线原生 HTML/CSS/SVG/JS（无 CDN、无构建步骤）
scripts/        CLI 与场景核对脚本
tests/          unittest 测试套件
data/           运行期 SQLite（git 忽略）
```

## 主要 API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/import/generate` | 导入全部合成场景（重复板自动跳过） |
| POST | `/api/import/scenario` | 导入单个场景（`A_random`/`B_row_confounded`/`C_edge_missing`） |
| GET | `/api/plates` / `/api/plates/<id>` | 板列表 / 单板原始孔位（无坏孔字段） |
| POST | `/api/analyses` | 以给定板集合与阈值 k 创建不可变分析快照 |
| GET | `/api/analyses` / `/api/analyses/<id>` | 版本列表 / 快照完整内容 |
| POST | `/api/analyses/<id>/rerun` | 翻转坏孔标记，派生新版本（父版本不变） |
