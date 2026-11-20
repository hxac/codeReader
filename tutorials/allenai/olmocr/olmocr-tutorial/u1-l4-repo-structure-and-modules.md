# u1-l4 仓库结构与模块地图

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `olmocr/` 包下每个顶层 `.py` 文件（`pipeline.py`、`work_queue.py`、`metrics.py`、`s3_utils.py`、`image_utils.py`、`datatypes.py`、`check.py`、`repeatdetect.py`、`version.py`）各自的一句话职责。
2. 说出七个子包（`prompts/`、`filter/`、`data/`、`train/`、`bench/`、`synth/`、`viewer/`）分别管哪一段事情。
3. 画出模块间的依赖方向草图：`pipeline`（推理）与 `train`（训练）是两个"消费端"，`bench`、`prompts`、`data` 是被双方共享的底层。
4. 把 `pyproject.toml` 里的各个 pip extras 与仓库里的子包对应起来——"装了什么 extras，才有哪部分代码能跑"。

本讲不深入任何一个模块的内部实现（那是第 2~7 单元的事），只建立一张**全局代码地图**。以后读到任何一篇讲义，你都能立刻定位"这个文件在地图的哪个位置、它的上下游是谁"。

## 2. 前置知识

本讲只需要几个很轻的 Python 概念，用通俗语言过一遍：

- **模块（module）与包（package）**：一个 `.py` 文件就是一个模块；一个含 `__init__.py` 的目录就是一个包。`olmocr/work_queue.py` 对应模块 `olmocr.work_queue`；`olmocr/prompts/` 目录对应包 `olmocr.prompts`。
- **`__init__.py` 的作用**：包被 `import` 时最先执行的文件，通常用来"转发"子模块里的重要名字。例如 `from olmocr.prompts import PageResponse` 能直接用，是因为 `prompts/__init__.py` 里替你 `from .prompts import PageResponse` 了。
- **抽象基类（abc）**：用 `@abc.abstractmethod` 声明"子类必须实现这些方法"的父类。`work_queue.py` 的 `Backend` 就是它——定义了"一个存储后端该会做什么"，具体怎么做交给 `LocalBackend` 和 `S3Backend`。本讲只需要知道这个角色分工。
- **pip extras**：第 u1-l2 讲已经装过 `pip install olmocr`。`pyproject.toml` 里 `[project.optional-dependencies]` 定义的 `gpu`、`bench`、`train` 等分组就是 extras——不装它们，`olmocr` 主体仍能 import，只是对应功能的代码在运行时会因缺依赖而失败。
- **依赖方向**：如果 A 文件里出现 `from olmocr.B import ...`，就画一条边 A → B，读作"A 依赖 B"。一个健康的代码库里这些箭头应该大体单向、不绕圈。本讲教你用这条规则亲自"测量"出 olmocr 的架构。

承接前面三讲：u1-l1 讲了 olmocr "是什么"（VLM 驱动的 PDF→文本工具箱）；u1-l2 讲了 "怎么装"（系统依赖 + pip extras 分层）；u1-l3 讲了 "怎么跑出第一个结果"（workspace 五大产物、`olmocr` 命令经 `cli_main()` 进入异步 `main()`）。本讲回答第四个问题：**"代码长什么样、怎么分区"**。

## 3. 本讲源码地图

本讲涉及的文件（按重要程度排序）：

| 文件 | 在本讲中的角色 |
|---|---|
| `pyproject.toml` | 入口注册与 extras 划分，模块地图的"官方索引" |
| `olmocr/pipeline.py` | 全仓库最大的文件（1500+ 行），核心流水线 + CLI 入口 |
| `olmocr/work_queue.py` | 工作队列抽象（`WorkItem`/`Backend`/`WorkQueue`） |
| `olmocr/prompts/prompts.py`、`olmocr/prompts/__init__.py` | 提示词体系与 `PageResponse` 数据契约 |
| 其余顶层文件与子包 | 逐个"点名"，只看职责不看实现 |

仓库根目录还有 `tests/`（pytest 测试）、`scripts/`（发布等辅助脚本，不随包分发）、`docs/`、`Makefile`、`Dockerfile`、`Dockerfile.with-model`、`CHANGELOG.md` 等，本讲不展开，后面单元用到再讲。

`olmocr/` 包的整体形状（只列目录与 `.py`，省略包内资源文件）：

