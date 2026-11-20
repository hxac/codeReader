# 第一次运行：把一个 PDF 转成 Markdown

## 1. 本讲目标

学完本讲，你应该能够：

1. 使用 `olmocr` 命令行把一个 PDF 转成 Markdown 文件。
2. 准确说出 workspace（工作目录）里每个产物的作用：`work_index_list.csv.zstd`、`done_flags/`、`worker_locks/`、`results/`、`markdown/`。
3. 说清楚当你在终端敲下 `olmocr` 回车之后，这行命令是如何一步步映射到 `pyproject.toml` 的 `[project.scripts]`、再到 `olmocr/pipeline.py` 的 `cli_main()` 和 `main()` 的——也就是「命令从哪里来」这条链路。
4. 掌握一个没有 GPU 也能观察 workspace 结构的技巧（利用 `--stats` 在 GPU 检查之前短路返回的时序特点）。

## 2. 前置知识

本讲是第一篇「动手跑代码」的讲义，需要以下背景（均在前面两讲建立）：

- **命令行与 Python 环境**：你已经在干净的 conda 环境（Python ≥ 3.11）里用 pip 安装了 `olmocr`（第 1-2 讲），并且系统里装好了 poppler（`pdftoppm` 等命令可用）。
- **轻量安装 vs GPU 安装**：第 1-2 讲我们强调过，`pip install olmocr` 是轻量安装，**不包含 torch / vLLM 等深度学习框架**。本讲的完整转换流程需要推理服务，因此完整跑通有两条路：
  - **本地 GPU**：安装 `pip install "olmocr[gpu]"`，olmOCR 会自动在本机拉起一个 vLLM 服务器；
  - **远程服务器**：用 `--server http://主机:端口/v1` 指向别人托管的推理服务，本机完全不需要 GPU。
- **两个关键概念**：
  - **workspace（工作区）**：olmOCR 不是「原地转换」的工具，它把所有中间状态和结果都写进一个你指定的目录，这个目录就是 workspace。这样设计的好处是**可断点续跑、可多机协同**（第 3 单元会展开）。
  - **同步入口与异步主函数**：Python 的命令行入口必须是普通同步函数，而 olmOCR 的主流程用了 `asyncio` 异步编程（用 `async def` 定义、内部大量并发）。这两者之间需要一座桥，本讲第 4.3 节会看到这座桥。

如果你暂时没有 GPU，完全没问题——本讲的核心实践特意设计成**无 GPU 也能完成大部分**，详见 4.1.4。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|---|---|---|
| `README.md` | 项目说明书 | Usage Examples 一节的官方命令与结果查看方式 |
| `pyproject.toml` | 包的构建与安装配置 | `[project.scripts]` 如何注册 `olmocr` 命令 |
| `olmocr/pipeline.py` | 批处理流水线主文件（约 1500 行） | `main()` 的参数与时序、`worker()` 如何落盘结果、`get_markdown_path()`、`cli_main()` |
| `olmocr/work_queue.py` | 工作队列抽象（本讲只看骨架） | `LocalBackend` 在 workspace 里创建了哪些目录和文件 |

## 4. 核心概念与源码讲解

### 4.1 olmocr 命令行初体验

#### 4.1.1 概念说明

olmOCR 的标准用法是一条命令：指定一个 workspace 目录、一批输入 PDF、以及想要的输出形态。README 给出的第一条命令是：

```bash
# 下载一个示例 PDF（3 页）
curl -o olmocr-sample.pdf https://olmocr.allenai.org/papers/olmocr_3pg_sample.pdf

# 把它转换成 markdown
olmocr ./localworkspace --markdown --pdfs olmocr-sample.pdf
```

这条命令有三个部分：

| 部分 | 类型 | 含义 |
|---|---|---|
| `./localworkspace` | 位置参数（positional） | workspace 路径，所有中间状态和结果都写在这里；也可以是 `s3://` 开头的 S3 路径用于多机协同 |
| `--markdown` | 开关（flag） | 额外把自然文本写成 Markdown 文件，**保留输入 PDF 的目录结构** |
| `--pdfs olmocr-sample.pdf` | 列表参数 | 输入文件；可以跟多个路径、`*.pdf` 通配符、`.txt` 路径清单、S3 glob，PNG/JPEG 图片也行 |

