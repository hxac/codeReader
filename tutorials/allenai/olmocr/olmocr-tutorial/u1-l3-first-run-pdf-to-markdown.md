# 第一次运行：把一个 PDF 转成 Markdown

## 1. 本讲目标

学完本讲，你应该能够：

1. 独立运行 `olmocr ./localworkspace --markdown --pdfs <你的pdf>` 这条核心命令，把一个 PDF 转成 Markdown。
2. 说出 workspace（工作目录）里每个产物的作用：`work_index_list.csv.zstd`、`done_flags/`、`worker_locks/`、`results/output_*.jsonl`、`markdown/`。
3. 解释在终端敲下 `olmocr` 三个字符后，程序如何经由 `pyproject.toml` 的 `[project.scripts]` 找到 `olmocr/pipeline.py` 里的 `cli_main()` 函数，并知道 `python -m olmocr.pipeline` 是等价写法。
4. 在没有 GPU 的机器上，也能用代码亲手「部分复现」workspace 的创建过程，观察队列文件的结构。

## 2. 前置知识

- **CLI（命令行接口）**：在终端里通过敲命令与程序交互的方式。`olmocr` 就是一个 CLI 程序，它的参数（如 `--markdown`、`--pdfs`）由 Python 标准库 `argparse` 解析。
- **入口点（entry point）**：Python 包在安装时可以「注册」一个命令名到某个函数的映射。安装 olmocr 后，conda 环境的 `bin/` 目录下会生成一个叫 `olmocr` 的可执行脚本，它知道要调用 `olmocr.pipeline` 模块里的 `cli_main` 函数。
- **asyncio**：Python 的异步并发框架。olmocr 的主流程 `main()` 是一个协程，用 `asyncio.run()` 启动，这样才能同时管理 vLLM 服务器、多个 worker 和指标上报等并发任务。本讲只需记住「一条 `olmocr` 命令 = 一次 `asyncio.run(main())`」即可，细节留到单元 2。
- **workspace（工作目录）**：你传给 `olmocr` 的第一个位置参数（如 `./localworkspace`）。它不是临时的——流水线会把任务清单、完成标记、最终结果全部写在里面。正因为它持久化在磁盘（或 S3）上，多个节点才能共享同一个队列，这是 olmocr 能扩展到百万级 PDF 的基础。
- **Dolma 格式**：AI2 面向语言模型预训练的 JSON 文档格式（含 `id`、`text`、`metadata` 等字段）。即使你加了 `--markdown`，流水线也**总是**同时产出 Dolma JSONL；Markdown 只是额外赠送的人类友好视图。
- **GPU 的角色**：本地推理需要 NVIDIA GPU（≥12GB 显存）和约 30GB 磁盘放模型权重。没有 GPU 时可以用 `--server` 指向远程推理服务（第 1 讲的轻量安装正是为此准备的）。本讲的多个实践特意设计成**不需要 GPU** 也能完成。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [README.md](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/README.md) | 官方用法示例：第一条转换命令、查看结果的 `cat` 命令、`--help` 完整输出 |
| [pyproject.toml](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/pyproject.toml) | 包定义；`[project.scripts]` 里注册了 `olmocr` 命令到 `cli_main` |
| [olmocr/pipeline.py](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py) | 整个批处理流水线：`main()` 参数解析与任务编排、`worker()` 结果落盘、`get_markdown_path()` Markdown 路径计算、`cli_main()` 同步入口 |
| [olmocr/work_queue.py](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py) | 工作队列：`WorkQueue` 分组与调度，`LocalBackend` 在本地磁盘上创建 `work_index_list.csv.zstd`、`done_flags/`、`worker_locks/` |

## 4. 核心概念与源码讲解

### 4.1 模块一：olmocr 命令行初体验——一条命令背后发生了什么

#### 4.1.1 概念说明

README 给出的第一条命令是：

