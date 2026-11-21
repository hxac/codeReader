# u2-l3 PDF 渲染：从页面坐标到 base64 PNG

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 PDF 的「点」（point）坐标系与 MediaBox 的含义，并会用 `pdfinfo` 读出任意页面的物理尺寸。
2. 推导 `render_pdf_to_base64png` 中 `target_longest_image_dim * 72 / longest_dim` 这个分辨率换算公式，理解「72 像素/点」这一 PDF 坐标约定的来历。
3. 独立完成一个渲染实验：对同一个 PDF 页面分别以 644 和 1288 的目标最长边渲染，验证输出 PNG 的最长边确实等于设定值。
4. 读懂 `get_png_dimensions_from_base64` 的技巧——不解码整张图片、只切 base64 字符串的前 32 个字符就能读出宽高。

本讲在整条流水线中的位置：上一讲（u2-l2）讲的是「输入怎么收进来、怎么分组入队」；worker 从队列取到 PDF 之后、把它发给模型之前，必须先把某一**页**变成一张**图片**——这件事就发生在本讲的 `renderpdf.py` 里。可以说，这是 VLM OCR 的物理起点：**模型看到的从来不是 PDF，而是这一步渲染出来的 PNG**。

## 2. 前置知识

### 2.1 PDF 的坐标系：点（point）与 MediaBox

PDF 是矢量格式，页面本身没有「像素」概念，只有**点**（point/pt）：

- 1 点 = 1/72 英寸。这是 1980 年代印刷业沿用下来的约定，也是本讲那个 `72` 的唯一出处。
- 每一页用一个 **MediaBox** 矩形描述大小，形式是四个坐标 \([x_0,\ y_0,\ x_1,\ y_1]\)，即左下角 \((x_0, y_0)\) 与右上角 \((x_1, y_1)\)。
- 最常见的美国信纸（Letter）页面是 8.5×11 英寸，换算成点就是 \(8.5 \times 72 = 612\)、\(11 \times 72 = 792\)，所以你会到处看到 `612 x 792` 这个数字（本仓库 `tests/gnarly_pdfs/edgar.pdf` 第一页就是它，`tests/test_anchor.py` 的断言里印着 `Page dimensions: 612.0x792.0`）。
- 注意 MediaBox 的原点**不必**是 (0, 0)——坐标可以整体平移，理论上甚至可以方向相反。这就是为什么源码里算宽高要取差值的绝对值。

### 2.2 位图与 DPI：像素是怎么来的

把矢量页面变成位图（光栅化）时，需要一个**分辨率**（DPI，每英寸像素数）。换算关系是：

\[ \text{像素数} = \text{点数} \times \frac{\text{dpi}}{72} \]

例如 612 点宽的页面在 150 DPI 下渲染，宽度就是 \(612 \times 150 / 72 = 1275\) 像素。**同一个 PDF，DPI 不同，出来的图大小完全不同**——这是本讲要精读的核心机制。

### 2.3 base64 与 data URL

`base64` 把任意二进制编码成纯 ASCII 文本（3 字节 → 4 字符）。之所以必须编码，是因为渲染出的 PNG 要塞进发给推理服务的 **JSON 请求体**里，形如 `data:image/png;base64,xxxx...`，JSON 不能直接装原始二进制。

### 2.4 PNG 文件的开头长什么样

PNG 文件有固定的头部结构，本讲第三个模块就建立在这上面：

| 偏移（字节） | 长度 | 内容 |
|---|---|---|
| 0–7 | 8 | 文件签名 `\x89PNG\r\n\x1a\n` |
| 8–11 | 4 | 第一个 chunk 的长度（IHDR 恒为 13） |
| 12–15 | 4 | chunk 类型，恒为 ASCII `IHDR` |
| **16–19** | 4 | **宽度**（大端整数） |
| **20–23** | 4 | **高度**（大端整数） |
| 24 起 | … | 位深、颜色类型等，本讲用不到 |

也就是说：**想知道一张 PNG 的尺寸，只需要文件的第 16~24 字节**。一张 1288 像素页面的 PNG 可能有几百 KB，但读尺寸只需要 9 个字节——`get_png_dimensions_from_base64` 的全部聪明之处就在于此。

### 2.5 你需要的环境

