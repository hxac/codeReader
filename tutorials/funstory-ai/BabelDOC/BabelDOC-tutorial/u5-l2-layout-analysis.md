# 版面分析：DocLayout 模型与 LayoutParser

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 midend 第二个 stage `Parse Page Layout` 在整条翻译流水线里的位置与职责。
- 理解 DocLayout-YOLO ONNX 模型「吃一张页面图、吐一组带类别与置信度的区域框」的输入输出。
- 读懂 `LayoutParser` 如何把模型在「图像空间」里检测到的框，映射回 PDF 的「IL 坐标空间」并写入 `page.page_layout`。
- 区分本地 ONNX 推理与 RPC 版面服务两条路径，并知道 `--rpc-doclayout*` 参数如何切换。
- 看懂「模型漏检」时由 `fallback_line` 兜底生成的行级版面框。

本讲承接 u5-l1（`DetectScannedFile` 是第一个 stage），继续沿 `_do_translate_single` 的执行顺序往下走。

## 2. 前置知识

在读懂本讲之前，你需要先建立以下几个直觉（相关细节在前面讲义里已铺垫）：

1. **版面（layout）是什么**：一篇 PDF 论文页并不是「一整块文字」，而是由标题、正文段落、图（figure）、表（table）、公式（formula）、页眉页脚（page-header/page-footer）等「语义区域」拼出来的。版面分析就是把这些区域一个一个框出来，并打上类别标签。
2. **YOLO 是什么**：You Only Look Once，一类目标检测模型。输入一张图，输出若干个「矩形框 + 类别 + 置信度」。本讲不需要懂它的训练原理，只要知道它是一个「图像 → 框」的黑盒即可。
3. **IL 坐标系（Box）**：IL 里的 [`Box`](babeldoc/format/pdf/document_il/il_version_1.py) 用 `(x, y, x2, y2)` 表示一个矩形，原点在页面**左下角**，y 轴**向上**（PDF 标准）。
4. **图像坐标系**：OpenCV/渲染出来的图片，原点在**左上角**，y 轴**向下**。两者 y 方向相反，这是本讲坐标映射的核心。
5. **stage 顺序**：`DetectScannedFile`（u5-l1）跑完后，紧接着就是 `LayoutParser`，它的权重是 `14.03`（见下方源码地图），在所有 midend stage 里排第二重。

> 一个关键常识：**1 英寸 = 72 点（point）= 72 像素 @ 72 DPI**。本讲会反复用到「72 DPI 渲染时，1 像素正好等于 1 个 PDF 点」这个事实。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [babeldoc/docvision/base_doclayout.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/docvision/base_doclayout.py) | 抽象基类 `DocLayoutModel`、检测结果容器 `YoloResult` / `YoloBox`。定义「版面模型」该长什么样。 |
| [babeldoc/docvision/doclayout.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/docvision/doclayout.py) | 本地 ONNX 实现 `OnnxModel`：加载模型、预处理、推理、`handle_document` 按页产图。 |
| [babeldoc/format/pdf/document_il/midend/layout_parser.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/layout_parser.py) | midend stage `LayoutParser`：把检测结果映射成 IL 的 `PageLayout`，并生成 `fallback_line`。 |
| [babeldoc/docvision/rpc_doclayout8.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/docvision/rpc_doclayout8.py) | RPC 版面服务实现 `RpcDocLayoutModel`：把推理外包给远程 `/inference`。 |
| [babeldoc/main.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py) | 根据 `--rpc-doclayout*` 参数，在「本地 ONNX」与「RPC」之间二选一，装配进 `TranslationConfig`。 |
| [babeldoc/assets/assets.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/assets/assets.py) | `get_doclayout_onnx_model_path`：下载、校验（SHA3-256）并返回本地 ONNX 模型路径。 |
| [babeldoc/format/pdf/document_il/utils/extract_char.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/utils/extract_char.py) | `convert_page_to_char_boxes` / `process_page_chars_to_lines`：用字符几何把字符聚成「行」，供 fallback 使用。 |
| [babeldoc/format/pdf/document_il/utils/layout_helper.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/utils/layout_helper.py) | 下游对版面类别做归类（`is_text_layout` / `figure_table_layouts` / `layout_priority`），体现版面类别如何被消费。 |

stage 注册与调用点在编排文件里：

- [high_level.py:62-63](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L62-L63) 把 `Parse Page Layout` 注册进 `TRANSLATE_STAGES`，权重 `14.03`。
- [high_level.py:952-961](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L952-L961) 在 `_do_translate_single` 里真正调用 `LayoutParser(translation_config).process(docs, doc_pdf2zh)`，并在 `--debug` 下把结果落盘为 `layout_generator.json`。

---

## 4. 核心概念与源码讲解

### 4.1 DocLayout-YOLO ONNX 模型

#### 4.1.1 概念说明

`Parse Page Layout` 阶段要回答的问题是：「这一页里，哪里是正文、哪里是标题、哪里是图、哪里是公式？」BabelDOC 不自己训练检测模型，而是复用开源的 **DocLayout-YOLO**，并把它导出成 **ONNX** 格式随包分发。