```bash
olmocr ./localworkspace --markdown --pdfs olmocr-sample.pdf
```

三个组成部分：

- `./localworkspace`：**位置参数** workspace，所有中间状态和结果都写到这里；
- `--markdown`：开关（flag），额外把自然文本写成 `.md` 文件；
- `--pdfs olmocr-sample.pdf`：要转换的文档，可以给多个、可以给 PNG/JPEG、可以给 `*.pdf` 通配符，也可以给一个装着路径列表的 `.txt` 文件。

一条命令内部其实跑完了一整套流水线：解析参数 → 建/更新工作队列 → （本地模式下）下载约 30GB 模型 → 启动 vLLM 推理服务器 → 拉起 20 个 worker 协程消费队列 → 写结果 → 打印最终指标。本讲先建立这条主干的「检查点地图」，单元 2 再逐站精读。

#### 4.1.2 核心流程

`main()` 的执行顺序（省略细节）：

```
解析参数（parse_known_args，未知参数转发给 vLLM）
    │
    ├─ 创建 WorkQueue：workspace 以 s3:// 开头 → S3Backend，否则 → LocalBackend
    │
    ├─ 有 --pdfs？→ 逐个识别（本地 PDF/图片/txt 列表/S3 glob/tarball）
    │       → 采样估算平均页数 → 算出 items_per_group → populate_queue 写入索引
    │
    ├─ --stats？→ 只打印 workspace 统计然后退出（不需要 GPU）
    ├─ --beaker？→ 只提交集群任务然后退出
    │
    ├─ 本地模式：检查 GPU → 下载模型 → 启动 vLLM 服务器 → 等待就绪
    │
    ├─ initialize_queue（剔除已完成项、打乱顺序）
    │
    ├─ 启动 N 个 worker 协程（默认 20）并发消费队列
    │
    └─ 汇总打印 FINAL METRICS SUMMARY，结束
```

分组大小的估算公式为：

\[ \text{items\_per\_group} = \max\left(1,\ \left\lfloor \frac{\text{pages\_per\_group}}{\text{avg\_pages\_per\_pdf}} \right\rfloor\right) \]

即「希望每组约 `pages_per_group` 页（默认 500）」，除以抽样得到的平均页数，得到每组应装多少个 PDF。单页 PDF 会让组里塞进更多文件，厚报告则每组只装一两个。

#### 4.1.3 源码精读

**① 命令的起点：`cli_main` 把异步的 `main()` 包成同步入口**

[olmocr/pipeline.py:1526-1532](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1526-L1532)——`cli_main` 只有一行实事：`asyncio.run(main())`。文件末尾的 `if __name__ == "__main__"` 让 `python -m olmocr.pipeline` 也能走同一条路（README 中也提到了这个等价写法）。

```python
def cli_main():
    """Synchronous entry point for the CLI."""
    return asyncio.run(main())

if __name__ == "__main__":
    cli_main()
```

**② workspace 是位置参数，`--pdfs` 决定要处理什么**

[olmocr/pipeline.py:1199-1215](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1199-L1215)——`main()` 的第一步是构造 `argparse` 解析器。注意 `workspace` 是不带 `--` 的位置参数，`--pdfs` 用 `nargs="*"` 接收任意多个路径。

**③ `--markdown` 开关**

[olmocr/pipeline.py:1228](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1228)——`action="store_true"` 意味着「出现即为 True」，帮助文本说明它会按输入 PDF 的目录结构写 Markdown 文件。

**④ 建队列前先检查 poppler**

[olmocr/pipeline.py:1325-1332](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1325-L1332)——呼应第 2 讲：即使本节点不做推理，加载 PDF 也需要 poppler。随后按 workspace 前缀选择后端：`s3://` 开头用 `S3Backend`（多节点共享队列），否则用 `LocalBackend`（本讲的 `./localworkspace` 走这条）。