本讲的实践只需要第 1 单元装好的**轻量环境**：`pip install olmocr` + 系统级的 poppler（`pdfinfo`、`pdftoppm` 两个命令）+ Pillow。不需要 GPU，不需要模型。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [olmocr/data/renderpdf.py](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/data/renderpdf.py#L1-L130) | 本讲主角，仅 130 行：三个公开函数覆盖「量尺寸 → 渲染 → 快速读回尺寸」全流程 |
| [olmocr/pipeline.py](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L36-L36) | 消费方：`build_page_query` 调用渲染函数并把结果包成模型请求 |
| [tests/test_anchor.py](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/tests/test_anchor.py#L269-L280) | 校验测试：`pdfinfo` 读出的 MediaBox 与 pypdf 逐页对账 |
| tests/gnarly_pdfs/*.pdf | 实验材料：30 多个形态各异的「刁钻」PDF |

`renderpdf.py` 是全仓库被引用最广的模块之一（u1-l4 的模块地图里提过它是共享底座）：推理流水线（pipeline.py）、训练数据加载（train/dataloader.py）、GRPO 奖励渲染（train/grpo_train.py）、bench 全部 runner（bench/runners/*）、结果查看器（viewer/dolmaviewer.py）都调用它。**读懂这 130 行，等于摸到了大半个仓库共用的地基。**

## 4. 核心概念与源码讲解

### 4.1 量尺寸：`get_pdf_media_box_width_height`

#### 4.1.1 概念说明

要把第 N 页渲染成「最长边恰好等于 D 像素」的图，必须先知道这一页**在点坐标下**有多大。这个函数做的就是这件事：调用 poppler 自带的命令行工具 `pdfinfo`，从它的文本输出里抠出 MediaBox，返回 `(宽, 高)`，单位是点。

为什么不直接用 Python 库（pypdf）读？对比一下就明白：`pdfinfo` 是 C 写的原生工具，只解析文档结构不渲染内容，启动开销极小；而每个 worker 每页都要调一次这个函数，快就是硬道理。仓库还专门写了测试保证它和 pypdf 读出的结果一致（见 4.1.3）。

#### 4.1.2 核心流程

```text
get_pdf_media_box_width_height(pdf 路径, 页码)
  ├─ 1. 拼命令：pdfinfo -f 页码 -l 页码 -box -enc UTF-8 <pdf>
  ├─ 2. subprocess.run 执行，拿到文本输出
  ├─ 3. 逐行找包含 "MediaBox" 的第一行
  ├─ 4. 按 ":" 切一刀取右半边 → 按空白切出 4 个数字字符串
  └─ 5. 返回 (|x0 - x1|, |y0 - y1|)  ← 宽、高，单位：点
```

`pdfinfo` 各参数含义：

- `-f` / `-l`：first / last，只输出第几页的信息（避免大 PDF 全量打印）；
- `-box`：额外输出每页的 MediaBox / CropBox 等几何框（默认不输出）；
- `-enc UTF-8`：输出编码。

#### 4.1.3 源码精读

函数定义与命令拼装——注意 `-box` 是能拿到 MediaBox 行的关键开关：

[olmocr/data/renderpdf.py:9-21](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/data/renderpdf.py#L9-L21)
这段代码定义函数签名 `(local_pdf_path, page_num) -> tuple[float, float]`，拼出 `pdfinfo -f N -l N -box -enc UTF-8 <path>` 命令并用 `subprocess.run` 同步执行，捕获 stdout/stderr 文本。

解析循环与返回值：

[olmocr/data/renderpdf.py:30-36](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/data/renderpdf.py#L30-L36)
这段代码遍历 `pdfinfo` 输出的每一行，找到第一处含 `MediaBox` 的行（形如 `Page    1 MediaBox:     0.00     0.00   612.00   792.00`），按 `:` 切开后取出 4 个浮点数，返回 `(abs(x0 - x1), abs(y3 - y1))`。**取绝对值**是因为 MediaBox 原点可以不在 (0,0)、坐标甚至可能反向，直接取右上角坐标会得到错误的宽高。找不到该行则抛 `ValueError`。

两个值得注意的细节：

- **页码从 1 开始**。bench 的多个 miner 脚本在调用时都有注释 `# render_pdf_to_base64png is 1-indexed`（如 [olmocr/bench/miners/mine_headers_footers.py:122](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/bench/miners/mine_headers_footers.py#L122)），它们内部用 0 起点的页码，调用时要 `+1`。
- **解析以「第一个含 MediaBox 的行」为准**。`-box` 同时还会输出 CropBox 行，但因为只匹配 `MediaBox` 字样，CropBox 会被自然跳过。

正确性有测试兜底——仓库用 pypdf 对全部 gnarly PDF 逐页对账：

[tests/test_anchor.py:269-280](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/tests/test_anchor.py#L269-L280)
这段代码遍历 `tests/gnarly_pdfs/` 下所有 PDF 的所有页，断言 `get_pdf_media_box_width_height` 的返回值与 pypdf 的 `page.mediabox.width/height` 在千分位精度内一致——用「两个独立实现读出同一个数」来保证解析逻辑没有理解偏差。

#### 4.1.4 代码实践

1. **实践目标**：亲眼看到 `pdfinfo` 的原始输出，理解源码解析的就是这份文本。
2. **操作步骤**：在仓库根目录直接运行（无需 Python）：

   ```bash
   pdfinfo -f 1 -l 1 -box -enc UTF-8 tests/gnarly_pdfs/edgar.pdf
   ```

   再换一个尺寸「不寻常」的页面对比：

   ```bash
   pdfinfo -f 1 -l 1 -box -enc UTF-8 tests/gnarly_pdfs/skinnypage.pdf
   pdfinfo -f 1 -l 1 -box -enc UTF-8 tests/gnarly_pdfs/small_page_size.pdf
   ```

3. **需要观察的现象**：`edgar.pdf` 应输出形如 `Page    1 MediaBox:     0.00     0.00   612.00   792.00` 的行（精确的小数格式以本地输出为准）；`skinnypage.pdf` 的宽高比会明显偏离正常纸张。
4. **预期结果**：你看到的 MediaBox 行正是 `renderpdf.py:30-36` 解析的对象——把那行文本 mentally 代入 `line.split(":")[1].strip().split()`，亲手算一遍 `abs(0.00 - 612.00) = 612.0`。

#### 4.1.5 小练习与答案

**练习 1**：为什么返回值是 `abs(media_box[0] - media_box[2])` 而不是直接取 `media_box[2]`？
**答案**：MediaBox 的原点不保证在 (0, 0)。一个完全合法的 PDF 可以把页面放在 \([100, 100, 712, 892]\)，此时右上角 x 坐标是 712 但宽度仍是 612 点。取两坐标差的绝对值在平移甚至坐标反向的情况下都能得到正确的非负宽高。

**练习 2**：A4 纸（210×297 毫米）的 MediaBox 大约是多少点？
**答案**：210 mm ≈ 8.27 英寸 → \(8.27 \times 72 \approx 595\) 点；297 mm ≈ 11.69 英寸 → \(\approx 842\) 点。所以 A4 是 595×842 点——这个数字在 4.2 的练习里还会用到。

**练习 3**：函数参数 `page_num` 传 0 会发生什么？
**答案**：`pdfinfo -f 0 -l 0` 不是合法页码，命令大概率以非零退出码结束，函数在 `returncode != 0` 检查处抛出 `ValueError`（错误信息来自 stderr）。这就是为什么 bench 代码里处处强调它是 1 起点的。具体行为待本地验证。

### 4.2 渲染单页：`render_pdf_to_base64png` 与 72 像素/点

#### 4.2.1 概念说明

知道页面有多少点之后，就能算出「要让最长边恰好是 D 像素，该用多少 DPI」。核心是一句话：**像素数 = 点数 × dpi / 72**（72 像素/点正是 DPI 定义与 PDF 点定义的换算桥）。

设页面最长边为 \(L\) 点、目标最长边为 \(D\) 像素，解方程 \(L \times \text{dpi} / 72 = D\) 得：

\[ \text{dpi} = \frac{D \times 72}{L} \]

这正是源码第 52 行那个表达式。这样渲染出的图**最长边精确等于 D**（像素取整可能有 ±1 偏差），短边按原始宽高比等比缩放。

为什么是「控制最长边」而不是「控制短边」或「固定 DPI」？因为 VLM 对图像的处理成本与**图像总像素数**（进而与视觉 token 数）挂钩，控制最长边就控制了成本上界；同时等比缩放保持了页面纵横比，不会拉伸变形。

#### 4.2.2 核心流程

```text
render_pdf_to_base64png(pdf 路径, 页码, 目标最长边 D)
  ├─ 1. 调 4.1 的函数 → (w_pt, h_pt)，取 longest_dim = max(两者)
  ├─ 2. 计算 dpi = D × 72 / longest_dim
  ├─ 3. subprocess 调 pdftoppm：
  │       pdftoppm -png -f N -l N -r <dpi> <pdf>
  │     不给输出文件名参数 → 图像二进制直接写到 stdout
  ├─ 4. 断言退出码为 0（超时上限 120 秒）
  └─ 5. base64 编码 stdout 字节 → 返回 str
```

关键参数：`-png` 指定输出 PNG 格式；`-f/-l` 只渲染第 N 页；`-r` 接受**浮点数** DPI（例如 117.09），poppler 会按它换算像素网格。

#### 4.2.3 源码精读

函数主体与最长边计算：

[olmocr/data/renderpdf.py:39-41](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/data/renderpdf.py#L39-L41)
这段代码定义 `render_pdf_to_base64png(local_pdf_path, page_num, target_longest_image_dim=2048)`，第一步先取 MediaBox 宽高中的较大者 `longest_dim`——分辨率换算的分母。注意函数签名的默认值是 2048，但这只是兜底，真正生效的值永远由调用方传入（见本节末尾的「各场景分辨率一览」）。

拼装 `pdftoppm` 命令并捕获 stdout：

[olmocr/data/renderpdf.py:43-60](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/data/renderpdf.py#L43-L60)
这段代码以 `-png -f N -l N -r <dpi>` 参数调用 `pdftoppm`，超时 120 秒；命令行里**没有输出文件名参数**，poppler 的约定是此时把图像数据直接写到标准输出，于是 `stdout=subprocess.PIPE` 拿到的就是完整的 PNG 字节流。`-r` 的值就是换算公式本身：

```python
str(target_longest_image_dim * 72 / longest_dim),  # 72 pixels per point is the conversion factor
```

最后 `base64.b64encode(...).decode("utf-8")` 把 PNG 字节变成可塞进 JSON 的 ASCII 字符串。

算个具体例子（edgar.pdf 第 1 页，612×792 点，D=1288）：

\[ \text{dpi} = \frac{1288 \times 72}{792} \approx 117.09 \]
\[ \text{宽} = 612 \times \frac{117.09}{72} \approx 995 \text{ px}, \quad \text{高} = 1288 \text{ px} \]

即输出约 **995×1288** 的竖版图。D=644 时按比例约为一半：**497×644**。这两个数请在本节实践中亲手验证（像素取整可能带来 ±1 的偏差）。

流水线里是怎么用它的——渲染被包进线程与信号量：

[olmocr/pipeline.py:106-112](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L106-L112)
这段代码是流水线侧的包装函数 `build_page_query`：因为 `subprocess.run` 是阻塞调用，直接在协程里跑会卡死整个事件循环（u2-l1 讲过 worker 是 asyncio 协程），所以用 `asyncio.to_thread` 丢进线程池，并且套上模块级信号量 `pdf_render_max_workers_limit` 限流。

[olmocr/pipeline.py:87-87](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L87-L87)
这一行定义该信号量的初值：优先取 Beaker 分配的 CPU 数，否则 `cpu_count - 2`——渲染是 CPU 密集型子进程，不加限制地并发跑几百个 `pdftoppm` 会把机器打满。

渲染结果随后被包成 OpenAI 风格的请求体（`data:image/png;base64,...`），并在 worker 的单页处理路径里被调用：

[olmocr/pipeline.py:170-176](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L170-L176)
这段代码展示 `try_single_page` 调用 `build_page_query(pdf_local_path, page_num, args.target_longest_image_dim, ...)`——CLI 参数一路传到渲染函数。

CLI 默认值——流水线真正的「常用分辨率」是 1288：

[olmocr/pipeline.py:1229-1229](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1229-L1229)
这一行定义 `--target_longest_image_dim` 参数，默认 1288，帮助文案就是「渲染 PDF 页面时最长边使用的尺寸」。

**各场景分辨率一览**（都来自真实源码，可对照体会「同一函数、不同档位」）：

| 调用方 | 目标最长边 | 出处 |
|---|---|---|
| 推理流水线 CLI | **1288** | [olmocr/pipeline.py:1229](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1229-L1229) |
| GRPO 强化学习 | 1288 | [olmocr/train/grpo_train.py:345](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/train/grpo_train.py#L345-L345) |
| SFT 训练配置 | 1024 | [olmocr/train/config.py:33](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/train/config.py#L33-L33) |
| bench 的 GPT-4o/Claude/Gemini runner | 2048 | [olmocr/bench/runners/run_chatgpt.py:41](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/bench/runners/run_chatgpt.py#L41-L41) |
| 函数签名兜底默认 | 2048 | [olmocr/data/renderpdf.py:39](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/data/renderpdf.py#L39-L39) |

一个值得玩味的算术事实：**1288 = 28 × 46，644 = 28 × 23**，都恰好是 28 的整数倍。Qwen2.5-VL 的视觉编码器以 14 像素为一个 patch、2×2 合并（等效 28 像素网格）做动态分辨率处理，渲染边长对齐 28 可以减少边缘补齐产生的无效 token。这是从算术事实与模型公开机制出发的**合理推断**，源码中没有注释明确说明这一点，读者姑妄听之。

顺带一提，文件里还有一个 WebP 变体：

[olmocr/data/renderpdf.py:63-70](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/data/renderpdf.py#L63-L70)
这段代码先调 `render_pdf_to_base64png` 得到 PNG，再用 PIL 转存为 WebP 并 base64 返回——WebP 体积远小于 PNG，用于 `viewer/dolmaviewer.py` 和 bench 报告这类「人看 HTML」而非「模型吃图」的场景。

#### 4.2.4 代码实践

这是本讲的主实践：**验证「最长边 = 设定值」这一核心不变量**。

1. **实践目标**：对同一页面分别以 644 / 1288 渲染，确认输出 PNG 最长边等于设定值，且宽高比与 MediaBox 一致。
2. **操作步骤**：在仓库根目录创建 `render_check.py`（示例代码，非项目原有文件）：

   ```python
   # render_check.py（示例代码）
   import base64
   from io import BytesIO
   from PIL import Image
   from olmocr.data.renderpdf import (
       render_pdf_to_base64png,
       get_pdf_media_box_width_height,
       get_png_dimensions_from_base64,
   )

   pdf = "tests/gnarly_pdfs/edgar.pdf"
   w_pt, h_pt = get_pdf_media_box_width_height(pdf, 1)
   print(f"MediaBox: {w_pt} x {h_pt} pt")

   for target in (644, 1288):
       b64 = render_pdf_to_base64png(pdf, 1, target_longest_image_dim=target)
       with open(f"page_{target}.png", "wb") as f:      # 存成文件方便肉眼看
           f.write(base64.b64decode(b64))
       fast = get_png_dimensions_from_base64(b64)        # 4.3 的快速读法
       pil = Image.open(BytesIO(base64.b64decode(b64))).size  # PIL 的权威读法
       print(f"target={target}: fast={fast}, PIL={pil}, longest={max(fast)}")
   ```

   运行 `python render_check.py`（需要 `pip install olmocr` 且系统装有 poppler）。
3. **需要观察的现象**：两种读法得到的宽高应当一致；打开 `page_644.png` 与 `page_1288.png`，后者肉眼明显更清晰（文字边缘更锐利）。
4. **预期结果**：按 4.2.3 的手算，612×792 的页面在 target=1288 时输出约 `995×1288`、target=644 时约 `497×644`，`longest` 一列分别等于 644 与 1288（像素取整可能有 ±1 偏差，具体数值待本地验证）。再算一列 `宽/高` 比值，应与 `612/792` 一致——等比缩放没有变形。

#### 4.2.5 小练习与答案

**练习 1**：A4 页面（595×842 点）以 target=1288 渲染，dpi 是多少？输出大约多大？
**答案**：\(\text{dpi} = 1288 \times 72 / 842 \approx 110.1\)；宽 \(\approx 595 \times 110.1/72 \approx 910\) px，高 \(\approx 842 \times 110.1/72 = 1288\) px。输出约 910×1288。

**练习 2**：如果去掉命令行里的 `-r` 参数，输出会怎样？
**答案**：`pdftoppm` 默认 150 DPI，输出尺寸不再受 `target_longest_image_dim` 控制：612 点宽会渲染成 \(612 \times 150/72 = 1275\) px。长边 792 点则变成 1650 px——远超 1288 的目标，视觉 token 成本随之膨胀。「控制最长边」的核心机制就失效了。

**练习 3**：函数签名默认 2048，流水线却默认 1288，这说明什么设计态度？
**答案**：`renderpdf.py` 作为共享底座不替调用方做决策，2048 只是兜底；分辨率是**场景相关的超参数**——线上推理选 1288（兼顾精度与成本），SFT 训练用 1024，调用闭源大模型的 bench runner 给 2048（那些模型按像素计费且没有本地 patch 对齐问题）。同一个渲染函数，档位由各消费方按自己的模型与预算决定。

### 4.3 快速读尺寸：`get_png_dimensions_from_base64`

#### 4.3.1 概念说明

渲染完之后，很多下游场景只想知道「这张图多大了」——例如 bench 的 runner 要按宽高决定发给 API 的处理方式、`mine_multi_column.py` 要按宽高做布局分析。如果为了读个宽高就 `b64decode` 整条字符串（可能上 MB）再让 PIL 解析整张 PNG，就太浪费了。

这个函数利用 2.4 节的 PNG 结构知识：**宽高就在文件第 16~24 字节**，所以只切出 base64 字符串中覆盖这 9 个字节的那一小段（12 个字符），解码这 12 个字符就够了。docstring 里写得很直白：`Should be really fast to support filtering`（要足够快以支持过滤场景）。

#### 4.3.2 核心流程

难点在于**字节下标 → base64 字符下标**的换算。base64 以 4 字符为一组、每组对应 3 字节：

\[ \text{字符组边界} = \left\lfloor \frac{\text{字节下标}}{3} \right\rfloor \times 4 \]

```text
get_png_dimensions_from_base64(b64 字符串)
  ├─ 1. 校验前 8 个 base64 字符 ≈ PNG 签名前 6 字节，否则 ValueError
  ├─ 2. 需要的字节区间：[16, 24)（宽 4 字节 + 高 4 字节）
  ├─ 3. 换算成 base64 字符区间：
  │       start = (16 // 3) * 4 = 20
  │       end   = ((24 + 2) // 3) * 4 = 32
  ├─ 4. 只解码 b64[20:32] → 9 字节（覆盖原文件第 15~23 字节）
  ├─ 5. 组内偏移 offset = 16 % 3 = 1
  │       width  = 解码字节[1:5]   （原文件 16~19 字节）
  │       height = 解码字节[5:9]   （原文件 20~23 字节）
  └─ 6. 大端 int.from_bytes → 返回 (width, height)
```

为什么 `offset = 字节下标 % 3` 恰好是解码结果里的位置？因为切出来的子串永远从**组边界**（字节 15，即 3 的倍数）开始，目标字节 16 在组内的偏移就是 \(16 - 15 = 1 = 16 \bmod 3\)。两个式子殊途同归。

#### 4.3.3 源码精读

PNG 签名的快速校验：

[olmocr/data/renderpdf.py:89-92](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/data/renderpdf.py#L89-L92)
这段代码把 8 字节 PNG 签名 `\x89PNG\r\n\x1a\n` 编成 base64，然后只比较入参的**前 8 个字符**。注意 8 个 base64 字符只覆盖 6 个字节，所以这是一次「够用就好」的启发式校验——足以挡住 JPEG 之类的异物，但并非完整的签名校验（见练习 2）。

字节区间声明与 base64 换算：

[olmocr/data/renderpdf.py:94-113](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/data/renderpdf.py#L94-L113)
这段代码把宽度字节区间 [16,20)、高度区间 [20,24) 换算成 base64 字符区间 [20,32)——`(start // 3) * 4` 对齐到组边界、`(end + 2) // 3 * 4` 向上取整覆盖残组——然后只 `b64decode` 这 12 个字符，得到 9 个原始字节。

按偏移取出宽高并转成大端整数：

[olmocr/data/renderpdf.py:116-127](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/data/renderpdf.py#L116-L127)
这段代码用 `offset = start_byte % 3` 在 9 字节里定位，切出 width/height 各 4 字节，`int.from_bytes(..., "big")` 按 PNG 规范的大端序转成整数。长度不足 4 字节则抛 `ValueError`。

真实调用方举例——bench 的 GPT-4o runner 用它读回渲染尺寸：

[olmocr/bench/runners/run_chatgpt.py:78-81](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/bench/runners/run_chatgpt.py#L78-L81)
这段代码在拿到 base64 图像后立刻调 `get_png_dimensions_from_base64` 取宽高，用于后续处理决策，而不是解码整张图。

仓库里也有一处「解码存盘再读」的对照写法：

[tests/test_anchor.py:161-166](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/tests/test_anchor.py#L161-L166)
这段代码（test_anchor.py 中的测试）把 base64 解码写入临时 PNG 文件再交给别的工具——测试场景不在意性能；两相对照更能体会「快速版」存在的意义。同一文件里还有一个直接用到本函数的断言：

[tests/test_anchor.py:262-266](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/tests/test_anchor.py#L262-L266)
这段代码解析出 buildsilver 生成的图像后断言 `max(width, height) == 2048`——正是「最长边等于目标」这一不变量在 silver 数据构建（分辨率为 2048 档）场景下的回归测试。

#### 4.3.4 代码实践

1. **实践目标**：证明「只看前 32 个字符」真的能读出整张图的尺寸，并观察非法输入的行为。
2. **操作步骤**：创建 `fast_dim_check.py`（示例代码，非项目原有文件）：

   ```python
   # fast_dim_check.py（示例代码）
   import base64
   from olmocr.data.renderpdf import render_pdf_to_base64png, get_png_dimensions_from_base64

   b64 = render_pdf_to_base64png("tests/gnarly_pdfs/edgar.pdf", 1, 644)
   print("整条 base64 长度（字符数）:", len(b64))
   print("完整输入 -> ", get_png_dimensions_from_base64(b64))
   print("只用前 32 字符 -> ", get_png_dimensions_from_base64(b64[:32]))

   # 喂一段 JPEG 魔数开头的数据，应当被签名校验挡下
   fake = base64.b64encode(b"\xff\xd8\xff\xe0" + b"x" * 40).decode()
   try:
       get_png_dimensions_from_base64(fake)
   except ValueError as e:
       print("caught:", e)
   ```

   运行 `python fast_dim_check.py`。
3. **需要观察的现象**：`len(b64)` 是几十万量级的数字，而 `b64[:32]` 只有 32 个字符——两者读出的宽高完全相同；JPEG 数据触发 `ValueError: Not a valid PNG file`。
4. **预期结果**：`b64[:32]` 覆盖了第 20~31 字符，恰好包含解码所需的全部分组，因此两种输入返回同一组 `(width, height)`。若把切片改成 `b64[:16]` 再试，会因解码出的高度字节不足 4 个而命中 `Insufficient data to extract dimensions` 分支——这个边界行为待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么不直接切 `b64[16:24]`（按字节下标切字符）？
**答案**：base64 的 4 字符组与 3 字节组是整体映射，从任意位置切开再解码会得到错位的字节。必须从组边界（4 字符的倍数、对应 3 字节的倍数）对齐：字符 20 对应字节 15~17 这一组的起点，因此解码后还要用 `offset = 16 % 3 = 1` 在组内修正到字节 16。

**练习 2**：签名校验完整吗？
**答案**：不完整。`png_signature_base64[:8]` 只取了 base64 前 8 个字符，对应 PNG 8 字节签名的**前 6 个字节**（`\x89PNG\r\x1a`）。对本项目的过滤场景（区分 PNG 与其他格式）足够，但它不是严格校验——构造一个前 6 字节相同的非 PNG 也能骗过这一步，然后在读宽高时得到无意义的数字。

**练习 3**：假设一张 1288 档页面的 base64 有 90 万字符，这个函数比「全量解码 + PIL 打开」快在哪？
**答案**：快在只解码 12 个字符（9 字节）而不是 90 万字符（约 67 万字节），并且完全跳过了 PNG 解压与像素解析。当过滤逻辑要对几百万张图逐个读尺寸时（正是 docstring 所说的 filtering 场景），这个量级的差距会被放大成分钟级甚至小时级的差别。

## 5. 综合实践

**任务：写一个「多 PDF 渲染审计」脚本，验证渲染不变量并发现形态各异的页面。**

要求（示例代码，自行实现）：

1. 从 `tests/gnarly_pdfs/` 里挑 4~5 个 PDF，建议包含：`edgar.pdf`（正常竖版）、`skinnypage.pdf`（极端纵横比）、`small_page_size.pdf`（小页面）、`edgar-rotated90.pdf`（内嵌旋转）、`newspaper.pdf`（报纸版面）。
2. 对每个 PDF 的第 1 页：先用 `get_pdf_media_box_width_height` 记录 `w_pt × h_pt`；再用 `render_pdf_to_base64png(target=1288)` 渲染；最后用 `get_png_dimensions_from_base64` 读回像素宽高。
3. 打印一张表，列为：文件名、MediaBox（pt）、实际 dpi（自己按公式补算）、PNG 尺寸（px）、最长边。逐行核对**最长边 ≈ 1288** 这一不变量。
4. 重点观察 `edgar-rotated90.pdf`：它的 MediaBox 报告的宽高与渲染出的像素宽高**方向一致吗**？思考原因——`pdfinfo` 读的是 MediaBox 的原始数值，而 `pdftoppm` 渲染的是「页面被展示时的样子」，两者对 `/Rotate` 旋转属性的处理可能不同。把你的观察写进表格备注。
5. 顺手加一列 `PNG 字节数`（`len(base64.b64decode(b64))`），对比不同页面的面积与体积关系。

这个任务把本讲三个函数串成了一条完整的迷你流水线：**量尺寸 → 换算渲染 → 快速读回校验**，也正是 pipeline.py 里 `build_page_query` 每处理一页时在幕后做的事情。第 4 步的旋转观察还能帮你提前理解后续讲义（u2-l6）里 `rotation_correction` 旋转纠错为什么要存在。

## 6. 本讲小结

- PDF 页面以**点**为单位（1 点 = 1/72 英寸），MediaBox 四个坐标给出页面矩形；`pdfinfo -box` 能以极低开销读出它，源码用 `abs(x0 - x1)` 抵御非零原点。
- 渲染分辨率的核心换算：\(\text{dpi} = D \times 72 / \max(w_{pt}, h_{pt})\)，使**最长边精确等于目标值**、短边等比缩放；像素数 = 点数 × dpi / 72。
- `pdftoppm` 不给输出文件名时把 PNG 写到 stdout，被 `subprocess.PIPE` 直接捕获，全程不落盘；流水线用 `asyncio.to_thread` + CPU 数信号量（`pipeline.py:87`）把阻塞的子进程调用安全地放进事件循环。
- 分辨率是场景超参数：线上推理与 GRPO 用 1288、SFT 训练用 1024、闭源模型 bench 用 2048；1288 与 644 恰为 28 的整数倍，与 Qwen2.5-VL 的 28 像素等效 patch 网格吻合（推断）。
- PNG 宽高固定存放在文件第 16~24 字节；`get_png_dimensions_from_base64` 通过 `(字节//3)*4` 的组对齐换算只解码 12 个 base64 字符就读出尺寸，专为海量过滤场景的极致性能设计。

## 7. 下一步学习建议

图片到手之后，下一步就是把它和提示词一起发给模型。建议：

1. **直接进入下一讲 u2-l4（提示词体系与 PageResponse 数据模型）**：看 `build_page_query` 返回体里的 `data:image/png;base64,...` 是如何与 `build_no_anchoring_v4_yaml_prompt()` 组装成请求的，以及模型被要求以什么样的结构化格式回应。
2. **提前翻一眼训练侧的复用**：[olmocr/train/dataloader.py:189-194](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/train/dataloader.py#L189-L194) 中的 `PDFRenderer` step——训练数据加载与线上推理调用的是同一个 `render_pdf_to_base64png`，这保证了「训练看到什么分辨率，推理就用什么分辨率」的一致性，是第 6 单元的前置伏笔。
3. **动手实验（可选）**：用不同 `target_longest_image_dim`（如 512 / 1024 / 2048）渲染同一页，肉眼对比小字号文本的可读性，体会分辨率与视觉 token 成本之间的权衡。