ONNX（Open Neural Network Exchange）是一种跨框架的模型交换格式：训练用 PyTorch，导出成 `.onnx` 后，可以用 `onnxruntime` 在任何机器上推理，不必装 PyTorch/CUDA 这套重依赖。这正契合 BabelDOC「想被轻量嵌入」的定位。

模型文件叫 `doclayout_yolo_docstructbench_imgsz1024.onnx`，由资源系统按需下载并校验：

- [assets.py:236-238](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/assets/assets.py#L236-L238) 取出模型文件名，并用 `DOCLAYOUT_YOLO_DOCSTRUCTBENCH_IMGSZ1024ONNX_SHA3-256` 校验完整性。
- [assets.py:266-267](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/assets/assets.py#L266-L267) `get_doclayout_onnx_model_path()` 是同步入口。

> 文件名里的 `imgsz1024` 暗示模型固定输入边长 1024，这点会在下面预处理里印证。

#### 4.1.2 核心流程

`OnnxModel` 的推理分四步：

1. **加载**：读 ONNX，从模型元数据（metadata）里抠出 `stride`（下采样步长）和 `names`（类别 id → 类别名 映射）。
2. **预处理**：把页面图等比缩放 + 灰边填充到 `1024×1024`，归一化到 `[0,1]`，转成 `BCHW`。
3. **推理**：`onnxruntime.InferenceSession.run`，得到原始检测张量。
4. **后处理**：按置信度 `> 0.25` 过滤框，把框从 `1024×1024` 缩放回原图尺寸，包成 `YoloResult`。

一句话：**图 → 模型 → 一堆带类别的框**。

#### 4.1.3 源码精读

**模型抽象与结果容器**（[base_doclayout.py:12-68](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/docvision/base_doclayout.py#L12-L68)）定义了「版面模型」的契约：

```python
class YoloResult:                       # 一张图的全部检测结果
    # boxes 按 conf 降序排序（line 21）
class YoloBox:                          # 单个检测框
    self.xyxy  # [x1,y1,x2,y2]，图像坐标系
    self.conf  # 置信度
    self.cls   # 类别 id（整数）
class DocLayoutModel(abc.ABC):          # 抽象基类
    @staticmethod
    def load_onnx(): ...                # 默认返回本地 OnnxModel（line 42-47）
    @property
    def stride(self) -> int: ...        # 抽象：模型下采样步长
    def handle_document(...): ...       # 抽象：按页遍历文档，逐页 yield (page, YoloResult)
```

注意 `load_onnx` / `load_available` 都默认落到本地 ONNX（[base_doclayout.py:42-51](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/docvision/base_doclayout.py#L42-L51)），而 `handle_document` 是抽象方法——本地版与 RPC 版各自实现，但对外接口一致。这就是 4.4 节「可切换」的根基。

**模型加载与元数据**（[doclayout.py:42-78](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/docvision/doclayout.py#L42-L78)）：

```python
class OnnxModel(DocLayoutModel):
    _FIXED_IMGSZ = 1024
    def __init__(self, model_path: str):
        model = onnx.load(model_path)
        metadata = {d.key: d.value for d in model.metadata_props}
        self._stride = ast.literal_eval(metadata["stride"])   # 从元数据读步长
        self._names  = ast.literal_eval(metadata["names"])    # 从元数据读类别名 {0:'title', ...}
        ...
        self.model = onnxruntime.InferenceSession(..., providers=providers)
        self.lock = threading.Lock()                          # 推理用锁串行化
```

类别名**不是硬编码**，而是从 ONNX 元数据 `names` 字段读出来（[doclayout.py:47-48](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/docvision/doclayout.py#L47-L48)）。这意味着换一个微调过的模型，类别集合就跟着变，BabelDOC 不必改代码。provider 选择上有个小优化：在 macOS（`Darwin`）且能用 CoreML 时，把输入形状固化为 `[1,3,1024,1024]` 让 CoreML 接管绝大部分计算图（[doclayout.py:52-66](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/docvision/doclayout.py#L52-L66)、[doclayout.py:80-96](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/docvision/doclayout.py#L80-L96)）；其他平台只挑 CPU provider，刻意避开 directml/cuda 的兼容性坑。

**预处理：缩放 + 填充**（[doclayout.py:107-153](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/docvision/doclayout.py#L107-L153)）的核心是 `resize_and_pad_image`：

```python
r = min(new_h / h, new_w / w)            # 等比缩放比（取较小，保证塞得下）
image = cv2.resize(image, (resized_w, resized_h), ...)
# 剩下的边用 (114,114,114) 灰边补到 1024×1024（letterbox）
image = cv2.copyMakeBorder(image, top, bottom, left, right, ...)
```

`(114,114,114)` 是 YOLO 系列惯用的「无意义灰」填充色。等比缩放 + 居中补灰边（letterbox）保证不拉伸变形。

**推理与后处理**（[doclayout.py:181-242](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/docvision/doclayout.py#L181-L242)）：

```python
target_imgsz = 1024
pix = self.resize_and_pad_image(img, new_shape=target_imgsz)
pix = np.transpose(pix, (2, 0, 1))        # HWC → CHW
pix = pix.astype(np.float32) / 255.0      # 归一化到 [0,1]
batch_input = np.stack(processed_batch, axis=0)          # BCHW
batch_preds = self.model.run(None, {"images": batch_input})[0]
for j in range(batch_size_actual):
    preds = batch_preds[j]
    preds = preds[preds[..., 4] > 0.25]                  # 置信度过滤（line 233）
    if len(preds) > 0:
        preds[..., :4] = self.scale_boxes((new_h, new_w), preds[..., :4], orig_shapes[j])
    results.append(YoloResult(boxes_data=preds, names=self._names))
```

两个关键数字：输入固定 `1024`（[doclayout.py:209](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/docvision/doclayout.py#L209)），置信度阈值 `0.25`（[doclayout.py:233](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/docvision/doclayout.py#L233)）。`scale_boxes`（[doclayout.py:155-179](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/docvision/doclayout.py#L155-L179)）做的事正好是预处理的逆运算：先把框减去灰边 padding，再除以缩放比 `gain`，把框从 `1024×1024` 还原回原图像素坐标。

#### 4.1.4 代码实践

**实践目标**：亲手加载模型，对一页 PDF 推理，把检测到的类别与坐标打印出来。

**操作步骤**：

1. 先跑一次 `babeldoc --warmup`（或任意一次翻译），让模型下载到 `~/.cache/babeldoc`。
2. 写一个最小脚本 `layout_probe.py`（**示例代码**，非项目自带）：

   ```python
   import pymupdf
   from babeldoc.docvision.base_doclayout import DocLayoutModel
   from babeldoc.format.pdf.document_il.utils.mupdf_helper import get_no_rotation_img
   import numpy as np

   model = DocLayoutModel.load_onnx()          # 本地 ONNX
   doc = pymupdf.open("examples/ci/test.pdf")
   page = doc[0]
   pix = get_no_rotation_img(page)             # dpi=72，去旋转渲染
   img = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, 3)[:, :, ::-1]
   result = model.predict(img)[0]              # 一个 YoloResult
   for box in result.boxes:
       print(result.names[box.cls], round(float(box.conf), 3), box.xyxy.tolist())
   ```

3. 运行 `python layout_probe.py`。

**需要观察的现象**：控制台输出若干行，每行形如 `title 0.91 [x1, y1, x2, y2]`，类别名来自模型的 `names`，坐标是**图像像素**（左上角原点）。

**预期结果**：能稳定看到类似 `title`、`text-block`（正文）、`figure`、`table` 等类别，置信度普遍 `> 0.5`。坐标范围在 `0~pix.height/width` 之间。

**待本地验证**：因模型版本差异，你看到的**类别名清单**可能与本讲示例不同——这正是「类别从元数据读取」的结果。请以你机器上 `result.names` 的实际值为准，并把它记下来。

#### 4.1.5 小练习与答案

**练习 1**：为什么 BabelDOC 用 ONNX 而不是直接跑 PyTorch 权重？

**答案**：ONNX + `onnxruntime` 不依赖 PyTorch/CUDA 这套重型工具链，部署体积小、跨平台，契合 BabelDOC「想被轻量嵌入下游项目」的定位。

**练习 2**：`resize_and_pad_image` 为什么要用 `(114,114,114)` 灰边而不是黑色 `(0,0,0)`？

**答案**：`(114,114,114)` 是 YOLO 训练时 letterbox 用的标准填充色。推理时用相同填充，能保持与训练分布一致，避免黑边引入分布偏移导致检测变差。

---

### 4.2 区域检测结果与 `handle_document`

#### 4.2.1 概念说明

`predict` 处理的是「单张图」，但流水线要处理「整个文档」。`handle_document` 就是这层封装：它逐页把 PDF 页面渲染成图、调 `predict`、再把 `(page, YoloResult)` 一对一对 yield 出去。它是一个**生成器（generator）**，这样 `LayoutParser` 可以「边出结果、边映射、边报进度」，而不必等所有页都算完。

#### 4.2.2 核心流程

```
for page in pages:
    raise_if_cancelled()              # 支持中途取消
    pix = get_no_rotation_img(page)   # 渲染成图（dpi=72，去旋转）
    image = reshape(pix.samples)      # 字节 → numpy HWC
    result = predict(image)[0]
    save_debug_image(...)             # --debug 时存带框图
    yield page, result
```

#### 4.2.3 源码精读

[doclayout.py:244-269](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/docvision/doclayout.py#L244-L269)：

```python
def handle_document(self, pages, mupdf_doc, translate_config, save_debug_image):
    for page in pages:
        translate_config.raise_if_cancelled()          # 取消点
        with self.lock:                                 # 渲染串行（pymupdf 非线程安全）
            pix = get_no_rotation_img(mupdf_doc[page.page_number])   # 默认 dpi=72
        image = np.frombuffer(pix.samples, np.uint8).reshape(
            pix.height, pix.width, 3)[:, :, ::-1]       # RGB→BGR
        predict_result = self.predict(image)[0]
        save_debug_image(image, predict_result, page.page_number + 1)
        yield page, predict_result
```

三个要点：

- **`get_no_rotation_img`**（[mupdf_helper.py:7-13](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/utils/mupdf_helper.py#L7-L13)）临时把页面旋转角置 0 再渲染、渲染完恢复，避免旋转页把框算歪。
- **默认 `dpi=72`**：1 像素 = 1 点，所以图像像素坐标≈PDF 点坐标，这是 4.3 节「只翻 y、不缩放」的前提。RPC 版（4.5 节）用 `dpi=150`，于是要额外缩放。
- **`self.lock`**：渲染这一步加锁串行，因为 PyMuPDF 的 `Page` 对象不是线程安全的。

**类别如何被下游消费**：`YoloBox.cls` 是整数 id，需要 `result.names[cls]` 查到字符串类名（如 `"text-block"`）。下游 `layout_helper.py` 把这些类名归成几大类：

- 文本类（[layout_helper.py:801-839](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/utils/layout_helper.py#L801-L839) `is_text_layout`）：`title`、`plain text`、`text`、`caption`、`footnote`、`page_header`、`page_footer`、`list_item`……
- 图/表类（[layout_helper.py:901-913](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/utils/layout_helper.py#L901-L913) `figure_table_layouts`）：`figure`、`table`、各 `*_caption`、`table_cell` 等。
- 公式类（[layout_helper.py:859](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/utils/layout_helper.py#L859) `formula_layout_types = {"formula"}`）。

下游把这么多类名「并集」收录，是为了兼容不同版本的 DocLayout-YOLO 模型——这也印证了「类别来自元数据」的设计意图。

#### 4.2.4 代码实践

**实践目标**：在 4.1.4 的脚本基础上，对照下游分类函数，统计你这页检测出的区域分别属于「文本 / 图表 / 公式」哪一类。

**操作步骤**：在 `layout_probe.py` 末尾追加（**示例代码**）：

```python
from babeldoc.format.pdf.document_il.utils.layout_helper import (
    is_text_layout, is_character_in_formula_layout,
)
from babeldoc.format.pdf.document_il.utils.layout_helper import Layout

text_n = fig_n = 0
for box in result.boxes:
    name = result.names[box.cls]
    lay = Layout(box.cls, name)
    if is_text_layout(lay):
        text_n += 1
    else:
        fig_n += 1
print("text-like:", text_n, "non-text:", fig_n)
```

**需要观察的现象**：标题、正文、页眉页脚会被判为 text-like；图、表、公式等归为 non-text。

**预期结果**：一篇普通论文首页，text-like 区域通常多于 non-text 区域。

**待本地验证**：若 `is_text_layout` 因类别名不在清单里而全部判 False，说明你的模型类别名与下游清单不一致——可补打 `result.names` 全量值对比。

#### 4.2.5 小练习与答案

**练习 1**：`handle_document` 为什么设计成生成器（`yield`）而不是返回一个列表？

**答案**：生成器让 `LayoutParser` 能逐页「检测→映射→报进度」，无需等全部页算完。对大文档更省内存，也让 `progress_monitor` 的进度条更平滑。

**练习 2**：`YoloResult.__init__` 里为什么要 `self.boxes.sort(key=lambda x: x.conf, reverse=True)`？

**答案**：把置信度高的框排在前面。后续若有按顺序取「最可信区域」的逻辑，优先命中高置信框，降低误检影响。

---

### 4.3 图像空间到 IL 坐标的映射

#### 4.3.1 概念说明

模型吐出的框是**图像坐标系**（左上原点，y 向下）。但 IL 的 `Box` 是 **PDF 坐标系**（左下原点，y 向上）。`LayoutParser.process` 最核心的一段代码，就是做这个 y 轴翻转。

#### 4.3.2 核心流程

对每个检测框 `(x0,y0,x1,y1)`（图像空间）：

1. 用页面 `mediabox_size` 拿到页高 `h`、页宽 `w`（单位：点）。
2. y 翻转：`新y = h − 旧y`（图像的上变成 PDF 的下）。
3. 顺手给框**外扩 1 个像素**（`x0-1, x1+1`），做一点容差。
4. `np.clip` 把框夹回页面边界 `[0, w-1]`，防止越界。
5. 用映射后的坐标造一个 [`PageLayout`](babeldoc/format/pdf/document_il/il_version_1.py)（含 `box`、`id`、`conf`、`class_name`）。

#### 4.3.3 源码精读

[layout_parser.py:119-176](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/layout_parser.py#L119-L176) 的 `process` 方法：

```python
for page, layouts in self.model.handle_document(
    docs.page, mupdf_doc, self.translation_config, self._save_debug_image
):
    page_layouts = []
    for layout in layouts.boxes:
        x0, y0, x1, y1 = layout.xyxy                  # 图像空间
        box = mupdf_doc[page.page_number].mediabox_size
        b_h = math.ceil(box.y); b_w = math.ceil(box.x)
        h, w = b_h, b_w
        x0, y0, x1, y1 = (
            np.clip(int(x0 - 1), 0, w - 1),            # 外扩 + 夹界
            np.clip(int(h - y1 - 1), 0, h - 1),        # y 翻转：上边
            np.clip(int(x1 + 1), 0, w - 1),
            np.clip(int(h - y0 + 1), 0, h - 1),        # y 翻转：下边
        )
        page_layout = il_version_1.PageLayout(
            id=len(page_layouts) + 1,
            box=il_version_1.Box(x0.item(), y0.item(), x1.item(), y1.item()),
            conf=layout.conf.item(),
            class_name=layouts.names[layout.cls],      # 整数 id → 类名字符串
        )
        page_layouts.append(page_layout)
    page.page_layout = page_layouts                    # 写回 IL
    progress.advance(1)
```

坐标映射的数学（设页高 `h`、图像空间框 `[x0,y0,x1,y1]`，下标 t/b 为上/下）：

\[
x' = x \quad(\text{x 方向不变，仅外扩 } \pm 1)
\]

\[
y'_{\text{bottom}} = h - y_{\text{top}}, \qquad y'_{\text{top}} = h - y_{\text{bottom}}
\]

> 为什么是 `h - y1 - 1` 而不是 `h - y1`？`-1/+1` 是外扩 1 像素的容差，让映射后的框比检测框略大一圈，对下游「字符是否落在该版面内」的 IoU 判定更宽松，减少边界字符被漏判。

**为什么图像像素能直接当 PDF 点用？** 因为 `handle_document` 用 `dpi=72` 渲染，72 DPI 下 1 像素 = 1 点。所以这里**没有缩放，只有 y 翻转**。如果改成别的 DPI，这段映射就得引入缩放因子（4.5 节的 RPC 版正是如此）。

写回 IL 的结果：`page.page_layout` 变成一个 `PageLayout` 列表（[il_version_1.py:314-345](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/il_version_1.py#L314-L345)），每个含 `box`/`id`/`conf`/`class_name`。这个列表随后会被 `ParagraphFinder`、`StylesAndFormulas`、`TableParser`、`Typesetting` 反复读取——版面是下游一切字符归属判断的「底图」。

#### 4.3.4 代码实践

**实践目标**：手工验证坐标映射的正确性。

**操作步骤**：

1. 在 4.1.4 脚本里，对同一个框，分别打印「图像空间 `box.xyxy`」和「按本节公式手算的 IL 空间坐标」：

   ```python
   h, w = pix.height, pix.width
   for box in result.boxes:
       x0,y0,x1,y1 = box.xyxy
       print("img:", [int(v) for v in box.xyxy],
             "il :", [int(x0)-1, h-int(y1)-1, int(x1)+1, h-int(y0)+1])
   ```

2. 再用 `--debug` 跑一次真实翻译，打开工作目录里的 `layout_generator.json`（由 [high_level.py:957-961](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/high_level.py#L957-L961) 落盘），找同一页的 `pageLayout`。

**需要观察的现象**：手算的 IL 坐标应与 `layout_generator.json` 里对应 `pageLayout.box` 的值一致（允许 1 像素取整误差）。

**预期结果**：两者吻合，说明「72 DPI 下只翻 y」的映射理解正确。

**待本地验证**：若你的 PDF 页面带 `/Rotate`，注意 `get_no_rotation_img` 已去旋转，`mediabox_size` 仍是原始 mediabox——坐标对齐应以去旋转后的图为准。

#### 4.3.5 小练习与答案

**练习 1**：如果某天有人把 `handle_document` 的渲染 DPI 从 72 改成 144，`process` 里的坐标映射会出什么问题？

**答案**：144 DPI 下 1 点 = 2 像素，模型输出的框是「2 倍像素」坐标，但映射仍按 `h`（点）去翻转且不缩放，框会比真实位置大一倍。需要像 4.5 节 RPC 版那样引入 `scale = 72 / DPI` 修正。

**练习 2**：`np.clip(..., 0, w - 1)` 为什么用 `w - 1` 而不是 `w`？

**答案**：合法像素/坐标索引从 0 开始，最大有效值是 `w - 1`，用 `w` 会越界一格。

---

### 4.4 `fallback_line`：模型漏检的行级兜底

#### 4.4.1 概念说明

模型不是万能的，偶尔会把某段文字「漏框」，或把一整块正文只框成一个大块而不分细行。如果完全依赖模型版面，下游 `ParagraphFinder` 聚段时就可能因为缺版面框而丢字。

BabelDOC 的对策是 **`fallback_line`**：不依赖模型，直接用 IL 里已有的字符几何（`pdf_character` 的 `visual_bbox`），用聚类算法把字符重新聚成「行」，每行生成一个 `class_name="fallback_line"` 的版面框，**追加**到模型版面之后。它是纯几何、确定性的兜底。

#### 4.4.2 核心流程

```
对每一页（用线程池并发）:
    取已有的模型版面 exists_page_layouts
    convert_page_to_char_boxes(page)          # 抽出每个字符的 (box, unicode, vertical)
    process_page_chars_to_lines(char_boxes)   # DBSCAN 聚类成行
    for 每个行簇 cluster:
        算该行所有字符的总包围盒
        new PageLayout(box=包围盒, conf=1, class_name="fallback_line")
        追加到 exists_page_layouts
```

#### 4.4.3 源码精读

触发点在 `process` 末尾，用一个线程池并发处理所有页（[layout_parser.py:171-175](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/layout_parser.py#L171-L175)）：

```python
with ThreadPoolExecutor(max_workers=os.cpu_count()) as executor:
    for page in docs.page:
        executor.submit(self.generate_fallback_line_layout_for_page, page, progress)
```

注意 `stage_start` 的总量是 `total * 2`（[layout_parser.py:123-126](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/layout_parser.py#L123-L126)）：每页两份进度——一份给模型检测、一份给 fallback，二者各 `advance(1)`，凑齐。

兜底生成主体（[layout_parser.py:178-211](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/layout_parser.py#L178-L211)）：

```python
def generate_fallback_line_layout_for_page(self, page, progress):
    try:
        exists_page_layouts = page.page_layout
        char_boxes = convert_page_to_char_boxes(page)              # 字符 → (box, unicode, vertical)
        if not char_boxes:
            return
        clusters = process_page_chars_to_lines(char_boxes)         # 聚类成行
        for cluster in clusters:
            boxes = [c[0] for c in cluster.chars]
            min_x = min(b.x for b in boxes); max_x = max(b.x2 for b in boxes)
            min_y = min(b.y for b in boxes); max_y = max(b.y2 for b in boxes)
            cluster.chars = il_version_1.Box(min_x, min_y, max_x, max_y)  # 复用字段存包围盒
            page_layout = il_version_1.PageLayout(
                id=len(exists_page_layouts) + 1,
                box=il_version_1.Box(min_x, min_y, max_x, max_y),
                conf=1,
                class_name="fallback_line",
            )
            exists_page_layouts.append(page_layout)                # 追加，不覆盖模型版面
        self._save_debug_box_to_page(page)
    finally:
        progress.advance(1)
```

**字符聚行的算法**在 [extract_char.py](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/utils/extract_char.py)：

- `convert_page_to_char_boxes`（[extract_char.py:151-157](https://github.com/funstory-ai-BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/utils/extract_char.py#L151-L157)）：把每个 `pdf_character` 投影成 `(visual_bbox.box, char_unicode, vertical)` 三元组。
- `process_page_chars_to_lines`（[extract_char.py:566-572](https://github.com/funstory-ai-BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/utils/extract_char.py#L566-L572)）→ `_cluster_by_axis`（[extract_char.py:160-345](https://github.com/funstory-ai-BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/utils/extract_char.py#L160-L345)）：先按副轴（横向文字的 y）用重叠率分「带（band）」，再在每个带里用 **DBSCAN** 按主轴（x）聚成行；过宽/过高的行还会被二次拆分。

> 关键设计：fallback 行用的是**字符几何**，与模型用的是「渲染图」。两套来源、同一套 `PageLayout` 容器，下游再按 `class_name` 区分对待——`fallback_line` 在 `_save_debug_box_to_page` 里用更细的线宽（`scale_factor=0.1`，[layout_parser.py:70-72](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/layout_parser.py#L70-L72)）以便和模型框区分。

#### 4.4.4 代码实践

**实践目标**：观察 fallback 行在什么条件下产生、与模型框有何不同。

**操作步骤**：

1. 用 `--debug` 翻译 `examples/ci/test.pdf`：
   ```bash
   babeldoc --openai --openai-api-key sk-xxx --files examples/ci/test.pdf \
            --debug --output ./out
   ```
2. 打开工作目录的 `layout_generator.json`，挑一页，分别统计：
   - `class_name` 不为 `"fallback_line"` 的版面数（模型框）；
   - `class_name == "fallback_line"` 的版面数（兜底框）。
3. 若开了 `ocr-box-image` 调试图（[layout_parser.py:26-59](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/format/pdf/document_il/midend/layout_parser.py#L26-L59)），打开对应页 jpg，绿色粗框是模型框，最终 PDF 里细线框是 fallback。

**需要观察的现象**：模型框是大块（标题/正文/图），fallback 框是细粒度的「行」。两者叠加覆盖全页字符。

**预期结果**：即便模型漏框了某段，fallback 行也能补上对应区域的行级框，确保没有字符「悬空」无版面归属。

**待本地验证**：fallback 行数量与该页字符密度正相关；空白页或纯图页 `char_boxes` 为空会提前 `return`，不产生 fallback。

#### 4.4.5 小练习与答案

**练习 1**：为什么 fallback 用 DBSCAN 而不是简单的「按 y 坐标等高分组」？

**答案**：论文里行高、行距不固定，还有分栏、上下标。DBSCAN 基于字符间距自适应聚类（`eps` 取平均字宽的倍数），能容忍这种不规律；等高分组遇到稍微错位的行就会断裂。

**练习 2**：`fallback_line` 的框 `conf=1`，而模型框 `conf` 通常 `< 1`。这样设计有什么好处？

**答案**：`conf=1` 标记「这是确定性几何兜底、非概率检测」，下游可以据此区分对待——例如在置信度排序时把 fallback 当作「确定存在」而非「可能存在」。

---

### 4.5 RPC 版面服务与本地 ONNX 的切换

#### 4.5.1 概念说明

本地 ONNX 推理虽好，但在两类场景下不理想：(1) **CPU 较弱或无 GPU 的机器**，推理慢；(2) **批量服务**（如 BabelDOC 自带的 executor RPC 服务），希望把推理集中到带 GPU 的机器上。

于是有了 `RpcDocLayoutModel`：它实现同一套 `DocLayoutModel` 接口，但 `handle_document` 不在本地跑模型，而是**把页面图 base64 编码 POST 给远程 `/inference`**，由服务端跑模型、回传框。`LayoutParser` 完全无感——它只认 `handle_document` 这个接口。

#### 4.5.2 核心流程

```
main.py: 根据 --rpc-doclayout* 参数二选一
    有 rpc 参数  → RpcDocLayoutModel(host=...)
    否则         → DocLayoutModel.load_onnx()   # 本地 ONNX
装配进 translation_config.doc_layout_model
LayoutParser.__init__ 里 self.model = translation_config.doc_layout_model
```

RPC 版 `handle_document` 用**滑动窗口并发**：一边按页提交 HTTP 请求（最多 N 个在飞），一边按页序 yield 结果，保证顺序且不阻塞。

#### 4.5.3 源码精读

**装配切换**（[main.py:543-575](https://github.com/funstory-ai-BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L543-L575)）：

```python
if args.rpc_doclayout:
    from babeldoc.docvision.rpc_doclayout import RpcDocLayoutModel
    doc_layout_model = RpcDocLayoutModel(host=args.rpc_doclayout)
elif args.rpc_doclayout2:
    ...   # rpc_doclayout2 ~ rpc_doclayout7 同理，对应不同版本实现
else:
    from babeldoc.docvision.doclayout import DocLayoutModel
    doc_layout_model = DocLayoutModel.load_onnx()      # 默认本地
```

`rpc_doclayout` 到 `rpc_doclayout7` 是不同版本的 RPC 适配，`rpc_doclayout8` 是最新一代（executor 服务用它，见 [babeldoc_adapter.py:357-362](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/tools/executor/babeldoc_adapter.py#L357-L362)）。它们都继承 `DocLayoutModel`，对 `LayoutParser` 透明。

**RPC 请求与 DPI 缩放**（[rpc_doclayout8.py:188-218](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/docvision/rpc_doclayout8.py#L188-L218)、[rpc_doclayout8.py:278-315](https://github.com/funstory-ai-BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/docvision/rpc_doclayout8.py#L278-L315)）：

```python
DPI = 150
def _prepare_page_input(...):
    pix = get_no_rotation_img(mupdf_doc[page.page_number], dpi=DPI)   # 150 DPI，比本地清晰
    image_data = _encode_image(image)                                 # base64 jpg
def _request_layout(...):
    request_body = {"schema_version": 1, "page_number": ...,
                    "dpi": DPI, "image": base64..., "image_size": [...]}
    response = httpx.post(f"{self.host}/inference", json=request_body, ...)
```

注意 RPC 版用 **`DPI=150`**（[rpc_doclayout8.py:31](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/docvision/rpc_doclayout8.py#L31)），比本地的 72 高一倍多，传给服务端的图更清晰。但服务端回传的框是「150 DPI 像素」坐标，客户端必须缩放回点坐标：

[rpc_doclayout8.py:317-357](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/docvision/rpc_doclayout8.py#L317-L357)：

```python
scale = 72 / DPI                          # = 0.48，把 150dpi 像素缩回点
for item in boxes_payload:
    coords = [float(value) * scale for value in box]   # 缩放
    boxes.append(YoloBox(None, np.array(coords), ...))
```

缩放后的坐标仍是「图像空间、左上原点」，于是回到 `LayoutParser.process` 走和本地版**完全相同**的 y 翻转逻辑。这就是为什么 `LayoutParser` 不必关心 model 是哪种实现——两种实现都交付「图像空间、点单位」的框。

**滑动窗口并发**（[rpc_doclayout8.py:127-186](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/docvision/rpc_doclayout8.py#L127-L186)）：用 `fill_window()` 控制在飞请求数 `max_in_flight = layout_request_max_workers + buffer_limit`（默认 8+2），用 `next_yield` 指针保证按页序 yield，乱序完成的请求先存进 `completed` 字典等轮到自己。这样既并发提速、又保持顺序。

#### 4.5.4 代码实践

**实践目标**：理解本地与 RPC 两条路径的差异，不强制真实启动服务（启动服务属 u8-l4 范围）。

**操作步骤**（**源码阅读型实践**）：

1. 阅读 [main.py:543-575](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L543-L575)，确认「无 `--rpc-doclayout*` 时默认 `load_onnx()`」。
2. 对照 [rpc_doclayout8.py:31](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/docvision/rpc_doclayout8.py#L31)（`DPI=150`）与 [doclayout.py:257](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/docvision/doclayout.py#L257)（默认 `dpi=72`），写下两者 DPI 差异。
3. 解释 `scale = 72 / DPI`（[rpc_doclayout8.py:331](https://github.com/funstory-ai-BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/docvision/rpc_doclayout8.py#L331)）的作用。

**需要观察的现象**：RPC 版多了「150 DPI 渲染 → base64 → POST → 回传框 → ×(72/150) 缩放」一整套，而本地版 72 DPI 直出。

**预期结果**：你能用自己的话说清——两者最终都向 `LayoutParser` 交付「图像空间、点单位」的框，差异仅在「谁来跑模型、用什么 DPI」。

**待本地验证**：若你有可用的版面 RPC 服务，可用 `--rpc-doclayout8 http://host:port` 跑一次，对比生成的 `layout_generator.json` 与本地版是否近似（坐标体系一致，框集合因 DPI/模型版本可能略有差异）。

#### 4.5.5 小练习与答案

**练习 1**：为什么 RPC 版用 150 DPI 而本地版用 72 DPI？

**答案**：本地版 72 DPI 是为了「像素=点」省去缩放、且降低本地 CPU 负担；RPC 版把推理外包到服务端（通常有更强算力/GPU），用更高 DPI 换更清晰的检测精度，代价是回传框要按 `72/DPI` 缩回点坐标。

**练习 2**：`LayoutParser` 代码里没有任何 `if isinstance(self.model, RpcDocLayoutModel)` 分支，它是怎么做到对两种后端透明的？

**答案**：依赖抽象基类 `DocLayoutModel` 的统一接口 `handle_document`。两种实现都按「逐页 yield (page, YoloResult)、框为图像空间点坐标」的契约交付，`LayoutParser` 只消费这个契约，不关心实现细节——这是面向对象的依赖倒置。

---

## 5. 综合实践

**任务**：把本讲的四个模块串起来，手工还原 `Parse Page Layout` 这个 stage 的完整数据流。

1. **准备**：用 `babeldoc --warmup` 确保 ONNX 模型已下载。
2. **写一个端到端探针脚本**（**示例代码**），对 `examples/ci/test.pdf` 第 0 页完成：
   - (a) `DocLayoutModel.load_onnx()` 加载模型；
   - (b) `get_no_rotation_img` 渲染图，`model.predict` 得到 `YoloResult`；
   - (c) 打印每个框的 `(names[cls], conf, 图像空间xyxy)`；
   - (d) 按 4.3 节公式手算 IL 空间坐标；
   - (e) 调 `convert_page_to_char_boxes` + `process_page_chars_to_lines`，打印 fallback 行数与其包围盒。
3. **对照真实输出**：用 `--debug` 跑真实翻译，打开 `layout_generator.json`，验证你手算的模型框坐标、fallback 行数量与官方落盘是否一致。
4. **思考题**：如果你把 `handle_document` 的渲染 DPI 改成 144，(d) 的坐标映射会怎样失真？应在哪里加 `scale` 修正（提示：参考 RPC 版的 `scale = 72 / DPI`）？

> 通过这个任务，你会同时摸到「模型推理 → 坐标映射 → 几何兜底 → 落盘校验」整条链路，理解 `LayoutParser` 为何能成为下游一切字符归属判断的「底图」。

## 6. 本讲小结

- `Parse Page Layout` 是 midend 第二个 stage，权重 `14.03`，紧跟在 `DetectScannedFile` 之后；它产出的 `page.page_layout` 是 `ParagraphFinder`/`StylesAndFormulas`/`TableParser`/`Typesetting` 的共同底图。
- BabelDOC 复用 **DocLayout-YOLO ONNX** 模型（`doclayout_yolo_docstructbench_imgsz1024.onnx`），类别名从 ONNX 元数据 `names` 读取，输入固定 1024、置信度阈值 0.25。
- `OnnxModel.handle_document` 用 **72 DPI** 去旋转渲染每页，逐页 `yield (page, YoloResult)`，让 `LayoutParser` 边出结果边报进度。
- 核心坐标映射：72 DPI 下「像素=点」，故**只翻 y 轴**（`新y = h − 旧y`）并外扩 1 像素容差，无需缩放；结果写入 IL 的 `PageLayout`。
- **`fallback_line`** 用字符几何 + DBSCAN 聚行，对模型漏检做行级兜底，`conf=1`、追加而非覆盖模型版面。
- **本地 ONNX 与 RPC 版面服务**通过抽象基类 `DocLayoutModel` 互换；RPC 版用 150 DPI 并以 `scale = 72 / DPI` 把回传框缩回点坐标，对 `LayoutParser` 完全透明。

## 7. 下一步学习建议

- **u5-l3 段落识别（ParagraphFinder）**：直接消费本讲产出的 `page_layout`，用 `layout_helper` 的 `is_text_layout`/`layout_priority` 判定字符归属并聚段，是本讲最自然的下游。
- **u5-l4 公式与样式（StylesAndFormulas）**：会用到本讲的 `formula`/`isolate_formula` 版面框与 `figure_table_layouts` 分类。
- **u5-l5 表格解析（TableParser）**：在版面识别为 `table` 的区域里检测单元格结构。
- **延伸阅读**：若对推理部署感兴趣，可读 [babeldoc/tools/executor](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/tools/executor) 下的 RPC 服务实现（u8-l4 会专讲），看 `rpc_doclayout8` 的服务端是如何把 ONNX 包成 `/inference` 端点的。