```python
check_poppler_version()

if args.workspace.startswith("s3://"):
    work_queue = WorkQueue(S3Backend(workspace_s3, args.workspace))
else:
    work_queue = WorkQueue(LocalBackend(args.workspace))
```

**⑤ `--pdfs` 的识别分支**

[olmocr/pipeline.py:1334-1373](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1334-L1373)——本地文件按内容分类：读前 4 个字节判断是否 `b"%PDF"` 魔数；否则用 `is_png`/`is_jpeg` 探测图片；`.txt` 文件被当作「路径清单」逐行读取；都不匹配则抛 `ValueError`。这一段是第 2 讲（输入收集）的主角，这里只需知道「你的 `olmocr-sample.pdf` 在这里被验明正身，加进 `pdf_work_paths` 集合」。

**⑥ 采样估算分组，写入队列索引**

[olmocr/pipeline.py:1378-1408](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1378-L1408)——随机抽最多 100 个 PDF 用 `PdfReader` 数页数，套用上面的公式得到 `items_per_group`，然后 `populate_queue` 把路径分组写入 workspace 的索引文件。

**⑦ `--stats` 是免 GPU 的旁路**

[olmocr/pipeline.py:1414-1420](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1414-L1420)——`--stats` 分支在 GPU 检查（第 1424 行的 `check_torch_gpu_available()`）**之前**就 `return` 了，所以轻量安装也能用它查看 workspace 的完成情况和产出统计（统计逻辑在 [olmocr/pipeline.py:1105](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1105) 的 `print_stats` 里）。

**⑧ 干活的部分：下载模型、起 vLLM、拉 worker**

[olmocr/pipeline.py:1422-1462](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1422-L1462)——本地模式（没给 `--server`）时先 `download_model`，再把 `args.server` 指向 `http://localhost:{port}/v1`；随后 `initialize_queue` 剔除已完成项并打乱顺序；最后循环 `asyncio.create_task(worker(...))` 创建 `args.workers`（默认 20）个 worker 协程，`asyncio.gather` 等它们全部结束。

#### 4.1.4 代码实践

**实践：对比两种等价的启动方式（无需 GPU，约 2 分钟）**

1. **实践目标**：验证 `olmocr` 命令与 `python -m olmocr.pipeline` 走的是同一个入口。
2. **操作步骤**：
   ```bash
   conda activate olmocr          # 第 2 讲创建的环境
   olmocr --help | head -5
   python -m olmocr.pipeline --help | head -5
   ```
3. **需要观察的现象**：两次输出的 `usage:` 行都以 `pipeline.py` 开头、参数列表完全一致（因为 `argparse` 的 prog 名默认取自脚本文件名）。
4. **预期结果**：两份帮助文本逐字相同；再试 `olmocr`（不带参数）会报 `the following arguments are required: workspace`，这正说明 workspace 是必需的位置参数。
5. 输出内容与机器环境有关，具体文本**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：如果不加 `--markdown` 只运行 `olmocr ./localworkspace --pdfs a.pdf`，还会得到 Markdown 文件吗？

答案：不会。Markdown 写入完全由 [olmocr/pipeline.py:766](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L766) 的 `if args.markdown:` 守卫；但 Dolma JSONL（`results/output_*.jsonl`）**始终**会生成，Markdown 只是从 Dolma 文档的 `text` 字段派生出的附加视图。

**练习 2**：为什么 README 特别提醒可以用 `--server` 指向外部服务器？

答案：本地模式要求 GPU + 约 30GB 模型下载（[olmocr/pipeline.py:1430-1434](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1430-L1434) 的 `download_model` 与内部 vLLM）。给出 `--server` 后 `use_internal_server` 为 False，跳过这两步，只做「渲染页面 → 发 HTTP 请求」，所以第 2 讲的轻量安装（不含 torch）就够用。

**练习 3**：`--workers 20` 和 `--max_concurrent_requests 1600` 分别限制什么？