`--pdfs` 不给也合法——那时 olmOCR 会直接读取 workspace 里已有的队列继续干活（断点续跑）。

#### 4.1.2 核心流程

当你运行 `olmocr ./localworkspace --markdown --pdfs x.pdf` 后，`main()` 内部大致按这个顺序推进（本讲只建立地图，第 2 单元逐站精读）：

```
解析参数 (parse_known_args)
  ↓
检查 poppler 可用                     ← 轻量安装也装不了它，必须是系统依赖
  ↓
创建 WorkQueue（本地或 S3 后端）       ← workspace 的目录骨架在这里被创建！
  ↓
收集 --pdfs 输入 → 采样估算页数 → populate_queue 写入队列索引
  ↓
[--stats ?] → 打印统计后直接 return   ← 短路点①：不碰 GPU
[--beaker ?] → 提交集群任务后 return  ← 短路点②
  ↓
检查 GPU（仅在需要本地推理时）
  ↓
下载模型（约 30GB）→ 拉起 vLLM 服务 → 等待就绪
  ↓
启动 N 个 worker 协程并发处理队列
  ↓
输出最终指标汇总
```

注意一个**非常有用的时序细节**：workspace 的创建和队列填充发生在 GPU 检查**之前**。这意味着即使你没有任何 GPU，只要运行到 `--stats` 短路点，workspace 里的队列骨架照样会生成——这是本讲无 GPU 实践的理论依据。

#### 4.1.3 源码精读

先看参数定义。`main()` 是一个 `async def` 函数，一进来先构建 argparse：

- [olmocr/pipeline.py:1199-1215](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1199-L1215)：定义 `workspace` 位置参数（本地目录或 `s3://` 路径）、`--pdfs` 列表参数，以及默认模型 `allenai/olmOCR-2-7B-1025-FP8`（Hugging Face 上的 olmOCR-2 7B FP8 量化权重）。
- [olmocr/pipeline.py:1226-1228](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1226-L1228)：`--stats` 与 `--markdown` 两个开关参数的定义。`--markdown` 的帮助文本明确说了「preserving the folder structure of the input pdfs」。

再看关键时序：

- [olmocr/pipeline.py:1325-1332](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1325-L1332)：先 `check_poppler_version()`（就是第 1-2 讲 `check.py` 里那个 poppler 检查的复用——没有它连输入 PDF 都读不了）；然后根据 workspace 是否以 `s3://` 开头，选择 `S3Backend` 或 `LocalBackend` 构造 `WorkQueue`。**构造 `LocalBackend` 的那一刻，workspace 目录骨架就被创建了**（下一节展开）。
- [olmocr/pipeline.py:1334-1375](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1334-L1375)：`--pdfs` 收集逻辑——S3 glob 展开、本地文件按扩展名和文件头魔数分类（`%PDF` 开头当 PDF，PNG/JPEG 魔数当图片），`.txt` 则按「每行一个路径」的清单读入。
- [olmocr/pipeline.py:1377-1412](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1377-L1412)：随机抽样最多 100 个 PDF 用 `PdfReader` 数页数，估算平均页数后计算 `items_per_group`（目标每组的页数 ÷ 平均页数），再调用 `work_queue.populate_queue()` 写入队列索引。
- [olmocr/pipeline.py:1414-1425](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1414-L1425)：`--stats` 和 `--beaker` 两个短路点先后 `return`；**直到这之后**才调用 `check_torch_gpu_available()`。这就是「无 GPU 也能跑到 `--stats`」的源码依据。
- [olmocr/pipeline.py:1429-1451](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1429-L1451)：需要本地推理时先 `download_model()`（首次运行会下载约 30GB 权重，需要耐心），然后启动 vLLM 服务并等待就绪。
- [olmocr/pipeline.py:1455-1462](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1455-L1462)：按 `--workers`（默认 20）创建 worker 协程并 `asyncio.gather` 等待全部完成。

