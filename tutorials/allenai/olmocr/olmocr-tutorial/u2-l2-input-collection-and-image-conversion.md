# 输入收集：本地路径、S3 glob、tarball 与图片转 PDF

## 1. 本讲目标

上一讲我们俯瞰了 `main()` 的十个检查点，知道它「先搭台、再派活、最后收场」。本讲放大第一个真正的业务检查点——**输入收集**：`--pdfs` 参数里那些五花八门的路径，是如何被整理成一份干净的工作清单的。

学完本讲你应该能够：

1. 逐分支描述 `--pdfs` 支持的五种输入形态（S3 路径/glob、本地 PDF、本地图片、本地 tarball、txt 路径清单）以及它们的处理分支与错误分支。
2. 解释 PNG/JPEG 魔数（magic number）检测的原理，说明 olmOCR 为什么「不信后缀、信文件头」，以及一张图片是如何在流水线里被包成单页 PDF 的。
3. 掌握「采样估算平均页数 → 计算 `items_per_group`」的逻辑，理解为什么分组粒度要按**页数**而不是按**文档个数**来定。

## 2. 前置知识

### 2.1 魔数（magic number）

文件的后缀名是可以随便改的：把 `photo.png` 改名成 `photo.pdf`，文件内容一个字节都不会变。所以判断文件真实类型的可靠办法，是读文件开头的几个字节——几乎所有文件格式都在开头约定了一串「签名字节」，俗称魔数：

| 格式 | 开头字节（Python 字节串表示） | 长度 |
| --- | --- | --- |
| PNG | `b"\x89PNG\r\n\x1a\n"` | 8 字节 |
| JPEG | `b"\xff\xd8"`（SOI 段标记） | 2 字节 |
| PDF | `b"%PDF"` | 4 字节 |

本讲会看到 olmOCR 对同一路径做了**后缀 + 魔数**的双重校验：后缀决定「要不要看」，魔数决定「算不算数」。

### 2.2 glob 模式

glob 是 shell 风格的通配符匹配：`*` 匹配任意字符序列，`?` 匹配单个字符，`[abc]` 匹配方括号内任一字符。`s3://bucket/pdfs/*.pdf` 的含义就是「该 bucket 下 pdfs 前缀里所有以 `.pdf` 结尾的对象」。S3 本身不支持通配符查询，所以代码需要先算出通配符之前的最长固定前缀，用前缀列举对象，再在客户端逐个做 glob 匹配。

### 2.3 tarball 与 `tarball::内部路径` 记法

tarball 指打包压缩文件（这里是 `.tar.gz` / `.tgz`）。olmOCR 允许把成百上千个 PDF 打成一个 tarball 交给流水线，用它自己的记法 `tarball路径::内部成员路径`（注意是**双冒号**）来指称「某个 tarball 里的某个 PDF」，例如 `s3://bucket/batch1.tar.gz::docs/paper.pdf`。这样做的好处是：百万个小文件在 S3 上列举、请求的开销都很大，合并成大对象能显著降低请求成本。

### 2.4 采样估计与「按页分组」

设文档总数为 \( N \)，每篇文档的页数为 \( p_i \)。逐篇数页太贵（要把所有 PDF 都下载一遍），于是随机抽 \( n = \min(100, N) \) 篇，用样本均值

\[ \bar{p} = \frac{1}{n}\sum_{i=1}^{n} p_i \]

估计全体平均页数，再换算出每个工作项（work item group）应装多少篇文档。直觉是：**流水线真正的工作量以「页」为单位**（每页要渲染一次、推理一次），一个组装 5 篇 200 页的大书和装 5 篇 1 页的传单，工作量差了两个数量级。按页数定组大小，能让每个 work item 的工作量大致均匀，从而断点续跑的粒度均匀、重试的代价可控。

## 3. 本讲源码地图

| 文件 | 本讲涉及范围 | 作用 |
| --- | --- | --- |
| [olmocr/pipeline.py](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py) | L1334-L1412 输入收集主段；L479-L482 tarball 判定；L575-L599 图片转 PDF 的下游消费；L1205-L1210 参数定义 | 输入分类、采样估算、入队 |
| [olmocr/image_utils.py](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/image_utils.py) | 全文（仅 61 行） | 魔数探测与图片转 PDF |
| [olmocr/s3_utils.py](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/s3_utils.py) | L36-L63 glob 展开；L87-L98 本地/S3 统一取字节 | 输入收集的底层取数工具 |