```text
olmocr/
├── __init__.py            # 只导出 VERSION
├── version.py             # 版本号
├── pipeline.py            # ★ 核心流水线 + CLI 入口
├── work_queue.py          # ★ 工作队列（本地/S3 双后端）
├── metrics.py             # 运行时指标
├── s3_utils.py            # S3/GCS/WEKA 工具函数
├── image_utils.py         # 图片→PDF 转换与格式探测
├── datatypes.py           # PdfOutput 数据类（历史遗留，包内无人引用）
├── check.py               # 环境自检（u1-l2 已精读）
├── repeatdetect.py        # 重复 n-gram 检测器
├── prompts/               # 提示词与锚文本
├── filter/                # PDF 过滤（语言/垃圾内容）
├── data/                  # 渲染与训练数据准备脚本
├── train/                 # SFT/GRPO 训练
├── bench/                 # olmOCR-Bench 评测体系
├── synth/                 # 合成数据生成
└── viewer/                # Dolma 结果查看器
```

## 4. 核心概念与源码讲解

### 4.1 顶层模块清单梳理：olmocr/ 根目录的单文件模块

#### 4.1.1 概念说明

打开任何一个陌生 Python 项目，最有效的第一步不是从 `main` 开始读，而是先**清点根目录的顶层模块**——它们往往是一个项目的"公共名词表"（数据结构、工具函数），而大文件则是"动词表"（流程编排）。olmocr 的根目录有 9 个 `.py` 文件，其中两个带 ★ 的是重头戏：

- `pipeline.py`：整个项目的主流程。你在 u1-l3 敲的 `olmocr` 命令就落在它的 `cli_main()`。
- `work_queue.py`：把"待处理 PDF 列表"变成一个可断点续跑、可多机协同的队列。
- 其余 7 个都是被前者引用的工具/数据模块。

#### 4.1.2 核心流程

浏览顶层模块的方法论（本节实践会照此执行）：

1. `ls olmocr/*.py` 拿到清单。
2. 对每个文件只看两样东西：模块级 docstring/常量、顶层 `class` / `def` 的名字。
3. 用一句话回答："如果删掉这个文件，哪类功能会坏？"
4. 对照 `pyproject.toml` 的 `[project.scripts]`，确认命令行入口指向哪个模块。

#### 4.1.3 源码精读

**入口注册。** `olmocr` 命令是官方钦定的唯一 CLI 入口，注册在 pyproject 里：