README 的官方用法与结果查看方式见 [README.md:264-303](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/README.md#L264-L303)：包括单 PDF、图片、多 PDF 通配符、远程 server 四种形态，以及 `cat localworkspace/markdown/olmocr-sample.md` 查看结果。

#### 4.1.4 代码实践

**实践目标**：亲手运行 olmOCR 命令，并根据自己有没有 GPU 选择完整或精简路线，观察命令的输出日志。

**路线 A（推荐所有人先做，无需 GPU，轻量安装即可）**：

1. 操作步骤：
   ```bash
   cd <仓库根目录>   # 仓库自带测试 PDF，省去下载
   olmocr ./localworkspace --stats --pdfs tests/gnarly_pdfs/ambiguous.pdf
   ```
2. 需要观察的现象：
   - 终端先打印加载 `--pdfs`、采样估算页数的日志；
   - 随后打印 workspace 统计信息（`print_stats`，见 [olmocr/pipeline.py:1105](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1105)），然后进程正常退出，**全程没有报 GPU 相关错误**；
   - 当前目录多出了 `localworkspace/`。
3. 预期结果：命令成功退出（退出码 0），`localworkspace/` 下已有队列骨架文件（4.2 节实践会解剖它），但**没有** `results/` 和 `markdown/`——因为还没有任何页面被真正转换。
4. 小提示：若提示缺少 `tqdm` 之类的间接依赖，说明你的轻量环境没带上它，`pip install tqdm` 补齐即可。

**路线 B（有 GPU，或已按第 1-2 讲装好 `olmocr[gpu]`）**：

1. 操作步骤（与 README 完全一致）：
   ```bash
   curl -o olmocr-sample.pdf https://olmocr.allenai.org/papers/olmocr_3pg_sample.pdf
   olmocr ./localworkspace --markdown --pdfs olmocr-sample.pdf
   ```
2. 需要观察的现象：下载模型进度 → vLLM 启动日志 → worker 处理日志 → 结尾的 `FINAL METRICS SUMMARY`（含输入/输出 token 数、失败页率、按尝试次数分布的完成页数）。
3. 预期结果：`localworkspace/markdown/olmocr-sample.md` 出现，内容是该 PDF 三页的 Markdown 文本。若显存不足报 `KV cache is larger than available memory`，按日志提示加 `--gpu_memory_utilization 0.80 --max_model_len 16384` 重试。

**路线 C（无 GPU 但想看完整转换结果，待本地验证）**：向托管了 olmOCR 的推理服务商租用 API（README 列出了 Cirrascale、DeepInfra、Parasail 等），用 `--server URL --api_key KEY --workers 1 --max_concurrent_requests 20` 的形式运行。具体命令见 [README.md:336-351](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/README.md#L336-L351)。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `olmocr ./localworkspace --stats --pdfs x.pdf` 在没有 GPU、也没装 torch 的轻量环境里也能正常运行？

**参考答案**：查看 [olmocr/pipeline.py:1414-1425](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1414-L1425)，`--stats` 分支在 `check_torch_gpu_available()` 之前就 `print_stats(args, work_queue)` 并 `return` 了；而它之前的步骤（poppler 检查、WorkQueue 创建、`--pdfs` 收集）只需要 poppler 和 pypdf 这类轻量依赖。所以进程根本走不到需要 torch 的代码。

**练习 2**：README 里「Convert an Image file」的示例是 `olmocr ./localworkspace --markdown --pdfs random_page.png`。图片是怎么被当作 PDF 流水线的输入的？

**参考答案**：输入收集阶段（[olmocr/pipeline.py:1350-1361](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1350-L1361)）会按魔数判断文件类型，PNG/JPEG 会被归入 `pdf_work_paths`（同一个队列）；后续 `olmocr/image_utils.py` 里的 `convert_image_to_pdf_bytes` 会把图片包成单页 PDF 再走统一流程（第 2 单元 u2-l2 精读）。

**练习 3**：不加 `--pdfs` 直接运行 `olmocr ./localworkspace`（GPU 环境）会发生什么？

**参考答案**：`args.pdfs` 为 `None` 时跳过收集与 `populate_queue`（[olmocr/pipeline.py:1334](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1334) 的 `if args.pdfs:` 不成立），接着 `initialize_queue()` 会加载 workspace 中已有的索引、剔除已完成项。如果队列空了（[olmocr/pipeline.py:1442-1444](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1442-L1444)）就打印 "No work to do, exiting" 退出；否则继续处理剩余任务——这就是断点续跑。

### 4.2 workspace 目录结构观察

#### 4.2.1 概念说明

workspace 是 olmOCR 的「施工现场+仓库」：队列索引、完成标记、锁、Dolma 结果、Markdown 结果全部落在这里。它被设计成**状态的唯一载体**，于是天然获得两个能力：

- **断点续跑**：进程崩了再启动，已完成的项会被跳过；
- **多机协同**：把 workspace 放在 S3 上，多台机器对着同一个队列抢活干（第 3 单元展开）。

理解 workspace 结构，是看懂 olmOCR 全部工程设计的钥匙。

#### 4.2.2 核心流程

一次完整的本地运行之后，workspace 长这样（带注释）：

```
localworkspace/
├── work_index_list.csv.zstd     # 队列总索引：zstd 压缩的 CSV，每行 = 组哈希 + 该组的 PDF 路径
├── done_flags/                  # 完成标记目录：每个完成的工作组一个空文件
│   └── done_<group_hash>.flag
├── worker_locks/                # 工作组锁目录：防止两个 worker 同时领同一个活
│   └── worker_<group_hash>.lock
├── results/                     # Dolma 格式输出（面向预训练语料的 JSONL）
│   └── output_<group_hash>.jsonl
└── markdown/                    # --markdown 时的人类可读输出，保留输入目录结构
    └── olmocr-sample.md
```

生成时序上：前三个（索引、done_flags、worker_locks）在**任务开始前**创建；后两个（results、markdown）由 worker **每完成一组**写入，最后再放一个 done 标记。

#### 4.2.3 源码精读

- [olmocr/work_queue.py:26-27](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L26-L27)：两个目录名常量 `WORKER_LOCKS_DIR = "worker_locks"`、`DONE_FLAGS_DIR = "done_flags"`，本地和 S3 后端共用同一套命名。
- [olmocr/work_queue.py:244-252](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L244-L252)：`LocalBackend.__init__` 确定索引路径为 `<workspace>/work_index_list.csv.zstd`，并立即 `os.makedirs` 创建 workspace、done_flags、worker_locks 三个目录——所以哪怕什么活都没干，目录骨架也先建好。
- [olmocr/work_queue.py:105-111](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L105-L111)：`_compute_workgroup_hash` 把一组路径**排序后**逐个喂给 sha1，得到确定性的组哈希——同一组路径无论在谁机器上算，哈希都相同，这是多机去重的基石。
- [olmocr/work_queue.py:127-151](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L127-L151)：`populate_queue` 先读旧索引做**路径级去重**（已存在的路径不再入队），再把新路径按 `items_per_group` 切组、算哈希，最后把「组哈希 + 各路径」编码成 CSV 行写回 zstd 文件。
- [olmocr/pipeline.py:741-759](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L741-L759)：`worker()` 处理完一个工作组后，把若干 Dolma 文档写成 JSONL，先落临时文件再复制到 `<workspace>/results/output_<hash>.jsonl`（S3 workspace 则改为上传）。
- [olmocr/pipeline.py:765-794](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L765-L794)：若带 `--markdown`，对每个文档取 `Source-File` 元数据，用 `get_markdown_path` 算出输出路径并写入纯文本。
- [olmocr/pipeline.py:654-695](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L654-L695)：`get_markdown_path` 的路径映射规则——本地路径去掉开头的 `/` 变相对路径（保留目录层级），`s3://` 取 bucket 之后的路径，`tarball::内部路径` 特殊拼接；统一清洗掉 `..` 防路径穿越；扩展名换成 `.md`，落到 `<workspace>/markdown/` 之下。这解释了为什么输入 `./olmocr-sample.pdf` 的输出正好是 `markdown/olmocr-sample.md`。
- [olmocr/pipeline.py:796-804](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L796-L804)：更新指标后调用 `work_queue.mark_done(work_item)`——即在 done_flags 里放 `done_<hash>.flag`，该组从此被视为完成。

#### 4.2.4 代码实践

**实践目标**：解剖路线 A 生成的 workspace，亲手解开队列索引并验证哈希算法。

1. 操作步骤（承接 4.1.4 路线 A 的产物；`zstandard` 是 olmocr 的基础依赖，轻量安装就有）：

   ```python
   # decode_work_index.py（示例代码，放在仓库外的任意目录运行）
   import csv, hashlib, zstandard

   with open("localworkspace/work_index_list.csv.zstd", "rb") as f:
       data = zstandard.ZstdDecompressor().decompress(f.read())

   for line in data.decode("utf-8").splitlines():
       row = next(csv.reader([line]))          # CSV 行 -> [组哈希, 路径1, 路径2, ...]
       group_hash, paths = row[0], row[1:]
       manual = hashlib.sha1("".join(sorted(paths)).encode("utf-8")).hexdigest()
       print("组哈希:", group_hash)
       print("路径  :", paths)
       print("手工重算哈希一致:", manual == group_hash)
   ```

2. 需要观察的现象：打印出一行队列记录，`组哈希` 是 40 位十六进制串（sha1），`路径` 是你传入的那个 PDF 的**绝对路径**；手工重算结果与文件中存的一致。
3. 预期结果：`手工重算哈希一致: True`。再用 `ls -R localworkspace` 对比查看：done_flags 与 worker_locks 存在但为空目录（因为 `--stats` 没有真正处理任何页），results、markdown 不存在。
4. 若路线 B 也做了：把同样的脚本跑在完整运行后的 workspace 上，应看到多条队列记录；done_flags 里每个哈希都有对应 flag；`results/output_<hash>.jsonl` 的文件名哈希能在索引里找到同名行。**待本地验证**（取决于是否执行了 GPU 路线）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `work_index_list.csv.zstd` 要用 zstd 压缩、CSV 逐行编码，而不是直接写一个 JSON 文件？

**参考答案**：这个索引在百万 PDF 规模下会有百万级行，zstd 压缩显著减小体积与 S3 传输量；CSV 行式编码（`_encode_csv_row`，[olmocr/work_queue.py:92-103](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L92-L103)）可以正确转义路径中可能出现的逗号、引号等字符，格式简单、可流式逐行处理。这是典型的「工程上够用且稳」的选择。

**练习 2**：输入路径是 `/home/me/docs/report.pdf` 时，`--markdown` 的输出文件在 workspace 的什么位置？如果输入是 `s3://bucket/2024/papers/a.pdf` 呢？

**参考答案**：查 `get_markdown_path`（[olmocr/pipeline.py:654-695](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L654-L695)）：本地路径去掉开头的 `/` 得到 `home/me/docs/report.pdf`，所以输出是 `<workspace>/markdown/home/me/docs/report.md`；S3 路径取 bucket 之后的 `2024/papers/a.pdf`，输出 `<workspace>/markdown/2024/papers/a.md`。

**练习 3**：假设 worker 处理到一半进程被 kill，重启后同一个工作组会被重复处理吗？已经写了一半的 `results/output_xxx.jsonl` 会造成问题吗？

**参考答案**：不会被重复「完成」，但可能被重复「执行」。锁与标记机制（[olmocr/work_queue.py:174](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L174) 的 `get_work` 会跳过已完成和已上锁的组）保证并发安全；但 done flag 只在整组**全部完成并落盘后**才写（[olmocr/pipeline.py:802](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L802)），所以被 kill 的组没有 flag，重启后会整组重跑并用同名 `output_<hash>.jsonl` **整体覆盖**旧的部分写入文件（worker 每次都整文件重写），最终状态仍然正确——以「最终一致」换实现简单。

### 4.3 pyproject 入口脚本映射

#### 4.3.1 概念说明

终端里的 `olmocr` 既不是编译出的二进制，也不是 shell 脚本，而是 Python 打包系统的**console script（控制台脚本）**机制：pip 安装包时，读取包元数据中的 `[project.scripts]` 表，在环境的 `bin/` 目录下生成一个极小的可执行包装器，它 import 你指定的函数并调用。所以「`olmocr` 这个命令」的全部秘密就在 `pyproject.toml` 的一行配置里。

此外还有一座「同步-异步之桥」：console script 入口必须是普通同步函数，而主流程 `main()` 是 `async def`。`cli_main()` 用 `asyncio.run()` 把两者接起来。

#### 4.3.2 核心流程

```
你在终端敲: olmocr ./localworkspace --markdown --pdfs x.pdf
   ↓
环境的 bin/olmocr 包装脚本（pip 安装时生成）
   ↓ import
pyproject.toml [project.scripts] 指向的 olmocr.pipeline:cli_main
   ↓
cli_main()  ──asyncio.run()──▶  main()   （异步世界：协程、TaskGroup、信号量……）
   ↓
argparse 解析参数 → 后续所有流程（见 4.1.2）
```

等价入口：`python -m olmocr.pipeline 参数...`——它依赖 `pipeline.py` 文件末尾的 `if __name__ == "__main__"` 守卫，README 也专门提到了这一点。

#### 4.3.3 源码精读

- [pyproject.toml:49-50](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/pyproject.toml#L49-L50)：`[project.scripts]` 表声明 `olmocr = "olmocr.pipeline:cli_main"`——命令名 `olmocr`，调用 `olmocr.pipeline` 模块里的 `cli_main` 函数。第 1-2 讲说「olmocr 命令由 scripts 入口注册」，指的就是这两行。
- [olmocr/pipeline.py:1526-1528](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1526-L1528)：`cli_main` 只有 3 行——`asyncio.run(main())`。这就是同步命令行世界与异步流水线之间唯一的桥。
- [olmocr/pipeline.py:1531-1532](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1531-L1532)：`if __name__ == "__main__": cli_main()`，使 `python -m olmocr.pipeline` 与 `olmocr` 完全等价（[README.md:294](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/README.md#L294) 的 Note）。
- 顺带留意 [pyproject.toml:21-40](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/pyproject.toml#L21-L40)：`requires-python = ">=3.11"` 与基础依赖表——这解释了为什么第 1-2 讲的轻量安装就能跑通本讲路线 A：`pypdf`（读页数）、`zstandard`（索引压缩）都在基础依赖里，而 torch 在 `gpu` extra 中（[pyproject.toml:53-57](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/pyproject.toml#L53-L57)）。

#### 4.3.4 代码实践

**实践目标**：亲眼看到 pip 生成的 console script 包装器，验证 `olmocr` 命令与 `cli_main` 的映射关系。

1. 操作步骤：
   ```bash
   which olmocr          # 找到命令的真实位置，应在 conda 环境的 bin/ 下
   head -n 5 "$(which olmocr)"   # 查看包装器内容
   python -c "from olmocr.pipeline import cli_main; print(cli_main)"
   ```
2. 需要观察的现象：`head` 输出一个以 `#!/path/to/python` 开头的小脚本（或 setuptools 生成的等价形式），其中引用了 `olmocr.pipeline` 模块的 `cli_main`；`python -c` 打印出 `<function cli_main at 0x...>`。
3. 预期结果：三步都成功，证明「`olmocr` 命令 = import olmocr.pipeline 并调用 cli_main」这条链路真实存在。不同操作系统/安装方式下包装器细节可能不同，但 import 关系不变。
4. 待本地验证项：包装器具体长相随 setuptools 版本略有差异，属正常现象。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `pyproject.toml` 里的 `[project.scripts]` 改成 `olmocr2 = "olmocr.pipeline:cli_main"` 并重新 `pip install -e .`，会发生什么？

**参考答案**：环境里会多出一个 `olmocr2` 命令且行为与 `olmocr` 完全相同，原 `olmocr` 命令是否保留取决于安装器是否清理旧入口。这说明命令名只是元数据里的一个键，入口函数才是本体。（本练习是思想实验，不要在源码里真改。）

**练习 2**：`cli_main` 为什么不直接写成 `def cli_main(): main()`？

**参考答案**：`main()` 是 `async def` 协程函数，直接调用只会返回一个协程对象而不会执行任何流水线代码。必须由 `asyncio.run()`（[olmocr/pipeline.py:1526-1528](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1526-L1528)）创建事件循环去驱动它；流水线里的 worker、vLLM 服务管理、HTTP 请求全都跑在这个事件循环上。

**练习 3**：`olmocr --help` 和 `python -m olmocr.pipeline --help` 的输出会有本质区别吗？

**参考答案**：不会。`--help` 由 argparse 在 `main()` 开头处理（[olmocr/pipeline.py:1199-1200](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1199-L1200)），两个入口最终都进入同一个 `main()`；唯一可见差别是 usage 行里的程序名（`pipeline.py` vs `olmocr`）。README 的 `--help` 完整输出见 [README.md:431-436](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/README.md#L431-L436)。

## 5. 综合实践

**任务：走完一次「输入 → workspace → 输出」的完整旅程，并写出可核查的证据链。**

1. 准备输入：从 `tests/gnarly_pdfs/` 里挑一个页数少的 PDF（例如 `ambiguous.pdf`），复制到一个带层级的目录，如 `./mypdfs/sub/demo.pdf`——这样能观察 markdown 输出对目录结构的保留。
2. 建立队列骨架（无 GPU 也行）：`olmocr ./localworkspace --stats --pdfs mypdfs/sub/demo.pdf`，然后：
   - 画出 `localworkspace/` 的目录树并给每一项写一句注释（对照 4.2.2 的图）；
   - 运行 4.2.4 的 `decode_work_index.py`，记录组哈希与路径，确认手工重算一致。
3. （有条件者）完整转换：`olmocr ./localworkspace --markdown --pdfs mypdfs/sub/demo.pdf`，验证：
   - `localworkspace/markdown/mypdfs/sub/demo.md` 存在且目录层级被保留；
   - `results/output_<hash>.jsonl` 的哈希与索引中的组哈希一致；
   - done_flags 里出现对应 flag。
4. 幂等性检验：把第 2 步（或第 3 步）的命令**原样再跑一遍**。预期：`populate_queue` 的去重逻辑（[olmocr/work_queue.py:136-140](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L136-L140)）使索引不会新增重复行；完整跑过的组会因 done flag 被跳过，第二次几乎立刻完成。
5. 交付物：一张带注释的目录树 + 索引解码输出 + 3-5 句结论（重点回答：workspace 里哪些是「调度状态」，哪些是「转换结果」？）。

## 6. 本讲小结

- `olmocr ./localworkspace --markdown --pdfs x.pdf` 是标准用法：workspace 位置参数 + `--markdown` 开关 + `--pdfs` 输入列表（支持 PDF/图片/txt 清单/S3 glob）。
- `main()` 的时序要点：**workspace 骨架与队列填充发生在 GPU 检查之前**，`--stats` 在碰 GPU 前短路返回——因此轻量环境也能完成队列搭建与观察。
- workspace 五大产物：`work_index_list.csv.zstd`（zstd 压缩的 CSV 队列索引，每行 = sha1 组哈希 + 路径）、`done_flags/`（完成标记）、`worker_locks/`（并发锁）、`results/`（Dolma JSONL）、`markdown/`（`--markdown` 时按输入目录结构落盘的 `.md`）。
- 组哈希 = 对组内路径**排序后**做 sha1，确定性保证跨机器一致；done flag 只在整组完成落盘后写入，重跑用整文件覆盖保证最终一致。
- `olmocr` 命令来自 `pyproject.toml` 的 `[project.scripts] olmocr = "olmocr.pipeline:cli_main"`；`cli_main` 用 `asyncio.run(main())` 连接同步入口与异步流水线；`python -m olmocr.pipeline` 是等价入口。

## 7. 下一步学习建议

- **u1-l4（仓库结构与模块地图）**：本讲只打开了 `pipeline.py` 和 `work_queue.py` 两个文件，下一讲把 `olmocr/` 包的全部子模块（prompts、filter、data、train、bench、synth、viewer 等）过一遍，建立完整代码地图。
- **u1-l5（CLI 参数全解析）**：本讲只用了 `workspace`、`--pdfs`、`--markdown`、`--stats` 四个参数，下一讲把 argparse 定义与 `--help` 输出一一对应，并理解 `parse_known_args` 把未知参数转发给 vLLM 的机制。
- **提前预习**：如果你好奇一个 PDF 页面从「渲染成图片」到「模型输出 Markdown」中间发生了什么，可以直接跳读第 2 单元 u2-l1（pipeline 主流程），本讲的 `main()` 时序图就是它的起点。