辅助背景（本讲只引用不展开）：`olmocr/work_queue.py` 的 `populate_queue`（下一单元 u3-l1 精读）、`pyproject.toml` 的依赖清单（确认 `img2pdf` 并非 pip 依赖）。

## 4. 核心概念与源码讲解

### 4.1 输入分类分支：五种输入形态，两个归宿

#### 4.1.1 概念说明

`--pdfs` 是 `nargs="*"` 的可变参数，一次可以跟很多个值，每个值独立走一遍分类逻辑。分类的目标不是「给每个文件贴标签」，而是把所有输入**归并到两个集合**里：

- `pdf_work_paths`：单文档路径（本地路径或 `s3://` 路径，PDF 或图片混装）；
- `tarball_paths`：tarball 路径，每个 tarball 后续会被当成**一个**工作项整体处理。

为什么是这两个集合？因为下游的处理方式完全不同：普通 PDF 按采样算出的 `items_per_group` 分组入队；tarball 不拆分、一个 tarball 就是一个工作项（`populate_queue(tarball_paths, 1)`）。分组的语义（组哈希、done flag、断点续跑）在 u3-l1 会展开，这里只需记住「集合决定命运」。

#### 4.1.2 核心流程

对 `args.pdfs` 中的每个值，依次判断：

```text
对每个 pdf_path:
  ├─ 以 "s3://" 开头?
  │    └─ 是 → expand_s3_glob 展开成具体对象集合
  │             ├─ 是 tarball 后缀 → tarball_paths
  │             └─ 其他           → pdf_work_paths
  ├─ 本地存在?
  │    ├─ 是 tarball 后缀 → tarball_paths
  │    ├─ 后缀是 .pdf/.png/.jpg/.jpeg (不区分大小写):
  │    │    ├─ 开头 4 字节 == b"%PDF"      → pdf_work_paths (按 PDF 装载)
  │    │    ├─ is_png() 或 is_jpeg() 为真  → pdf_work_paths (按图片装载)
  │    │    └─ 否则 → 打 warning, 跳过该文件
  │    ├─ 后缀是 .txt → 逐行读路径, 每行再按 tarball 与否分流到两个集合
  │    └─ 其他后缀 → raise ValueError
  └─ 都不是 → raise ValueError("pdfs argument needs to be either a local path,
                                 an s3 path, or an s3 glob pattern...")
最终: pdf_work_paths 采样分组入队; tarball_paths 以 1 个/项入队
```

注意两个细节：

1. **txt 清单是「递归一层」的展开**：清单里每行可以是一个普通路径，也可以是一个 tarball，代码对每行再次调用 `is_tarball_path` 分流；
2. **S3 分支没有后缀白名单**：glob 展开出什么就收什么（只区分 tarball 与否），校验责任交给了 glob 模式本身；而本地分支有严格的后缀 + 魔数双重校验。

#### 4.1.3 源码精读

先看参数定义。[pipeline.py:L1205-L1210](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1205-L1210) 用 `nargs="*"` 声明 `--pdfs`，帮助文本明确列出三种典型用法（S3 glob、清单文件等）：

```python
parser.add_argument(
    "--pdfs",
    nargs="*",
    help="Path to add pdfs stored in s3 to the workspace, can be a glob path s3://bucket/prefix/*.pdf or path to file containing list of pdf paths",
    default=None,
)
```

主分类逻辑在 [pipeline.py:L1334-L1373](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1334-L1373)。这段代码先初始化两个集合，然后进入 for 循环：

- [pipeline.py:L1341-L1345](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1341-L1345)：S3 分支。`expand_s3_glob(pdf_s3, pdf_path)` 把 glob 展开为 `{'s3://bucket/key': etag}` 字典，随后用两个推导式按 `is_tarball_path` 分流到两个集合。
- [pipeline.py:L1346-L1363](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1346-L1363)：本地分支。先判 tarball，再用 `lower().endswith(...)` 判四种后缀；关键一行是 `open(pdf_path, "rb").read(4) == b"%PDF"`——**直接读 4 个字节和 PDF 魔数比对**。后缀对但魔数不对时，再退而求其次问 `is_png`/`is_jpeg`（处理「实际是图片却顶着 .pdf 后缀」的文件）；两者都不是只打 warning 跳过，不中断整个任务。
- [pipeline.py:L1364-L1369](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1364-L1369)：txt 清单分支。逐行 `strip()` 并丢弃空行，每行同样按 tarball 与否分流。
- [pipeline.py:L1370-L1373](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1370-L1373)：两个错误分支——本地存在但后缀不支持 → `ValueError`；本地不存在（且不是 s3://）→ `ValueError`。