- [pyproject.toml:49-50](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/pyproject.toml#L49-L50) —— `[project.scripts]` 声明 `olmocr = "olmocr.pipeline:cli_main"`：pip 安装后生成的 `olmocr` 可执行文件就是调用 `olmocr/pipeline.py` 里的 `cli_main` 函数。这解释了为什么"主流程"非 `pipeline.py` 莫属。

**pipeline.py 的骨架。** 它 1500+ 行，但顶层函数屈指可数，每个都能对应流水线的一站：

- [olmocr/pipeline.py:1526](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1526) —— `def cli_main():` 同步壳，内部用 `asyncio.run()` 桥接异步世界（u1-l3 已讲）。
- [olmocr/pipeline.py:1199](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1199) —— `async def main():` 真正的主流程：建队列、收 PDF、起 vLLM、拉 workers（u2-l1 将逐行精读）。
- [olmocr/pipeline.py:698](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L698) —— `async def worker(...)` 单个 worker 协程：从队列取活、逐页处理、落盘。
- [olmocr/pipeline.py:281](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L281) —— `async def process_page(...)` 单页处理编排（重试、温度阶梯，u2-l6 的主角）。
- [olmocr/pipeline.py:149](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L149) —— `async def try_single_page(...)` 一次模型调用。
- [olmocr/pipeline.py:602](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L602) —— `build_dolma_document(...)` 把各页结果拼成 Dolma JSON（u2-l8）。
- [olmocr/pipeline.py:654](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L654) —— `get_markdown_path(...)` 计算每个源文件的 Markdown 输出路径。
- [olmocr/pipeline.py:807](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L807) —— `async def vllm_server_task(...)` 启动并看护 vLLM 推理服务（u3-l3）。
- [olmocr/pipeline.py:992](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L992) —— `def submit_beaker_job(args):` 把任务提交到 AI2 的 Beaker 集群（u3-l4）。

> 记忆法：`pipeline.py` 就是"一条装配线"，从 `main` 进、从 `worker`/`process_page` 干活、以 `build_dolma_document` 收尾。

**work_queue.py 的三件套。** 这个文件不到 600 行，结构非常干净：

- [olmocr/work_queue.py:30-35](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L30-L35) —— `WorkItem` 数据类：一组待处理路径 + 它们的组哈希（u1-l3 见过的 sha1）。
- [olmocr/work_queue.py:38-79](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L38-L79) —— `Backend` 抽象基类：定义 8 个存储原语（读写索引、查/写 done flag、查/建/删 worker 锁）。
- [olmocr/work_queue.py:82-90](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L82-L90) —— `WorkQueue`：持有某个 `Backend` 实例，向上提供 `populate_queue`/`get_work`/`mark_done` 等队列语义。
- [olmocr/work_queue.py:238](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L238) 与 [olmocr/work_queue.py:392](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L392) —— `Backend` 的两个实现：`LocalBackend`（本地文件）与 `S3Backend`（S3 对象）。u3-l1/u3-l2 会精读。

**其余顶层文件速览。**

- [olmocr/metrics.py:7](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/metrics.py#L7) —— `MetricsKeeper`：累计指标 + 滑动窗口速率；[olmocr/metrics.py:104](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/metrics.py#L104) —— `WorkerTracker`：每个 worker 当前在处理什么（u4-l4）。
- [olmocr/s3_utils.py:26](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/s3_utils.py#L26) —— `parse_s3_path`、[L36](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/s3_utils.py#L36) `expand_s3_glob`、[L87](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/s3_utils.py#L87) `get_s3_bytes`、[L163](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/s3_utils.py#L163) `download_zstd_csv`、[L199](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/s3_utils.py#L199) `download_directory`（跨 S3/GCS/WEKA 拉模型目录）——S3 世界的一把瑞士军刀（u4-l3）。
- [olmocr/image_utils.py:6-37](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/image_utils.py#L6-L37) —— `convert_image_to_pdf_bytes`：调用外部命令 `img2pdf` 把 PNG/JPEG 包成 PDF 字节；同文件还有 `is_png`/`is_jpeg` 魔数探测（u2-l2 用到）。
- [olmocr/datatypes.py:7-33](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/datatypes.py#L7-L33) —— `PdfOutput.mk_dolma_doc`：把整篇 PDF 文本打包成 Dolma JSON。**注意**：全仓库目前没有任何模块 import 它（现在的流水线用的是 `pipeline.py` 里的 `build_dolma_document`），读代码时可以把它当作历史遗留的独立数据类，避免混淆。
- [olmocr/check.py:9](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/check.py#L9) —— `check_poppler_version`、[L30](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/check.py#L30) `check_torch_gpu_available`（u1-l2 已精读）。
- [olmocr/repeatdetect.py:8](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/repeatdetect.py#L8) —— `RepeatDetector`：用 n-gram 统计检测文本是否陷入复读（VLM 的典型病：同一段话无限重复），被 `bench/tests.py` 引用。
- [olmocr/version.py:10-11](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/version.py#L10-L11) —— 版本字符串拼接处，`pyproject.toml` 的 `dynamic = ["version"]` 就是从这里取的（见 [pyproject.toml:149-150](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/pyproject.toml#L149-L150)）。

#### 4.1.4 代码实践（源码阅读型：亲手做一张顶层模块职责卡）

1. **实践目标**：为 `olmocr/` 根目录全部 9 个 `.py` 文件各写一句话职责，形成你的第一版"名词表"。
2. **操作步骤**：
   - 在仓库根目录执行 `ls olmocr/*.py`，得到 9 个文件。
   - 对每个文件执行 `grep -n "^class \|^def \|^async def " olmocr/<文件名>`，只看顶层定义名，不读实现。
   - 填写下面这张表（左列是文件名，右列留给你自己写；写完可对照 4.1.3 的描述自查）：

     | 文件 | 一句话职责（自己填） | 参考答案关键词 |
     |---|---|---|
     | `pipeline.py` | ______ | 主流程 + CLI 入口 |
     | `work_queue.py` | ______ | 可断点续跑的工作队列 |
     | `metrics.py` | ______ | 指标与 worker 状态 |
     | `s3_utils.py` | ______ | S3 路径/下载/上传工具 |
     | `image_utils.py` | ______ | 图片转 PDF、格式探测 |
     | `datatypes.py` | ______ | PdfOutput（遗留） |
     | `check.py` | ______ | 环境自检 |
     | `repeatdetect.py` | ______ | 复读检测 |
     | `version.py` | ______ | 版本号 |

3. **需要观察的现象**：`grep` 输出里每个文件的顶层定义数量差异很大——`pipeline.py` 十几个函数全是流程动词，`version.py` 连一个函数都没有只有常量。
4. **预期结果**：你会直观感受到"少数大文件管流程、多数小文件管工具"的分层。grep 命令本身不依赖任何 Python 环境，一定能跑通。

#### 4.1.5 小练习与答案

**练习 1**：不看讲义，说出 `olmocr` 命令从敲下回车到进入异步主流程，依次经过哪两个函数、在哪个文件？

> **答案**：`olmocr/pipeline.py` 中的 `cli_main()`（L1526，由 `pyproject.toml` `[project.scripts]` 注册）→ `asyncio.run()` 调用 → `main()`（L1199）。

**练习 2**：`work_queue.py` 里 `Backend`、`LocalBackend`、`S3Backend` 三者的关系是什么？为什么要把 `Backend` 设计成抽象基类？

> **答案**：`Backend` 是抽象基类（L38），用 `@abc.abstractmethod` 规定 8 个存储原语；`LocalBackend`（L238）用本地文件实现，`S3Backend`（L392）用 S3 对象实现。抽象的目的是让上层 `WorkQueue`（L82）完全不关心存储介质——同一套断点续跑逻辑既能在笔记本本地跑，也能在多机 S3 协同跑。

**练习 3**：如果你要在 Python 里打印当前安装的 olmocr 版本号，应该 import 哪个模块、读哪个变量？

> **答案**：`from olmocr.version import VERSION`（或 `import olmocr; olmocr.VERSION`，因为 `olmocr/__init__.py` 第 1 行把它转发出来了；定义在 [olmocr/version.py:11](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/version.py#L11)）。

### 4.2 子包职责速查表：七个子包各管一段

#### 4.2.1 概念说明

顶层文件是"名词表"，子包则是按**生命周期阶段**划分的"车间"：提示词怎么写（prompts）、输入要不要丢弃（filter）、页面怎么变成图片（data）、模型怎么训练（train）、效果怎么评（bench）、训练数据怎么造（synth）、结果怎么人工抽查（viewer）。理解子包 = 理解 olmOCR 这个项目的业务闭环：

```text
真实 PDF ──(pipeline 主流水线，用 prompts/data/filter)──> Markdown/Dolma
                                        │
     synth 合成数据 + data 里的 GPT-4o silver 数据
                                        │
                                        ▼
                            train（SFT + GRPO，奖励来自 bench）
                                        │
                                        ▼
                            bench 评测 ──> viewer 人工抽查
```

#### 4.2.2 核心流程

速查一个子包的三步法：

1. 看子包 `__init__.py` 转发了哪些名字——那是作者认定的"对外接口"。
2. 看 `ls` 里文件名的动词前缀（`build_`/`prepare_`/`run_`/`mine_`）——它们大多是可直接运行的脚本。
3. 问自己：这个包服务的是**推理时**、**训练前**还是**训练后**？

#### 4.2.3 源码精读

逐个子包点名（每条永久链接都指向该子包最核心的定义）：

| 子包 | 职责一句话 | 核心证据（永久链接） |
|---|---|---|
| `prompts/` | 全部提示词模板 + 模型输出的数据契约 `PageResponse` | [prompts/prompts.py:164](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/prompts/prompts.py#L164) `build_no_anchoring_v4_yaml_prompt`（当前推理提示词）、[L67](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/prompts/prompts.py#L67) `PageResponse`；[anchor.py:17](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/prompts/anchor.py#L17) `get_anchor_text` 多引擎 PDF 文本提取；[prompts/\_\_init\_\_.py:1-9](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/prompts/__init__.py#L1-L9) 是对外接口清单 |
| `filter/` | 判断一个 PDF 值不值得送进模型（语言、表单、SEO 垃圾） | [filter/filter.py:14](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/filter/filter.py#L14) `PdfFilter`、[L65](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/filter/filter.py#L65) `filter_out_pdf`；[coherency.py:16](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/filter/coherency.py#L16) 用 SmolLM-135M 语言模型给文本的连贯性打分 |
| `data/` | "PDF ↔ 图片 ↔ 数据集"的搬运车间 | [data/renderpdf.py:39](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/data/renderpdf.py#L39) `render_pdf_to_base64png`（poppler 渲染，被 pipeline/bench/synth/train 四方共用）；`buildsilver.py`、`runopenaibatch.py`、`prepare_*.py` 是构建训练数据的一次性脚本（u7-l1） |
| `train/` | SFT 与 GRPO 训练全流程 | [train/train.py:80](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/train/train.py#L80) `QwenDataCollator`；[train/dataloader.py:94](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/train/dataloader.py#L94) `BaseMarkdownPDFDataset`；[train/front_matter.py:22](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/train/front_matter.py#L22) `FrontMatterParser`（解析模型输出，被 pipeline 反向复用）；[train/muon.py:232](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/train/muon.py#L232) Muon 优化器；`grpo_train.py` 强化学习（u6-l3） |
| `bench/` | olmOCR-Bench：把 OCR 评测做成"单元测试" | [bench/tests.py:23](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/bench/tests.py#L23) `TestType` 枚举、[L47](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/bench/tests.py#L47) `normalize_text`、[L766](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/bench/tests.py#L766) `load_single_test`；`benchmark.py`/`report.py` 评分与报告；`katex/`、`miners/`、`runners/`、`sample_data/` 是它的配套资产（u5 单元） |
| `synth/` | olmOCR-2 合成数据引擎：HTML 模板 → Claude 填内容 → 增强 → PDF → 单测质检 | [synth/mine_html_templates.py:25-50](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/synth/mine_html_templates.py#L25-L50) 的 import 就能看出它同时借力 bench、data、filter；`claude_client.py`/`augmentations.py`/`cutoff_detection.py` 是它的三根支柱（u7-l2） |
| `viewer/` | 把 Dolma 结果 + 原始 PDF + 页面渲染图拼成可翻页比对的 HTML 报告 | [viewer/dolmaviewer.py:20-50](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/viewer/dolmaviewer.py#L20-L50) `get_pdf_bytes_from_source` 支持 `tar.gz::内部文件名` 双冒号语法；同目录两个 `*_template.html` 是 Jinja2 模板（u7-l3） |

**一个值得注意的设计**：`train/front_matter.py` 明明住在 `train/` 包里，却是推理流水线的组成部分——[olmocr/pipeline.py:50](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L50) 直接 `from olmocr.train.front_matter import FrontMatterParser`。原因：训练时解析 silver 数据的 front matter 与推理时解析模型输出的 front matter 是**同一段逻辑**，作者选择让它住在一处、两处复用。这是"包名 ≠ 分层"的好例子。

**子包与 extras 的对应**（承接 u1-l2）：

- [pyproject.toml:52-57](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/pyproject.toml#L52-L57) `gpu` extra（torch/transformers/vllm）→ 解锁**本地运行** `pipeline.py` 与 `train/`；
- [pyproject.toml:92-106](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/pyproject.toml#L92-L106) `bench` extra（playwright/lxml/openai…）→ 解锁 `bench/` 的大部分功能；
- [pyproject.toml:108-118](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/pyproject.toml#L108-L118) `train` extra（accelerate/trl/peft…）→ 解锁 `train/`；
- 而 `prompts/` 只依赖标准库（[prompts/prompts.py:1-3](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/prompts/prompts.py#L1-L3) 只有 `re`/`dataclasses`/`typing`），所以轻量安装也能 import——这正是下面实践的依据。

另外注意 [pyproject.toml:127-135](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/pyproject.toml#L127-L135)：打包时显式排除了 `tests*` 与 `scripts*`——`pip install olmocr` 装到的只有 `olmocr/` 包本身。

#### 4.2.4 代码实践（验证轻量安装的可用边界）

1. **实践目标**：验证"轻量安装（u1-l2 装的 `pip install olmocr`）能 import 哪些子包"，从而亲手确认 extras 与子包的对应关系。
2. **操作步骤**：在激活了 u1-l2 conda 环境的终端里依次执行（每条一行）：

   ```bash
   python -c "import olmocr.prompts; print(olmocr.prompts.__file__)"
   python -c "import olmocr.work_queue; print('work_queue ok')"
   python -c "import olmocr.s3_utils; print('s3_utils ok')"
   python -c "import olmocr.filter; print('filter ok')"
   python -c "import olmocr.train; print('train ok')"
   python -c "import olmocr.bench.tests; print('bench ok')"
   ```

3. **需要观察的现象**：前几条应当成功（`work_queue` 依赖的 `zstandard`/`boto3` 在基础依赖表 [pyproject.toml:22-40](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/pyproject.toml#L22-L40) 里；`prompts` 只用标准库）。而最后两条很可能抛 `ModuleNotFoundError`——`train/dataloader.py` 顶层要 `torch`，`bench/tests.py` 要 `fuzzysearch` 等 bench extra 里的库。
4. **预期结果**：你会得到一张"能 import / 不能 import"的实证清单，与 4.2.3 的 extras 对应表互相印证。具体哪几条失败取决于你安装的 extras 组合，**待本地验证**（本讲义写作环境未安装该包，无法替你确认输出）。
5. 若 `import olmocr.train` 竟然成功了，检查一下是不是曾经装过 `olmocr[train]`——这也说明你环境里已经有训练依赖了。

#### 4.2.5 小练习与答案

**练习 1**：`from olmocr.prompts import PageResponse` 为什么不用写成 `from olmocr.prompts.prompts import PageResponse`？

> **答案**：因为 [olmocr/prompts/\_\_init\_\_.py:1-9](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/prompts/__init__.py#L1-L9) 在包初始化时把 `PageResponse` 等 7 个名字从子模块 `prompts.prompts` 转发到了包的命名空间。这也是作者标记"稳定对外接口"的方式。

**练习 2**：`data/renderpdf.py` 的 `render_pdf_to_base64png` 被哪些子包引用？这说明了什么？

> **答案**：被 `pipeline.py`（L36）、`bench/`（convert.py、runners/、miners/、report.py 等十余处）、`synth/mine_html_templates.py`、`viewer/dolmaviewer.py`、`data/` 内部脚本、`train/grpo_train.py` 引用——它是全仓库被复用最广的函数之一，"把一页 PDF 渲染成模型/评测都能吃的图片"是所有子包的共同底座。

**练习 3**：想给 OCR 结果做人工质量抽查，应该用哪个子包？它依赖哪两个模块获取原始 PDF 和页面图片？

> **答案**：`viewer/`（`dolmaviewer.py`）。它 import 了 [olmocr/viewer/dolmaviewer.py:16-17](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/viewer/dolmaviewer.py#L16-L17)：`data.renderpdf.render_pdf_to_base64webp`（渲染页面）和 `s3_utils`（`get_s3_bytes`/`parse_s3_path` 取原始 PDF，支持 tarball 双冒号路径）。

### 4.3 模块间依赖草图：谁 import 谁

#### 4.3.1 概念说明

架构不是画出来的，是 **import 出来的**。把每个文件里的 `from olmocr.X import ...` 收集起来，就得到一张真实的依赖图。olmocr 的依赖图呈清晰的"沙漏形"：

- **上层（消费端）**：`pipeline.py`（推理）与 `train/`（训练）——两个各自完整的程序；
- **中层（领域层）**：`prompts/`、`data/`、`filter/`、`bench/`——推理和训练都要用的共享能力；
- **底座（基础设施）**：`s3_utils.py`、`work_queue.py`、`metrics.py`、`image_utils.py` 等工具文件。

关键结论（也是本讲规格要求的学习目标）：**`pipeline`、`work_queue`、`train`、`bench` 四者中，`pipeline` 和 `train` 是平级的两个入口；`work_queue` 只服务于 `pipeline`（及其多机扩展）；`bench` 是 `train`（GRPO 奖励）与 `synth`（质检）共同依赖的下游裁判。**

#### 4.3.2 核心流程

推导依赖草图的伪代码：

```text
对 olmocr/ 下每个 .py 文件 F：
    抽取 F 中所有 "from olmocr.X..." 与 "import olmocr.X..." 行
    对每个 X 画边 F → X
忽略指向自身的边，得到边集 E

观察 E：
    1. 入度最高的节点 = 公共底座（预期：data.renderpdf、s3_utils、prompts）
    2. 出度最高的节点 = 总装配厂（预期：pipeline.py）
    3. 若出现环 = 值得警惕的设计（预期：没有）
```

#### 4.3.3 源码精读

四条最有代表性的"依赖边"，每条都给出了铁证：

**边 1：pipeline → 几乎全世界。** [olmocr/pipeline.py:32-52](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L32-L52) 的 import 块一口气引了 `check`、`data.renderpdf`、`filter.filter`、`image_utils`、`metrics`、`prompts`、`prompts.anchor`、`s3_utils`、`train.front_matter`、`version`、`work_queue` 共 11 个内部模块——它是唯一"认识所有车间"的总装配厂：

```python
from olmocr.check import (check_poppler_version, check_torch_gpu_available)
from olmocr.data.renderpdf import render_pdf_to_base64png
from olmocr.filter.filter import Language, PdfFilter
from olmocr.image_utils import convert_image_to_pdf_bytes, is_jpeg, is_png
from olmocr.metrics import MetricsKeeper, WorkerTracker
from olmocr.prompts import PageResponse, build_no_anchoring_v4_yaml_prompt
from olmocr.prompts.anchor import get_anchor_text
from olmocr.s3_utils import (download_directory, download_zstd_csv, ...)
from olmocr.train.front_matter import FrontMatterParser
from olmocr.version import VERSION
from olmocr.work_queue import LocalBackend, S3Backend, WorkQueue
```

**边 2：work_queue → s3_utils。** [olmocr/work_queue.py:16-21](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L16-L21) 只引了 4 个 S3 函数——队列抽象站在存储工具之上，且**不依赖** pipeline/train/bench 任何一个。

**边 3：train → bench（olmOCR-2 的灵魂边）。** [olmocr/train/grpo_train.py:34-38](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/train/grpo_train.py#L34-L38)：

```python
from olmocr.bench.table_parsing import parse_html_tables
from olmocr.bench.tests import load_single_test
from olmocr.data.renderpdf import render_pdf_to_base64png
from olmocr.prompts import PageResponse, build_no_anchoring_v4_yaml_prompt
from olmocr.train.front_matter import FrontMatterParser
```

训练代码 import 评测代码，正是"把 olmOCR-Bench 单元测试当作 GRPO 奖励函数"这一核心创新在依赖图上的投影（u6-l3 精读）。

**边 4：synth → bench + data + filter；viewer → data + s3_utils。** [olmocr/synth/mine_html_templates.py:25-50](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/synth/mine_html_templates.py#L25-L50) 同时引 `bench.tests`（质检）、`data.renderpdf`（渲染）、`filter.filter`（筛选）；[olmocr/viewer/dolmaviewer.py:16-17](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/viewer/dolmaviewer.py#L16-L17) 只引 `data.renderpdf` 与 `s3_utils`。

把这些边汇总成草图（箭头 = "import"）：

```text
pipeline.py ──→ work_queue ──→ s3_utils ←── viewer
     │                            ↑
     ├──→ train.front_matter      │
     ├──→ prompts ←───────────────┼──── train/（grpo_train、dataloader）
     ├──→ data.renderpdf ←────────┼──────── bench/、synth/、viewer
     ├──→ filter.filter ←─────────┼──────── bench/miners、synth、data
     ├──→ metrics、check、image_utils、version
     │
     │            train/grpo_train ──→ bench.tests   ★ 灵魂边
     └──────────── bench/tests ──→ repeatdetect
```

反方向验证：`bench/runners/run_olmocr_pipeline.py` 的 `from olmocr.pipeline import ...` 是评测框架调被测对象，属于"工具脚本调程序"，不改变"pipeline 平时不认识 bench"的结论（pipeline.py 的 import 块里确实没有 `olmocr.bench`）。

#### 4.3.4 代码实践（源码阅读型：用 grep 亲手量出依赖图）

1. **实践目标**：自动生成全仓库的 `olmocr` 内部依赖边清单，验证 4.3.3 的草图。
2. **操作步骤**：在仓库根目录执行：

   ```bash
   grep -rn "^from olmocr\.\|^import olmocr\." olmocr/ --include="*.py" \
     | sed 's/:from olmocr\.\([a-z_.]*\).*/ -> olmocr.\1/' > /tmp/olmocr_deps.txt
   sort /tmp/olmocr_deps.txt | uniq -c | sort -rn | head -40
   ```

   第一条命令把每处内部 import 变成 `文件 -> 模块` 的边；第二条统计每条边出现的次数并按频次排序。

3. **需要观察的现象**：输出里 `-> olmocr.data.renderpdf` 的行会霸榜（十几个文件引它）；`olmocr/pipeline.py -> ...` 只有 1 行却引了 11 个模块；几乎看不到指向 `olmocr.pipeline` 的边（只有 `bench/runners/run_olmocr_pipeline.py` 一处）。
4. **预期结果**：你得到的频次榜应当与"沙漏形"结论一致——`data.renderpdf`、`s3_utils`、`prompts`、`filter.filter` 是被引最多的底座。grep/sed 只做文本处理，任何环境都能跑；本讲义环境未执行此命令，输出细节**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`work_queue.py` 有没有 import `pipeline.py`？这个事实有什么工程意义？

> **答案**：没有——`work_queue.py` 只 import `olmocr.s3_utils`（L16-21）。意义：队列模块可以脱离整条流水线被单独测试、单独复用（u3-l1 的实践就会单独实例化 `LocalBackend` + `WorkQueue`），依赖单向是模块可独立测试的前提。

**练习 2**：`pipeline.py` 的 import 块里有 `from olmocr.train.front_matter import FrontMatterParser`，却不曾 import `olmocr.bench`。结合 u1-l1 讲过的"olmOCR-2 = 合成数据 + GRPO"，说说为什么这个不对称是合理的？

> **答案**：推理流水线只需要"解析模型输出的 front matter"，这段逻辑与训练数据解析同源，所以复用 `train/front_matter.py`；而评测（bench）属于训练/研究侧的下游环节——推理时不需要知道自己会被谁打分。反过来 `train/grpo_train.py` 必须引用 `bench.tests`，因为奖励就是测试通过率。不对称恰恰反映了"推理轻、训练重"的职责切分。

**练习 3**：如果让你新增一个"把结果上传到某内部系统"的功能，按现有依赖方向，它应该放哪里？为什么？

> **答案**：参考 `viewer/` 的做法——做成一个只依赖 `s3_utils`/`data.renderpdf` 等底座的新子包或脚本，让 pipeline 的落盘产物（Dolma JSONL / Markdown）作为它的输入，而不是往 `pipeline.py` 里加 import。保持 pipeline 不认识它，才能不加重这个已经 1500+ 行的总装配厂。

## 5. 综合实践

**任务：制作你自己的《olmocr 模块地图卡片》**（纯源码阅读，不需要 GPU，不需要 pip 安装任何新东西）。

1. **目标**：产出一份一页纸的地图，包含三个区块，以后每读一篇讲义就回来在地图上"点亮"一个模块。
2. **步骤**：
   - **区块 A（名词表）**：完成 4.1.4 的 9 行顶层文件职责表。
   - **区块 B（车间表）**：为 7 个子包各写一行"职责 + 对外接口（看 `__init__.py` 或最核心的一个类/函数）+ 服务的生命周期阶段（推理时/训练前/训练后）"。
   - **区块 C（依赖边）**：运行 4.3.4 的 grep 管线，把 `uniq -c` 频次前 10 的依赖边抄进地图，并标出唯一一条 `train → bench` 边。
   - 最后在地图底部写一行"验证日期 + HEAD 提交号"（当前为 `f7cfe4c`），因为行号会随版本漂移。
3. **自查标准**：只看你的地图、不看讲义，能回答出以下三问即算通过——
   - `olmocr` 命令的入口链条是什么？（cli_main → main）
   - 被最多模块复用的渲染函数在哪？（`data/renderpdf.py` 的 `render_pdf_to_base64png`）
   - 为什么训练代码要 import 评测代码？（GRPO 奖励 = bench 单测通过率）

## 6. 本讲小结

- `olmocr/` 根目录 9 个 `.py` 是"名词表"：`pipeline.py` 是总装配厂与 CLI 入口，`work_queue.py` 提供可断点续跑的队列抽象（`Backend` + 本地/S3 双实现），其余是 metrics/s3/image/check/version 等工具与 `datatypes.py` 这个已无人引用的历史遗留。
- 七个子包按生命周期分工：`prompts`（提示词与输出契约）、`filter`（输入筛选）、`data`（渲染与数据准备）、`train`（SFT/GRPO）、`bench`（单元测试式评测）、`synth`（合成数据）、`viewer`（人工抽查）。
- 依赖图是"沙漏形"：`pipeline` 与 `train` 是平级消费端，`data.renderpdf`/`s3_utils`/`prompts`/`filter` 是被反复复用的腰部底座；`train/grpo_train → bench.tests` 这条"灵魂边"是 olmOCR-2 奖励机制的源码投影。
- `pyproject.toml` 的 extras（`gpu`/`bench`/`train`…）与子包一一对应：轻量安装可 import `prompts`/`work_queue`，但 `train`/`bench` 需要各自的 extras——地图即安装清单。
- 包名 ≠ 分层：`train/front_matter.py` 被推理流水线反向复用，读代码时要以 import 边而非目录名判断真实角色。

## 7. 下一步学习建议

- 下一讲（u1-l5）将精读 `main()` 里的 argparse 定义，把 `olmocr --help` 的几十个参数逐组拆解——建议先把本讲的模块地图放在手边，因为很多参数（如 `--workspace` 对应 work_queue、`--limit-mm-per-prompt` 对应 vLLM）只有知道"归哪个模块管"才好理解。
- 如果你想立刻按地图深入，推荐顺序：第 2 单元沿 `pipeline.py` 调用链逐站精读（u2-l1 从 `main()` 开始），第 3 单元攻 `work_queue.py` 与多机部署。
- 想先睹 `train → bench` 这条灵魂边的读者，可以直接跳读 [olmocr/train/grpo_train.py:34-38](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/train/grpo_train.py#L34-L38) 与 [olmocr/bench/tests.py:766](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/bench/tests.py#L766) 的 `load_single_test`，但完整理解需要第 5、6 单元的铺垫。