答案：前者是同时运行的 **worker 协程**数量（[olmocr/pipeline.py:1457-1459](https://github.com/allenai/olmocr/blob/f7cfe4c22098b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1457-L1459) 的循环次数），每个 worker 领一组 PDF；后者是同时在飞的 HTTP 推理请求数上限（用 `asyncio.BoundedSemaphore` 实现，见 [olmocr/pipeline.py:1289](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1289)）。

### 4.2 模块二：workspace 目录结构观察——流水线在磁盘上留下了什么

#### 4.2.1 概念说明

运行结束后，`./localworkspace` 大致长这样：

```
localworkspace/
├── work_index_list.csv.zstd      # 任务清单：zstd 压缩的 CSV，每行 = 组哈希 + 该组的 PDF 路径
├── done_flags/                   # 完成标记：每组一个 done_{hash}.flag 空文件
│   └── done_{hash}.flag
├── worker_locks/                 # 领取锁：worker_{hash}.lock，防止两个节点做同一组活
│   └── worker_{hash}.lock
├── results/
│   └── output_{hash}.jsonl       # 每组一个 Dolma 文档输出文件
└── markdown/                     # --markdown 时才有，按源路径镜像目录结构
    └── olmocr-sample.md
```

设计思想：**用文件系统的存在性当数据库**。不需要 SQLite 或消息队列——「这组活干完了吗」就看 `done_flags/` 里有没有对应 `.flag`，「有没有人正在干」就看 `worker_locks/` 里的锁文件是否新鲜。换成 S3 上的同名对象后，同一套协议就变成了多节点协调机制（单元 3 详述）。

#### 4.2.2 核心流程

workspace 里各类文件的生成时序：

```
olmocr 命令启动
  │
  ├─ LocalBackend.__init__ → 立即创建 workspace/、done_flags/、worker_locks/ 三个目录
  │
  ├─ populate_queue → 生成 work_index_list.csv.zstd（新增路径分组后整体重写）
  │
  ├─ worker 领任务 → 创建 worker_{hash}.lock
  │       ├─ 处理组内每个 PDF（渲染 → 推理 → 拼 Dolma 文档）
  │       ├─ 写 results/output_{hash}.jsonl
  │       ├─ --markdown 时：按 Source-File 镜像路径写 markdown/**.md
  │       └─ mark_done → 创建 done_{hash}.flag，删除 worker_{hash}.lock
  │
  └─ 再次运行同一命令 → initialize_queue 读索引、剔除已 done 的组，只做剩余的活
```

组哈希的计算：对组内路径**排序后**依次喂给 sha1——

\[ \text{hash} = \mathrm{sha1}\big(\texttt{concat}(\mathrm{sorted}(\text{paths}))\big) \]

排序保证了同一组文件无论以什么顺序传入，哈希都相同，因此 `output_{hash}.jsonl` 的文件名是确定性的、可复现的。

#### 4.2.3 源码精读

**① 两个约定俗成的目录名**

[olmocr/work_queue.py:25-27](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L25-L27)——`WORKER_LOCKS_DIR = "worker_locks"`、`DONE_FLAGS_DIR = "done_flags"`，本地与 S3 后端共用这两个名字。

**② LocalBackend 构造时就把目录建好**

[olmocr/work_queue.py:244-252](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L244-L252)——三个 `os.makedirs(..., exist_ok=True)` 解释了为什么命令一启动 workspace 里就出现两个空目录。索引文件路径也在这里定死为 `work_index_list.csv.zstd`。

**③ 索引文件就是 zstd 压缩的 CSV**

[olmocr/work_queue.py:265-295](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L265-L295)——读取时解压再按行拆；写入时把所有行 `"\n".join` 后整体压缩覆盖。每行是 CSV 编码的 `[组哈希, 路径1, 路径2, ...]`。

**④ 哈希与分组**

[olmocr/work_queue.py:105-111](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L105-L111)——`_compute_workgroup_hash`：`sorted(work_paths)` 后逐个 `sha1.update(path)`，保证确定性。

[olmocr/work_queue.py:127-151](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L127-L151)——`populate_queue`：先读旧索引，**减去已存在的路径**（去重，重复跑同一命令不会重复干活），新路径按 `items_per_group` 切片分组，然后合并重写索引。

**⑤ worker 把结果写到哪里**

[olmocr/pipeline.py:741-763](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L741-L763)——先写临时文件，再拷贝到 `os.path.join(args.workspace, "results", f"output_{work_item.hash}.jsonl")`；本地 workspace 走 `shutil.copyfile`，S3 workspace 走 `upload_file`。**先临时后落位**的写法保证 `results/` 里永远不会出现半截文件。

**⑥ Markdown 的路径镜像规则**

[olmocr/pipeline.py:654-695](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L654-L695)——`get_markdown_path`：本地路径去掉开头的 `/`，剔除 `..` 防路径穿越，扩展名换成 `.md`，放进 `workspace/markdown/` 下。所以传 `tests/gnarly_pdfs/dolma-page-1.pdf` 会得到 `localworkspace/markdown/tests/gnarly_pdfs/dolma-page-1.md`；README 里传当前目录的 `olmocr-sample.pdf`，就得到 `localworkspace/markdown/olmocr-sample.md`（对应 README 的 `cat localworkspace/markdown/olmocr-sample.md`）。

**⑦ Markdown 内容来自 Dolma 文档的 text 字段**

[olmocr/pipeline.py:765-794](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L765-L794)——`--markdown` 时遍历每个 Dolma 文档，取 `doc["metadata"]["Source-File"]` 算输出路径，把 `doc["text"]` 原样写入。而 `text` 是各页 `natural_text` 用换行拼接的结果（见 [olmocr/pipeline.py:602-651](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L602-L651) 的 `build_dolma_document`，它同时记录 `pdf_page_spans` 字符偏移、token 统计等元数据——单元 2 第 8 讲的主角）。

#### 4.2.4 代码实践

**实践：不跑推理，亲手制造一个 workspace（无需 GPU，约 5 分钟）**

1. **实践目标**：绕过模型推理，直接用 `WorkQueue` + `LocalBackend` 复现 workspace 的创建与去重行为，看懂索引文件内容。
2. **操作步骤**：把下面的脚本存为仓库外的 `make_workspace.py`（示例代码，不属于 olmocr 项目）：

   ```python
   import asyncio
   from olmocr.work_queue import WorkQueue, LocalBackend, Backend

   async def main():
       wq = WorkQueue(LocalBackend("./miniworkspace"))
       # 假装有三个要处理的 PDF（文件不需要真的存在）
       await wq.populate_queue(
           ["tests/gnarly_pdfs/dolma-page-1.pdf",
            "tests/gnarly_pdfs/olmo-page-1.pdf",
            "tests/gnarly_pdfs/map1.pdf"],
           items_per_group=2,   # 期望每组 2 个文件 → 分成 2 组
       )
       # 再跑一次同样的 populate，验证去重
       await wq.populate_queue(
           ["tests/gnarly_pdfs/dolma-page-1.pdf"],
           items_per_group=2,
       )
       n = await wq.initialize_queue()
       print(f"queue size after dedup: {n}")

   asyncio.run(main())
   ```

   然后在 conda 环境里运行 `python make_workspace.py`，接着观察产物：

   ```bash
   find ./miniworkspace -type f -o -type d | sort
   python - <<'EOF'
   import zstandard
   data = zstandard.ZstdDecompressor().decompress(open("./miniworkspace/work_index_list.csv.zstd","rb").read())
   print(data.decode("utf-8"))
   EOF
   ```

3. **需要观察的现象**：`miniworkspace/` 下出现 `done_flags/`、`worker_locks/` 两个空目录和 `work_index_list.csv.zstd`；解压后的 CSV 有两行，每行以 40 位十六进制哈希开头，后跟 2 个和 1 个路径；脚本打印 `queue size after dedup: 2`。
4. **预期结果**：第二次 `populate_queue` 传入重复路径后索引没有新增行（去重逻辑生效）；`results/` 与 `markdown/` 不存在——它们只在 worker 真正完成推理后创建。解压脚本的原理与 [olmocr/work_queue.py:265-277](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L265-L277) 完全一致。
5. 环境正常时应稳定复现；若 olmocr 未安装到当前环境会 `ModuleNotFoundError`，请先回到第 2 讲完成安装。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `work_index_list.csv.zstd` 用 zstd 压缩而不是普通 CSV？

答案：这个清单在百万级 PDF 场景可能有几十万行、几十 MB 文本；zstd 压缩比高且解压极快，落在 S3 上还能显著减少存储与下载开销。逻辑上它仍然是「每行一条记录」的 CSV（[olmocr/work_queue.py:265-295](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L265-L295)），人类可以用一条 `zstandard` 解压命令直接读懂。

**练习 2**：`done_{hash}.flag` 是空文件，程序怎么知道它「新鲜」？

答案：对 done flag 只看**存在性**（[olmocr/work_queue.py:297-303](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L297-L303) 列出目录里所有 `done_*.flag` 提取哈希）。需要看时间戳的是 **worker 锁**：锁的 mtime 超过 1800 秒没更新就被视为 stale（死进程残留），其他 worker 可以抢占（`is_worker_lock_taken` 的超时参数，见 [olmocr/work_queue.py:62](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L62)）。

**练习 3**：你把同一个 PDF 用绝对路径 `/data/papers/report.pdf` 和相对路径 `papers/report.pdf` 各传一次，会发生什么？

答案：会被当成**两个不同的任务**。索引按字符串去重（[olmocr/work_queue.py:136-137](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py#L136-L137)），两种写法字符串不同、组哈希不同，于是转换两遍、写两个 Markdown（绝对路径的那个会被 `get_markdown_path` 去掉开头 `/`，落在 `markdown/data/papers/report.md`）。实践中应保持路径写法一致。

### 4.3 模块三：pyproject 入口脚本映射——从 `olmocr` 命令到 `cli_main` 函数

#### 4.3.1 概念说明

你也许好奇：终端凭什么认识 `olmocr` 这个命令？答案藏在打包配置里。现代 Python 包用 `pyproject.toml` 的 `[project.scripts]` 表声明「控制台脚本」：左边是命令名，右边是 `模块:函数`。`pip install` 时，setuptools 会在环境的 `bin/` 目录生成一个与命令同名的小启动器，它 import 指定模块并调用指定函数。所以 `olmocr` 命令的本质只是一句「调用 `olmocr.pipeline.cli_main()`」。

#### 4.3.2 核心流程

```
pip install olmocr
    │
    ├─ setuptools 读取 [project.scripts]:  olmocr = "olmocr.pipeline:cli_main"
    ├─ 在 conda 环境的 bin/ 下生成可执行启动器脚本 `olmocr`
    │
终端输入 olmocr ...
    │
    ├─ shell 从 PATH 找到 bin/olmocr
    ├─ 启动器 import olmocr.pipeline，调用 cli_main()
    ├─ cli_main() → asyncio.run(main())
    └─ main() 解析你给的参数，开始干活
```

同理，`python -m olmocr.pipeline` 让 Python 直接把该模块当脚本执行，触发文件末尾的 `if __name__ == "__main__": cli_main()`——两条路殊途同归。

#### 4.3.3 源码精读

**① 入口注册**

[pyproject.toml:49-50](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/pyproject.toml#L49-L50)——整个映射关系的唯一事实来源：

```toml
[project.scripts]
olmocr = "olmocr.pipeline:cli_main"
```

**② 被指向的函数**

[olmocr/pipeline.py:1526-1528](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1526-L1528)——`cli_main` 是同步包装器；`main()` 本身是 `async def`（[olmocr/pipeline.py:1199](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1199)），因为流水线内部大量使用 asyncio 并发。入口点是同步的这一细节，使得从注册到运行需要一次 `asyncio.run` 转接。

**③ README 的等价写法说明**

[README.md:264-294](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/README.md#L264-L294)——README 的 Usage Examples 一节给出从单 PDF、多 PDF 到远程服务器的全部命令样板，并在末尾注明 `python -m olmocr.pipeline` 可以替代 `olmocr`。查看结果的 `cat localworkspace/markdown/olmocr-sample.md` 也出自这里。

#### 4.3.4 代码实践

**实践：亲眼看见安装器生成的启动器（无需 GPU，约 3 分钟）**

1. **实践目标**：在磁盘上找到 `olmocr` 命令对应的启动器文件，确认它指向 `cli_main`。
2. **操作步骤**：

   ```bash
   conda activate olmocr
   which olmocr            # 例如 .../envs/olmocr/bin/olmocr
   cat "$(which olmocr)"   # 查看启动器内容
   ```

3. **需要观察的现象**：`which` 输出指向 conda 环境 `bin/` 下的 `olmocr` 文件；`cat` 出来是一小段 Python 代码，其中有 `from olmocr.pipeline import cli_main`（或等价的模块导入）与对它的调用。
4. **预期结果**：启动器内容与 [pyproject.toml:49-50](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/pyproject.toml#L49-L50) 的声明一一对应；这也解释了为什么换一个没装 olmocr 的环境，`olmocr` 命令就「消失」了。
5. 启动器具体写法随 setuptools 版本略有差异，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：如果项目把入口改成 `olmocr2 = "olmocr.pipeline:cli_main"` 并重新安装，旧命令还能用吗？

答案：能。`[project.scripts]` 的每一行都是独立生成一个启动器；只要 `olmocr` 那行没被删掉，旧命令仍在。改行只是新增/更名命令，不影响模块内部的 `cli_main`。

**练习 2**：为什么入口函数是同步的 `cli_main`，而不是直接把 `main` 注册为入口？

答案：控制台脚本的入口必须是普通同步函数（启动器直接 `sys.exit(func())`）。而 `main()` 是协程，直接调用只会返回一个 coroutine 对象而不执行。所以需要 `cli_main` 里的 `asyncio.run(main())` 完成从同步世界到异步世界的桥接。

**练习 3**：`python -m olmocr.pipeline ./localworkspace --pdfs a.pdf` 与 `olmocr ./localworkspace --pdfs a.pdf` 在解析参数时有区别吗？

答案：没有。两者最终都执行 `cli_main()` 中的同一个 `argparse` 解析器，参数语义完全一致；唯一可见差异是 `--help` 顶部显示的程序名（`pipeline.py`），因为 argparse 的 prog 默认来自 `sys.argv[0]` 的文件名。

## 5. 综合实践

**端到端任务：转换一个真实 PDF，并解释 workspace 里的每样东西**（对应本讲三个模块的综合）

1. **准备输入**（二选一）：
   - 下载官方样例：`curl -o olmocr-sample.pdf https://olmocr.allenai.org/papers/olmocr_3pg_sample.pdf`
   - 或直接用仓库自带测试文件：`tests/gnarly_pdfs/dolma-page-1.pdf`（1 页，转换最快）。

2. **运行转换**（需要 GPU 本地推理，或用 `--server` 指向远程端点）：
   ```bash
   # 本地 GPU 模式（首次会下载 ~30GB 模型，需 olmocr[gpu] 安装）
   olmocr ./localworkspace --markdown --pdfs olmocr-sample.pdf

   # 无 GPU 时改用远程服务器（轻量安装即可）
   olmocr ./localworkspace --server http://<服务器>:8000/v1 \
       --model allenai/olmOCR-2-7B-1025-FP8 --markdown --pdfs olmocr-sample.pdf
   ```

3. **盘点产物**：
   ```bash
   find ./localworkspace | sort
   cat localworkspace/markdown/olmocr-sample.md | head -40
   python - <<'EOF'
   import json, zstandard
   line = open(next(iter(__import__('glob').glob('localworkspace/results/output_*.jsonl')))).readline()
   doc = json.loads(line)
   print("字段:", sorted(doc.keys()))
   print("Source-File:", doc["metadata"]["Source-File"])
   print("页数:", doc["metadata"]["pdf-total-pages"])
   print("前 200 字:", doc["text"][:200])
   EOF
   ```

4. **验收清单（每项对照本讲对应小节）**：
   - [ ] `markdown/olmocr-sample.md` 存在且内容是干净的标题+正文（对应 4.2 的路径镜像规则）；
   - [ ] `results/output_{hash}.jsonl` 里每行一个 JSON，`Source-File` 就是你传的路径字符串；
   - [ ] `done_flags/` 里的 `.flag` 文件名哈希 = `output_` 后缀 = 索引 CSV 首列哈希，三者一致；
   - [ ] `worker_locks/` 为空（任务完成后锁被删除）；
   - [ ] 再次运行同一条命令，日志显示队列为空、几乎立刻退出（索引去重 + done flag 剔除）。

5. **预期结果**：全部勾选即证明你已把「命令 → 队列 → 推理 → 落盘 → 幂等重跑」这条链路走通。本讲环境无 GPU，端到端输出**待本地验证**；若暂无条件，请完成 4.2.4 的免 GPU 实践作为替代，并用 `olmocr ./localworkspace --stats` 观察统计旁路。

## 6. 本讲小结

- 一条 `olmocr ./localworkspace --markdown --pdfs x.pdf` 命令等价于一次 `asyncio.run(main())`：建队列 → 收集输入 → （本地）下载模型并启动 vLLM → 20 个 worker 协程并发消费 → 落盘 → 打印指标。
- workspace 是持久化的「账本」：`work_index_list.csv.zstd`（zstd 压缩 CSV 任务清单）、`done_flags/`（完成标记）、`worker_locks/`（领取锁）、`results/output_{hash}.jsonl`（Dolma 输出）、`markdown/`（`--markdown` 时的镜像目录）。
- Markdown 文件由 `get_markdown_path` 按源路径镜像生成，内容就是 Dolma 文档的 `text` 字段；不加 `--markdown` 也总有 Dolma JSONL。
- 组哈希是对组内路径排序后做 sha1，保证了输出文件名的确定性；索引按字符串去重，使重复运行天然幂等。
- `olmocr` 命令来自 `pyproject.toml` 的 `[project.scripts]` 注册，经 setuptools 生成启动器调用 `cli_main`；`python -m olmocr.pipeline` 是等价入口。
- `--stats` 分支在 GPU 检查之前返回，轻量安装也能用它查看 workspace 状态。

## 7. 下一步学习建议

下一讲（u1-l4「仓库结构与模块地图」）将把视野从 pipeline.py 扩大到整个 `olmocr/` 包：prompts、filter、data、train、bench、synth、viewer 各自负责什么、依赖方向如何，为后续单元建立代码地图。

如果你已具备 GPU 或远程服务器，也可以提前通读 `main()` 全文（[olmocr/pipeline.py:1199-1524](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1199-L1524)），标注出本讲流程图中每个节点对应的函数名——这正是单元 2 第 1 讲的实践任务。想深挖 workspace 机制的读者可以提前浏览 [olmocr/work_queue.py](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/work_queue.py) 中 `S3Backend` 的同名方法，体会「本地目录协议」如何原样映射到 S3 对象。