tarball 的判定标准非常朴素，见 [pipeline.py:L479-L482](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L479-L482)：只看小写后缀是不是 `.tar.gz` 或 `.tgz`（这里没有魔数校验）：

```python
def is_tarball_path(path: str) -> bool:
    """Check if a path is a tarball based on extension."""
    lower = path.lower()
    return lower.endswith(".tar.gz") or lower.endswith(".tgz")
```

S3 glob 的展开实现在 [s3_utils.py:L36-L63](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/s3_utils.py#L36-L63)：找到第一个通配符的位置，取其前的最长固定前缀，用 `list_objects_v2` 分页列举，再对每个 key 做 `glob.fnmatch.fnmatch` 匹配，命中者连同 ETag 一起返回。注释里还特意说明 `?` 不被支持，因为它与 URL 的查询参数语义冲突。

分类完成后，[pipeline.py:L1375](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1375) 打出那条你在第 1 单元可能已经见过的日志：`Found {N:,} regular pdf paths and {M:,} tarballs to add`。此外要注意：两个集合都是 `set`，**重复传入同一路径会自动去重**——这是后续「同一 workspace 反复补料不产生重复工作项」的第一道保险。

#### 4.1.4 代码实践

**实践目标**：不动用任何 GPU，亲眼看一遍「分类 → 采样 → 入队」的日志，验证 `--stats` 短路（u2-l1 检查点 ③）发生在输入收集**之后**。

**操作步骤**（待本地验证，需要按 u1-l2 装好 olmocr 与 poppler）：

1. 在仓库根目录准备三个文件：从 `tests/gnarly_pdfs/` 复制两个 PDF（如 `ambiguous.pdf`、`dolma-page-1.pdf`）到 `/tmp/input_demo/`，再手写一个 `list.txt`：

   ```text
   /tmp/input_demo/ambiguous.pdf
   /tmp/input_demo/dolma-page-1.pdf
   ```

2. 故意制造一个错误分支：把一行不存在的路径 `/tmp/input_demo/ghost.pdf` 追加到 `list.txt` 末尾，观察报错；随后删掉这行。

3. 运行干跑命令（`--stats` 使流程在入队后立即返回）：

   ```bash
   olmocr /tmp/ws_demo --stats --pdfs /tmp/input_demo/list.txt
   ```

**需要观察的现象**：日志依次出现 `Got --pdfs argument...`、`Loading file at ... as list of paths`、`Found 2 regular pdf paths and 0 tarballs to add`、`Sampling PDFs to calculate optimal length` 进度条、`Calculated items_per_group: ... based on average pages per PDF: ...`，最后 `--stats` 打印 workspace 统计后退出（全程无 GPU 参与）。

**预期结果**：`/tmp/ws_demo` 下生成 `work_index_list.csv.zstd` 索引（u1-l3 讲过其结构）；若附录错误路径则抛出 `ValueError`——但注意该错误发生在 `os.path.exists` 分支而非清单读取分支：清单里的行在分流时**不校验存在性**，校验会推迟到 worker 真正取文件时。

#### 4.1.5 小练习与答案

**练习 1**：同一目录下有 `a.pdf`（真实 PDF）、`b.pdf`（其实是张 PNG）、`c.exe`（存在）。把目录清单传给 `--pdfs`，三者各自命运如何？

**答案**：`a.pdf` 读前 4 字节得 `%PDF` → 进入 `pdf_work_paths`；`b.pdf` 魔数不是 `%PDF` 但 `is_png` 为真 → 同样进入 `pdf_work_paths`（日志显示 `as image document`）；`c.exe` 后缀不在白名单 → `raise ValueError`，整个命令终止。

**练习 2**：为什么 S3 分支不像本地分支那样做后缀与魔数校验？

**答案**：S3 分支的输入是 glob 模式，筛选已经由模式本身（如 `*.pdf`）完成；对百万级对象逐一发起请求读魔数代价太高。错误对象的兜底发生在 worker 阶段——`process_pdf` 取到字节后还会再做一次 `is_png`/`is_jpeg` 检查（见 4.2.3），失败页最终走 fallback 机制（u2-l6）。

**练习 3**：`pdf_work_paths` 和 `tarball_paths` 为什么用 `set` 而不用 `list`？

**答案**：一是去重——同一路径重复出现在多个 `--pdfs` 值或清单行中时只保留一份，避免生成重复工作项；二是配合后续 `populate_queue` 的分组哈希计算，集合语义让「补料」操作幂等。（代价是丢失了输入顺序，但分组前本来就要做排序哈希，顺序无意义。）

### 4.2 图片格式探测与图片转 PDF

#### 4.2.1 概念说明

olmOCR 的推理单元是「PDF 的一页」——渲染成图片交给视觉语言模型（u2-l3 会精读渲染）。既然模型看到的本来就是图片，那直接吃 PNG/JPEG 岂不更直接？流水线仍选择**先把图片包成单页 PDF**，理由是统一内部表示：下游所有环节（按页处理、页码元数据、Dolma 的 `pdf_page_spans`、断点重试）都建立在「文档 = 若干页」的抽象上。把图片包装成 1 页 PDF，就能让图片输入免费复用整套 PDF 机制，而不是在几十处代码里写 `if 图片` 分支。

负责这件事的是 `olmocr/image_utils.py`，三个函数各司其职：`is_png`/`is_jpeg` 探测格式，`convert_image_to_pdf_bytes` 完成转换。

#### 4.2.2 核心流程

转换本身不重写 PDF 编码，而是调用外部命令行工具 `img2pdf`（「无损嵌入」：图片字节被直接放进 PDF 容器，不二次压缩）：

```text
convert_image_to_pdf_bytes(路径或路径列表)
  ├─ 输入归一化: str → [str]; 空列表 → ValueError
  ├─ 逐个检查文件存在, 不存在 → ValueError
  ├─ subprocess.run(["img2pdf", 文件...], capture_output=True)
  │    ├─ 成功 → 返回 stdout (即 PDF 字节)
  │    └─ CalledProcessError → 抛 RuntimeError(携带 stderr)
```

在流水线中的消费时机（不是收集时立刻转，而是**worker 取到文件时**才转）：

```text
process_pdf(pdf_orig_path)
  ├─ get_s3_bytes_with_backoff 下载字节到临时文件 (本地路径则直接读盘)
  ├─ is_png(tf.name) 或 is_jpeg(tf.name)?
  │    └─ 是 → tf.seek(0); 重写为 convert_image_to_pdf_bytes(tf.name) 的输出
  └─ process_single_pdf(...)  # 此时临时文件一定是合法 PDF
```

#### 4.2.3 源码精读

魔数探测在 [image_utils.py:L44-L61](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/image_utils.py#L44-L61)，两个函数都是「只读开头几个字节、逐字节比对签名」：

```python
def is_png(file_path):
    try:
        with open(file_path, "rb") as f:
            header = f.read(8)
            return header == b"\x89PNG\r\n\x1a\n"
    except Exception as e:
        print(f"Error: {e}")
        return False

def is_jpeg(file_path):
    try:
        with open(file_path, "rb") as f:
            header = f.read(2)
            return header == b"\xff\xd8"
    ...
```

注意两点：以二进制模式 `"rb"` 打开；任何 I/O 异常都吞掉并返回 `False`（探测失败按「不是该格式」处理，绝不让探测本身炸掉流水线）。

转换函数在 [image_utils.py:L6-L41](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/image_utils.py#L6-L41)，核心一行是 [image_utils.py:L34](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/image_utils.py#L34) 的 `subprocess.run(["img2pdf"] + image_files, check=True, capture_output=True)`——PDF 数据从 `result.stdout` 拿回。这里有一个**源码阅读才能发现的坑**：查 [pyproject.toml:L22-L40](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/pyproject.toml#L22-L40) 的依赖清单，`img2pdf` **不在**其中——它和 poppler 一样是**外部系统命令**，轻量 `pip install olmocr` 不会带来它。若缺失，图片输入会在 worker 转换时抛 `RuntimeError`。

下游消费点在 [pipeline.py:L589-L593](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L589-L593)（`process_pdf` 内）：下载完成后**对临时文件再做一次** `is_png`/`is_jpeg` 判断，命中则 `seek(0)` 覆写为 PDF 字节：

```python
if is_png(tf.name) or is_jpeg(tf.name):
    logger.info(f"Converting {pdf_orig_path} from image to PDF format...")
    tf.seek(0)
    tf.write(convert_image_to_pdf_bytes(tf.name))
    tf.flush()
```

为什么收集时（L1359）验过一次、这里（L589）又验一次？因为两次面对的对象不同：前者验的是**用户给的本地路径**；后者验的是**下载到本地的字节**（可能是 S3 对象、可能是别的机器写入的清单行），输入来源不同，校验就必须各自把关。这是大规模系统里典型的「边界处自证」思想。

此外，采样循环里也复用了同一对探测器：[pipeline.py:L1390-L1391](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1390-L1391) 把图片直接记为 1 页——一张图片就是一页文档，参与平均页数的计算。

#### 4.2.4 代码实践

**实践目标**：亲手完成「探测 → 转换 → 验证」闭环，理解图片如何变成单页 PDF。

**操作步骤**（待本地验证）：

1. 确认 `img2pdf` 命令可用（`img2pdf --help`）；若没有，先 `pip install img2pdf`（该包自带同名命令行工具）。
2. 在**仓库根目录之外**（如 `/tmp/img_demo/`）新建 `convert_demo.py`（示例代码）：

   ```python
   from PIL import Image  # Pillow 是 olmocr 的 pip 依赖, 无需额外安装

   from olmocr.image_utils import convert_image_to_pdf_bytes, is_jpeg, is_png
   from pypdf import PdfReader

   # 1. 用 Pillow 画一张 64x64 的纯色测试图
   img_path = "/tmp/img_demo/test.png"
   Image.new("RGB", (64, 64), color=(30, 144, 255)).save(img_path)

   # 2. 魔数探测: PNG 应为 True, JPEG 应为 False
   print("is_png:", is_png(img_path))    # 预期 True
   print("is_jpeg:", is_jpeg(img_path))  # 预期 False

   # 3. 转成 PDF 字节并落盘
   pdf_bytes = convert_image_to_pdf_bytes(img_path)
   pdf_path = "/tmp/img_demo/test_as_pdf.pdf"
   with open(pdf_path, "wb") as f:
       f.write(pdf_bytes)

   # 4. 用 pypdf 验证页数, 并顺手看魔数探测对新 PDF 的判断
   reader = PdfReader(pdf_path)
   print("num_pages:", len(reader.pages))  # 预期 1
   with open(pdf_path, "rb") as f:
       print("leading bytes:", f.read(4))   # 预期 b'%PDF'
   ```

3. 运行：`python /tmp/img_demo/convert_demo.py`。

**需要观察的现象**：`is_png: True`、`is_jpeg: False`、`num_pages: 1`、`leading bytes: b'%PDF'`；转换前后目录里多出的 `test_as_pdf.pdf` 体积与原 PNG 大致同量级（无损嵌入，无二次压缩）。

**预期结果**：如上四行输出全部符合预期，即证明「图片 → 单页 PDF」链路可用。若第 3 步抛 `RuntimeError: Error converting image(s) to PDF:`，说明 `img2pdf` 命令缺失，回到步骤 1。

#### 4.2.5 小练习与答案

**练习 1**：`is_png` 只读 8 个字节就返回。一个 2GB 的 PNG 文件，探测开销是多少？为什么这是可接受的设计？

**答案**：开销是常数——一次 `open` 加一次 8 字节 `read`，与文件大小无关。魔数探测在生产中会被调用在「每个下载完的文件」上（采样循环、`process_pdf`），必须做到近似零成本。

**练习 2**：把一张 JPEG 改名为 `x.pdf` 传给 `--pdfs`，能被接受吗？最终会被当成什么处理？

**答案**：能。本地分支先因 `.pdf` 后缀进入白名单，`read(4) != b"%PDF"` 排除 PDF 后，`is_jpeg` 命中，以 `as image document` 加入 `pdf_work_paths`；worker 下载后再次 `is_jpeg` 命中，被包成单页 PDF 参与正常流水线。

**练习 3**：`convert_image_to_pdf_bytes` 支持传入**列表**（多图合成一个多页 PDF），但流水线里只传了单文件路径。为什么不在这里把同目录的多张图合并？

**答案**：合并意味着发明一种「哪些图属于同一篇文档」的分组规则（按目录名？按文件名前缀？），任何约定都会猜错一部分；而「一图一文档」是唯一无歧义的语义。列表能力留给调用方（包括未来扩展）自行决定分组，库函数不替业务做主。

### 4.3 pages_per_group 采样估算：按页定组

#### 4.3.1 概念说明

回忆 u1-l5：`--pages_per_group` 用 `argparse.SUPPRESS` 声明、无静态默认值，在 [pipeline.py:L1314-L1315](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1314-L1315) 按运行形态补默认：走外部计费 API（给了 `--api_key`）时目标是每组约 **50** 页，自有 GPU 时 **500** 页。本模块讲这个「目标页数」如何变成「每组文档数」。

为什么按页分组（2.4 节的直觉再推一步）：一个 work item group 是断点续跑与完成标记的最小单元（u3-l1），组的工作量 = 组内总页数。若按固定文档数分组，遇到「1000 页的大书 + 1 页的收据」混合语料，组工作量会相差三个数量级：大组失败重试代价高、done flag 粒度太粗；小组则索引条目爆炸。控制「每组页数恒定」→ 每组重试代价上限恒定、进度统计均匀。

#### 4.3.2 核心流程

```text
若 pdf_work_paths 非空:
  n = min(100, 文档总数)
  sampled = random.sample(pdf_work_paths, n)        # 均匀随机抽样
  for pdf in sampled:
    字节 = get_s3_bytes(pdf_s3, pdf)                 # 本地路径直接读盘, s3 路径走网络
    写入临时文件
    if is_png 或 is_jpeg: 页数 = 1
    else:               页数 = len(PdfReader(临时文件).pages)
    读取失败 → warning 并跳过 (不中断)
  平均页数 p̄ = sum(页数) / len(页数)   # 全部失败 → 兜底 p̄ = 10
  items_per_group = max(1, int(pages_per_group / p̄))
  populate_queue(pdf_work_paths, items_per_group)
```

换算公式（`int()` 即向零取整，也就是正数域的下取整 \(\lfloor\cdot\rfloor\)）：

\[ \text{items\_per\_group} = \max\left(1,\ \left\lfloor \frac{\text{pages\_per\_group}}{\bar{p}} \right\rfloor\right) \]

几个边界：样本上限 100（再多的抽样收益递减，而每个样本都要下载一次文件）；单次读取失败只 warning 不中断（脏数据在大规模语料中是常态）；`max(1, ...)` 保证平均页数超过目标页数（比如全是 600 页的大书、目标 500 页）时，每组至少仍装 1 篇——宁可组偏大，也不能出现空组。

#### 4.3.3 源码精读

整段逻辑在 [pipeline.py:L1377-L1408](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1377-L1408)。

采样与页数统计：[pipeline.py:L1380-L1396](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1380-L1396) 中，`random.sample(list(pdf_work_paths), sample_size)` 从集合均匀抽 100 个；每个样本经 `get_s3_bytes` 取字节写入 `tempfile.NamedTemporaryFile(suffix=".pdf")`。这里能看到一个精巧的**本地/云统一**设计：`get_s3_bytes`（[s3_utils.py:L87-L98](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/s3_utils.py#L87-L98)）发现路径不带 `s3://`/`gs://`/`weka://` 前缀时直接退化为本地 `open().read()`——于是「本地文件」和「S3 对象」在采样循环里走同一段代码，无需 if/else 两套逻辑。页数判定则先问 `is_png`/`is_jpeg`（图片 = 1 页），否则 `PdfReader` 数页；`except Exception` 兜住一切读取失败。

```python
sample_size = min(100, len(pdf_work_paths))
sampled_pdfs = random.sample(list(pdf_work_paths), sample_size)
...
if is_png(tmp_file.name) or is_jpeg(tmp_file.name):
    page_counts.append(1)
else:
    reader = PdfReader(tmp_file.name)
    page_counts.append(len(reader.pages))
```

均值、兜底与换算：[pipeline.py:L1398-L1405](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1398-L1405)：

```python
if page_counts:
    avg_pages_per_pdf = sum(page_counts) / len(page_counts)
else:
    logger.warning("Could not read any PDFs to estimate average page count.")
    avg_pages_per_pdf = 10  # Default to 10 pages per PDF if sampling fails

items_per_group = max(1, int(args.pages_per_group / avg_pages_per_pdf))
```

全部样本读不出来时兜底 \(\bar{p}=10\)——一个「typical 文档十页上下」的经验值，保证流程能继续。最后 [pipeline.py:L1408](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1408) 把结果交给 `populate_queue`；而 tarball 走的是 [pipeline.py:L1411-L1412](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1411-L1412) 的独立通道——`populate_queue(tarball_paths, 1)`，**一个 tarball 就是一个工作项**，不参与页数估算（tarball 内部 PDF 由 `process_tarball` 在 worker 阶段整体解开，见 [pipeline.py:L485-L503](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L485-L503)，成员以 `tarball::成员名` 记法登记）。

代一个小数：若采样得 \(\bar{p}=8.2\)、自有 GPU 默认目标 500 页，则 \(\lfloor 500/8.2 \rfloor = 60\)，每组约 60 篇文档；若走 API 默认 50 页，则 \(\lfloor 50/8.2 \rfloor = 6\)。同一个语料，两种运行形态的组粒度差 10 倍——这就是 u1-l5 说「走计费 API 要调小闸门」在分组侧的体现。

#### 4.3.4 代码实践

**实践目标**：复刻采样估算，用仓库自带的「困难 PDF」当语料，感受 \(\bar{p}\) 对 `items_per_group` 的影响。

**操作步骤**（待本地验证；只需 `pypdf`，无需 img2pdf/GPU）：

1. 在仓库根目录之外新建 `estimate_demo.py`（示例代码）：

   ```python
   import glob
   import math
   import random

   from pypdf import PdfReader

   files = glob.glob("<仓库路径>/tests/gnarly_pdfs/*.pdf")
   n = min(100, len(files))
   sampled = random.sample(files, n)

   page_counts = []
   for f in sampled:
       try:
           page_counts.append(len(PdfReader(f).pages))
       except Exception as e:
           print(f"skip {f}: {e}")

   avg = sum(page_counts) / len(page_counts)
   for target in (500, 50):
       ipg = max(1, int(target / avg))
       print(f"avg_pages={avg:.2f}  target={target}  items_per_group={ipg}")
   ```

2. 把 `<仓库路径>` 替换为本机仓库位置，运行 `python estimate_demo.py`。

**需要观察的现象**：打印出的 `avg_pages`（gnarly_pdfs 是人工挑选的疑难样本，页数分布可能与真实语料不同）；同一均值下目标 500 与 50 得到的 `items_per_group` 成约 10 倍关系。

**预期结果**：`avg_pages` 为某个正数（具体值待本地验证，不同 PDF 集合结果不同）；两组 `items_per_group` 均为不小于 1 的整数。可再删掉 `random.sample` 改为全量统计，对比样本均值与真实均值的偏差，体会 100 上限的抽样误差有多大。

#### 4.3.5 小练习与答案

**练习 1**：采样循环对每个样本都要 `get_s3_bytes` 完整下载一次文件。`pypdf` 数页数其实只需要 PDF 的交叉引用表（文件尾部）。查看 [s3_utils.py:L87-L105](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/s3_utils.py#L87-L105) 的函数签名，说明代码已为这种优化预留了什么能力、为什么这里没用。

**答案**：`get_s3_bytes` 带有 `start_index`/`end_index` 参数，可构造 HTTP Range 请求只取字节区间（本地文件则会断言不支持区间），这就是「只下载文件尾部再数页」的底层能力。但采样循环传的是整文件：因为同一份字节马上还要给 `is_png`/`is_jpeg`（需要头部）与 `PdfReader`（需要尾部），且 pypdf 对「只有尾部的字节流」并不能直接打开。100 个样本的全量下载是一次性成本，优化收益有限——工程上选择了简单。

**练习 2**：语料全是 1 页的图片（PNG）时，公式会算出什么？合理吗？

**答案**：采样中图片一律记 1 页，\(\bar{p}=1\)，于是 `items_per_group = max(1, int(target/1)) = target`（500 或 50），即每组 500/50 张图片。合理——每张图就是一页，按页定组退化为按张定组，公式无需特判图片形态，这正是 4.2「统一内部表示」带来的好处。

**练习 3**：为什么 tarball 不参与采样估算，而是硬编码 `items_per_group=1`？

**答案**：tarball 是不可分割的最小交付单元：worker 拿到它才解开（`process_tarball`），入队阶段并不知道里面有多少个 PDF、多少页；且拆开 tarball 再逐 PDF 入队会破坏「降低 S3 请求次数」的初衷。一个 tarball 一个工作项，粒度虽粗但语义清晰。

## 5. 综合实践

**任务**：为一个「混合语料」模拟完整的输入收集决策，不依赖 GPU 与 S3。

1. **准备语料**：建目录 `/tmp/mixed_demo/input/`，放入 (a) 两个真实 PDF（从 `tests/gnarly_pdfs/` 复制）；(b) 一张 PNG（用 4.2.4 实践的脚本生成）；(c) 一个 tarball：`tar czf bundle.tgz ambiguous.pdf dolma-page-1.pdf`（在临时目录中打包两个 PDF）；(d) 清单 `list.txt`，内容为上述 (a)(b) 各文件的绝对路径加一行 tarball 的绝对路径。
2. **预测**：先笔算——假设采样得 \(\bar{p}=1.5\)（两张 PDF 共若干页 + 一张图片 1 页），自有 GPU 默认目标 500 时 `items_per_group` 是多少？tarball 会有几个工作项？
3. **干跑验证**：`olmocr /tmp/mixed_demo/ws --stats --pdfs /tmp/mixed_demo/input/list.txt`，对照日志核对：`Found 3 regular pdf paths and 1 tarballs to add`、`Calculated items_per_group: ...` 与你的笔算是否一致（注意日志里 `regular pdf paths` 计数包含那张 PNG）。
4. **核对索引**：解压 `work_index_list.csv.zstd`（`zstd -d` 或 Python `zstandard`），确认普通 PDF 与图片被分进了按 `items_per_group` 切出的组，而 tarball 独占一组（组的哈希规则在 u3-l1 精读，这里只看分组数量）。

**预期结果**：日志计数与你准备的文件数一致；索引中能看到图片路径与 tarball 路径并存。若想再进一步，可把 `--pages_per_group 20` 显式传入，观察 `items_per_group` 随目标页数线性变化。（步骤 3、4 待本地验证。）

## 6. 本讲小结

- `--pdfs` 的五种输入形态（S3 路径/glob、本地 PDF、本地图片、本地 tarball、txt 清单）经分类后**殊途同归**到两个集合：单文档路径集 `pdf_work_paths` 与 tarball 集 `tarball_paths`，集合用 `set` 天然去重。
- 本地文件走「后缀白名单 + 魔数」双重校验（`%PDF` 4 字节、PNG 8 字节、JPEG 2 字节）；S3 glob 展开只按后缀分 tarball，完整性校验推迟到 worker 取到字节之后。
- 图片输入不在收集时转换，而在 `process_pdf` 下载后经 `is_png`/`is_jpeg` 二次探测、由 `convert_image_to_pdf_bytes`（外部命令 `img2pdf`，**不在 pip 依赖中**）包成单页 PDF，从而免费复用整套「按页处理」机制。
- 分组粒度按**页**不按**篇**：随机抽 \(\min(100, N)\) 篇估出平均页数 \(\bar{p}\)，\(\text{items\_per\_group} = \max(1, \lfloor \text{pages\_per\_group}/\bar{p} \rfloor)\)；全部采样失败兜底 \(\bar{p}=10\)。
- tarball 不参与估算，一个 tarball 硬编码为一个工作项，内部成员以 `tarball::成员名` 记法登记。
- `get_s3_bytes` 对非云前缀路径退化为本地读盘，使采样循环用同一套代码兼容本地与 S3 输入。

## 7. 下一步学习建议

输入已入队，下一讲 **u2-l3《PDF 渲染：从页面坐标到 base64 PNG》**沿着调用链前进：`process_single_pdf` 拿到 PDF 后如何用 `pdfinfo` 的 MediaBox 换算渲染分辨率、用 `pdftoppm` 把每一页变成模型真正看到的图片。若你对「队列」本身更感兴趣，可以先跳到 **u3-l1《WorkQueue 抽象》**看 `populate_queue` 的分组哈希与 `initialize_queue` 的洗牌策略，再回到 u2-l3。建议顺手阅读 [s3_utils.py](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/s3_utils.py) 的 `parse_s3_path` 与 `get_s3_bytes_with_backoff`，为 u4-l3 的 S3 工具函数精读埋个伏笔。
