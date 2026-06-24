# 资源管理：warmup 与离线资源包

> 本讲属于「工程化与扩展」单元（u8），承接 u1-l2（安装与运行）。
> 在阅读本讲前，你应该已经能用 `babeldoc` 命令翻译过至少一个 PDF，并知道 `babeldoc` 是一个「被嵌入的库」。

---

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 BabelDOC 翻译一个 PDF 到底需要从外部获取哪些资源（字体、模型、CMap、tiktoken），它们各自服务于流水线的哪个环节。
- 看懂「资源清单 + SHA3-256 校验」这一套自校验下载机制，并能在源码里定位校验逻辑。
- 用 `babeldoc --warmup` 预热缓存，并理解它为什么不依赖 API key、为什么能在「装好即用」之前先验证安装。
- 用 `--generate-offline-assets` / `--restore-offline-assets` 在内网/离线环境里分发资源，并解释离线包文件名里的那串哈希为什么不能改。
- 理解「多上游（GitHub / HuggingFace / hf-mirror / ModelScope）择优下载」的设计：为什么要赛跑、为什么 GitHub 不存模型。

本讲只讲「资源怎么来、怎么校验、怎么缓存、怎么离线分发」，**不**讲资源被下游怎么使用（字体的 `has_char`、模型的 `handle_document`、CMap 的解析等，分别见 u7-l2、u5-l2、u4-l4）。

---

## 2. 前置知识

在进入源码前，先用大白话建立四个直觉。

**① BabelDOC 不是「一个二进制就能跑」的程序。** 它在翻译时会临时用到几类「重资源」：

| 资源类别 | 举例 | 服务于流水线哪个环节 |
| --- | --- | --- |
| 字体（fonts） | `SourceHanSerifCN-Regular.ttf`、`NotoSans-Regular.ttf` | backend 渲染译文（u7-l2/u7-l3） |
| 模型（models） | `doclayout_yolo_docstructbench_imgsz1024.onnx` | midend 版面分析（u5-l2） |
| CMap（cmap） | `UniGB-UTF8-H.json` 等 | frontend 解析 CID 字体编码（u4-l4） |
| tiktoken 缓存 | `fb374d419588a4632f3f557e76b4b70aebbca790` | midend 翻译/术语批处理的 token 计费（u6-l2/u6-l4） |

这些资源加起来上百 MB（光字体就有几十个），不可能全塞进 pip 包里。所以 BabelDOC 的策略是：**包里只放「清单和校验码」，真正的文件按需下载到本地缓存目录。**

**② SHA3-256 是「数字指纹」。** 对任意一个文件算一个固定长度的哈希值，只要文件改了一个字节，哈希值就完全不同。BabelDOC 把每个资源文件「正确的哈希值」写死在源码里，下载后再本地算一遍比对——对不上就当作损坏、删掉重下。这样即使网络抖动、CDN 缓存了坏文件，也能自动自愈。

**③ 「上游」就是「去哪里下载」。** 同样的字体/模型，BabelDOC 配了多个镜像（GitHub、HuggingFace、hf-mirror、ModelScope）。不同地区、不同网络环境下，哪个镜像最快是不一样的，所以它会让所有镜像「赛跑」，谁先返回就用谁。

**④ 异步函数 vs 同步调用。** BabelDOC 的资产下载函数都是 `async` 的（用 `httpx.AsyncClient` 并发下载），但 CLI 和翻译主链路是**同步**的。这两者之间靠一个「在新线程里跑事件循环」的小桥接连起来。本讲会反复看到这个桥接。

> 名词速查：
> - **缓存目录（cache folder）**：所有资源最终落在 `~/.cache/babeldoc/`。
> - **清单（manifest）**：写死在源码里的「文件名 → 哈希值」对照表。
> - **上游（upstream）**：下载源镜像。
> - **离线包（offline assets package）**：把所有资源打成的一个 zip，用于无网环境。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `babeldoc/const.py` | 定义缓存目录 `CACHE_FOLDER`、`get_cache_file_path`，以及 tiktoken 缓存目录的副作用初始化。 |
| `babeldoc/assets/embedding_assets_metadata.py` | **资源清单**：字体的嵌入元数据 `EMBEDDING_FONT_METADATA`、CMap 元数据 `CMAP_METADATA`、tiktoken 哈希 `TIKTOKEN_CACHES`、模型哈希、各上游 URL 模板，以及字体族映射。 |
| `babeldoc/assets/assets.py` | **资源管理主逻辑**：`verify_file` 校验、`download_file` 下载、多上游择优、warmup、离线包生成/恢复，以及 async↔sync 桥接。 |
| `babeldoc/main.py` | CLI 入口，把 `--warmup` / `--generate-offline-assets` / `--restore-offline-assets` 三个参数接到上面的函数上。 |

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块，按「资源有什么 → 怎么下载 → 怎么一次下全 → 怎么离线搬运」的认知顺序展开：

- 4.1 资源清单与校验（manifest + SHA3-256）
- 4.2 上游择优下载（multi-upstream race + download + async↔sync 桥接）
- 4.3 warmup 预热（一次性下载所有资源）
- 4.4 离线包生成与恢复（air-gapped 部署）

---

### 4.1 资源清单与校验

#### 4.1.1 概念说明

翻译一个 PDF 需要四类外部资源（见第 2 节表格）。BabelDOC 不把这些二进制塞进 pip 包，而是随包**内置它们的清单**：每个文件的名字、期望的 SHA3-256、以及（对字体而言）排版需要的度量信息。运行时按清单去下载，下完再算一次哈希比对，确认无损。

这套机制带来三个好处：

1. **包体小**：pip 包只携带清单，不携带几百 MB 二进制。
2. **自校验自愈**：下载损坏能被发现并自动重下。
3. **离线友好**：清单本身就在包里，离线环境下只要有文件就能校验，不依赖网络清单服务。

#### 4.1.2 核心流程

资源校验的执行过程可以概括为：

1. 根据文件名 + 类别，在缓存目录里算出目标路径（`get_cache_file_path`）。
2. 调 `verify_file(路径, 期望哈希)`：流式读取文件（每次 1MB），边读边更新 SHA3-256，最后与期望哈希比对。
3. 文件不存在或哈希不符 → 视为「缺失/损坏」→ 触发下载。

#### 4.1.3 源码精读

先看缓存目录的根。所有资源最终都落在用户主目录下的 `.cache/babeldoc`：

[const.py:11](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/const.py#L11) —— `CACHE_FOLDER = Path.home() / ".cache" / "babeldoc"`，是所有资源的根目录。

`get_cache_file_path` 把「文件名 + 类别」映射成具体路径，并**按需创建子目录**（`fonts/`、`models/`、`cmap/`、`tiktoken/`、`assets/`）：

[const.py:14-20](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/const.py#L14-L20) —— 给定 `filename` 与 `sub_folder`，返回 `CACHE_FOLDER/sub_folder/filename`，子目录不存在则创建。

> 小贴士：正因为这里会 `mkdir`，`--warmup` 跑完之后你能在 `~/.cache/babeldoc/` 下看到按类别分好的子目录——这是第 4.3 节实践的观察点。

校验函数 `verify_file` 是整套机制的基石：

[assets.py:89-99](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/assets/assets.py#L89-L99) —— 文件不存在直接返回 `False`；否则用 `hashlib.sha3_256()` 流式（1MB/块）算哈希，与传入的 `sha3_256` 比对，返回是否一致。

注意它**不抛异常**，只返回布尔值——这样调用方可以很自然地写成 `if verify_file(...): 直接用 else: 去下载`。

再看清单本身。最典型的「嵌入元数据」是字体清单 `EMBEDDING_FONT_METADATA`，每个字体条目里既有校验信息，也有排版要用的度量：

[embedding_assets_metadata.py:261-273](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/assets/embedding_assets_metadata.py#L261-L273) —— 以 `NotoSerif-Regular.ttf` 为例，`sha3_256` 用于下载校验，`ascent/descent/encoding_length` 是字体度量（u7-l2 排版量宽要用），`serif/bold/italic/monospace` 是字体族标签，`size` 是字节数。

其余三类资源的清单结构类似但更精简：

- CMap 清单 `CMAP_METADATA`：每个条目只有 `file_name` / `sha3_256` / `size`，例如 [embedding_assets_metadata.py:1062-1071](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/assets/embedding_assets_metadata.py#L1062-L1071)（`UniGB-UCS2-H/V.json`，CJK 字体最常用的 CMap）。
- tiktoken 清单 `TIKTOKEN_CACHES`：键是 tiktoken 的 blob 文件名（`fb374d419588a4632f3f557e76b4b70aebbca790`），值是它的 SHA3-256：[embedding_assets_metadata.py:7-9](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/assets/embedding_assets_metadata.py#L7-L9)。
- 模型哈希 `DOCLAYOUT_YOLO_DOCSTRUCTBENCH_IMGSZ1024ONNX_SHA3_256`：[embedding_assets_metadata.py:3-5](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/assets/embedding_assets_metadata.py#L3-L5)。

> **两个 import-time 的副作用（重要但隐蔽）。** 这个模块在被 import 时会自动执行两段清理逻辑：
> - [embedding_assets_metadata.py:1385-1397](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/assets/embedding_assets_metadata.py#L1385-L1397) `__add_fallback_to_font_family`：把其他语言的字体也追加进当前语言的字体族，实现「跨语言兜底」（中文 PDF 里混入日文假名也能渲染）。
> - [embedding_assets_metadata.py:1400-1410](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/assets/embedding_assets_metadata.py#L1400-L1410) `__cleanup_unused_font_metadata`：删掉任何字体族都没引用的字体元数据。
>
> 也就是说，源码里静态看到的 `EMBEDDING_FONT_METADATA` 条目，在程序真正运行时可能已被裁剪过。`generate_all_assets_file_list()`（4.4 节）遍历的就是**裁剪后**的字典，所以离线包里只会有真正用得到的字体。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `verify_file` 的「自愈」行为。

**操作步骤**（这是一个源码阅读 + 本地验证型实践）：

1. 写一段最小脚本（**示例代码**，非项目原有代码）：

   ```python
   # verify_demo.py
   from pathlib import Path
   from babeldoc.assets.assets import verify_file
   from babeldoc.const import get_cache_file_path

   # 取一个已知字体（若未下载，可换成本地任意小文件做实验）
   p: Path = get_cache_file_path("NotoSans-Regular.ttf", "fonts")
   print("path =", p)
   print("exists before =", p.exists())

   # 故意造一个「损坏」的占位文件，用一个错误的哈希去校验
   if not p.exists():
       p.write_bytes(b"corrupted content")
   print("verify with WRONG hash =", verify_file(p, "0" * 64))
   ```

2. 运行 `python verify_demo.py`。

**需要观察的现象**：

- `get_cache_file_path` 即使缓存里没有该文件，也会先把 `fonts/` 子目录建出来。
- 用一个全 `0` 的错误哈希去校验时，`verify_file` 返回 `False`（而不是抛异常）。

**预期结果**：脚本能正常打印路径、目录被创建、错误哈希返回 `False`。

**待本地验证**：若你机器上恰好有正确的 `NotoSans-Regular.ttf`，可用 `babeldoc.assets.embedding_assets_metadata.EMBEDDING_FONT_METADATA["NotoSans-Regular.ttf"]["sha3_256"]` 取正确哈希再校验一次，应得到 `True`。

#### 4.1.5 小练习与答案

**练习 1**：`verify_file` 为什么要「每次只读 1MB」而不是一次性 `f.read()`？
**参考答案**：字体和 ONNX 模型可能几十甚至上百 MB，一次性读进内存会占用峰值内存；分块流式读取能把内存占用压在 1MB 量级，对大文件更友好，也让校验在低内存机器上可行。

**练习 2**：为什么 `EMBEDDING_FONT_METADATA` 里除了 `sha3_256` 还要存 `ascent/descent/serif` 这些字段？
**参考答案**：因为排版（u7-l1/u7-l2）需要字体的度量（ascent/descent 算行高、`char_lengths` 量宽）和字体族标签（serif/sans、bold/italic）来选字体和换行。把这些一起缓存，是为了避免运行时再回头解析字体文件——下载一次，度量与校验码一并就位。

---

### 4.2 上游择优下载

#### 4.2.1 概念说明

有了清单和校验，下一步是「文件不在缓存里时，去哪下、怎么下」。BabelDOC 给同一批资源配置了多个镜像源（上游），并在运行时让它们**并发赛跑**，谁先成功返回就用谁，其余取消。这样在不同地区（GitHub 在部分地区访问慢或被墙；HuggingFace、hf-mirror、ModelScope 是替代镜像）都能拿到可用的下载源。

这里有一个**关键的约束**：**GitHub 这个上游只存字体，不存模型**。所以「最快字体上游」和「最快模型上游」可能不是同一个，需要分别选取。

#### 4.2.2 核心流程

择优下载的整体流程：

1. **并发赛跑**：对所有候选上游同时发起 `get_font_metadata`（拉取一个很小的 `font_metadata.json`），用 `asyncio.as_completed` 取**最先返回**的那个，立刻取消其余任务。
2. **缓存赛跑结果**：把「哪个上游最快」记到模块级全局变量，后续下载直接复用，避免每次都赛跑。
3. **选模型上游**：如果最快字体上游是 GitHub，则另外在「排除 GitHub」的上游里再赛跑一次，选出模型上游。
4. **真正下载**：用选定的上游拼出 URL，`download_file` 下载；下完用 `verify_file` 校验，损坏则删除并抛 `ValueError`，触发 tenacity 重试。
5. **桥接到同步**：上面这些都是 `async`，外部同步调用经 `run_coro` 在新线程里跑。

#### 4.2.3 源码精读

先看上游清单与 URL 模板。四个上游分别对应不同的镜像：

[embedding_assets_metadata.py:11-16](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/assets/embedding_assets_metadata.py#L11-L16) —— `FONT_METADATA_URL`：四个上游各自的 `font_metadata.json` 地址。注意 `hf-mirror` 那一行被注释掉了（当前默认不开），实际参与赛跑的是 `github / huggingface / modelscope` 三个。

> 注意 `FONT_URL_BY_UPSTREAM` / `CMAP_URL_BY_UPSTREAM` 是「按上游 + 文件名拼 URL」的 lambda 表：[embedding_assets_metadata.py:18-23](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/assets/embedding_assets_metadata.py#L18-L23)。而**模型 URL 表 `DOC_LAYOUT_ONNX_MODEL_URL` 里没有 `github`**：[embedding_assets_metadata.py:32-36](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/assets/embedding_assets_metadata.py#L32-L36) —— 这就是「GitHub 不存模型」的源码体现。

赛跑的核心是 `get_fastest_upstream_for_font`。它先查缓存，没缓存才真正赛跑，并用 `asyncio.Lock` 保证「只赛跑一次」：

[assets.py:182-207](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/assets/assets.py#L182-L207) —— `exclude_upstream=None` 且已有缓存结果时直接返回；否则进入锁，再次检查缓存（双重检查），仍未命中才调 `_get_fastest_upstream_for_font_internal` 真正赛跑，并把胜出者写进全局 `_FASTEST_FONT_UPSTREAM` / `_FASTEST_FONT_METADATA`。

真正的「赛跑」在内部函数里，用 `as_completed` 拿最先成功的：

[assets.py:160-179](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/assets/assets.py#L160-L179) —— 对每个上游创建一个 `get_font_metadata` 任务，`asyncio.as_completed` 一旦有一个成功就 `task.cancel()` 掉其余任务并返回；全都失败则返回 `(None, None)`。

> 这里有个**容易被忽略的细节**：`exclude_upstream` 不为 `None` 时（模型下载会用到），函数**既不读也不写缓存**（[assets.py:194-196](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/assets/assets.py#L194-L196)）。因为「排除某上游的最快」是临时需求，不应该污染「全局最快」缓存。

「GitHub 不存模型」在两个地方被处理：

[assets.py:210-211](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/assets/assets.py#L210-L211) —— `get_fastest_upstream_for_model` 直接复用字体赛跑逻辑，但 `exclude_upstream=["github"]`，即模型只在 HuggingFace/hf-mirror/ModelScope 里选。

[assets.py:214-232](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/assets/assets.py#L214-L232) —— `get_fastest_upstream` 统一对外：先选最快字体上游；若它是 `github`，再单独选一个模型上游，否则模型上游就等于字体上游。

下载本身带 tenacity 重试和「下载后必校验」：

[assets.py:102-128](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/assets/assets.py#L102-L128) —— `@retry` 最多 3 次、指数退避（1~15s）；只对网络类异常（`httpx.HTTPError`/`ConnectionError`/`ValueError`/`TimeoutError`）重试，且不重试 `CancelledError`。下载后 `verify_file`，校验失败则 `path.unlink(missing_ok=True)` 删除并抛 `ValueError("File ... is corrupted")`——这个 `ValueError` 正好命中重试条件，所以「下到坏文件」会自动重下。

> 注意 [assets.py:71-86](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/assets/assets.py#L71-L86) `_retry_if_not_cancelled_and_failed`：它是 `@retry` 的 `retry=` 谓词，精确控制「什么时候才值得重试」。

最后是 async↔sync 桥接。CLI 和翻译主链路是同步的，但资产函数全是 `async`，于是用一个「新线程 + 独立事件循环」来跑协程：

[assets.py:46-68](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/assets/assets.py#L46-L68) —— `run_in_another_thread` 起一个线程，在线程内 `asyncio.run(coro)` 跑协程，把结果或异常通过 `ResultContainer` 带回主线程。`run_coro` 是它的别名。本模块里每个同步包装（`warmup`、`get_font_and_metadata`、`get_cmap_file_path` 等）都走这条路。

> 为什么不在主线程复用一个事件循环？因为 BabelDOC 主链路可能在已有（或没有）事件循环的上下文里被调用，最稳妥的做法是给资产下载单独起一个线程和循环，避免 `asyncio.run` 不能嵌套的麻烦。代价是每次调用都新建一个线程——对「下完就缓存」的资产场景，这个开销可以接受。

#### 4.2.4 代码实践

**实践目标**：在源码层面走通「择优 → 下载 → 校验」的链路，并理解「下载坏文件会自动重下」。

**操作步骤**：

1. 在 `assets.py` 里追踪 `get_doclayout_onnx_model_path_async`（这是模型下载入口，逻辑最短）：

   [assets.py:235-254](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/assets/assets.py#L235-L254) —— 算出 `models/...onnx` 路径 → 已校验通过就直接返回 → 否则 `get_fastest_upstream_for_model`（排除 GitHub）→ 取 URL → `download_file`（带校验与重试）。

2. 列出这条链路上调用的关键函数顺序，标注每步所在的源码行。

**需要观察的现象**：模型下载走的是「排除 GitHub」的择优，而字体/CMap 下载走的是「不排除」的择优。

**预期结果**：调用链为
`get_doclayout_onnx_model_path_async` →（校验失败时）`get_fastest_upstream_for_model` → `get_fastest_upstream_for_font(exclude=["github"])` → `_get_fastest_upstream_for_font_internal` → `get_font_metadata`（赛跑）→ `download_file` → `verify_file`。

**待本地验证**：若你在断网或配了代理的环境下运行，可观察日志里 `Download file failed, retrying in ... seconds... (Attempt N/3)` 的退避重试输出（来自 [assets.py:106-109](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/assets/assets.py#L106-L109) 的 `before_sleep`）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `get_fastest_upstream_for_font` 用 `asyncio.as_completed` 而不是 `asyncio.gather`？
**参考答案**：`gather` 会等**所有**任务完成（或第一个异常），白白等慢的上游；`as_completed` 一旦有任务成功就立即返回并 `cancel()` 其余任务，真正做到「谁快用谁」，把延迟降到≈最快上游的延迟。

**练习 2**：`get_fastest_upstream_for_model` 为什么要 `exclude_upstream=["github"]`？
**参考答案**：因为 GitHub 镜像只存字体、不存 ONNX 模型（见 `DOC_LAYOUT_ONNX_MODEL_URL` 无 `github` 键）。若不排除，模型下载时会拼出一个 GitHub 上不存在的 URL 而失败。

**练习 3**：下载到一个「哈希对不上」的坏文件，BabelDOC 会怎么处理？
**参考答案**：`download_file` 里 `verify_file` 失败 → `path.unlink` 删除坏文件 → 抛 `ValueError("... is corrupted")` → 该异常命中 `@retry` 谓词 → 退避后重新下载，最多重试 3 次。

---

### 4.3 warmup 预热

#### 4.3.1 概念说明

`warmup`（预热）= 「在真正翻译之前，先把所有需要的资源一次性下到缓存里、并校验通过」。它的价值有两点：

1. **验证安装**：第一次用 `babeldoc`，不需要 API key，先 `--warmup` 确认网络、镜像、校验链路都通。
2. **消除首次翻译的「冷启动」**：否则你第一次翻译时，会卡在「边解析边等字体/模型下载」上，体验很差且容易超时。

#### 4.3.2 核心流程

`async_warmup` 做四件事：

1. 预热 tiktoken：`encoding_for_model("gpt-4o")` 触发 tiktoken 加载/下载它的 BPE 词表（走 `TIKTOKEN_CACHE_DIR`，见 const.py 副作用）。
2. 并发下载 ONNX 模型。
3. 并发下载全部字体。
4. 并发下载全部 CMap。

后三件用 `asyncio.gather` 同时进行，三者共享同一个 `httpx.AsyncClient` 和同一个「最快上游」缓存。

#### 4.3.3 源码精读

`async_warmup` 是预热的总入口：

[assets.py:449-458](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/assets/assets.py#L449-L458) —— 先 `encoding_for_model("gpt-4o")` 预热 tiktoken（这一步会用到 const.py 里设置的 `TIKTOKEN_CACHE_DIR`）；再开一个 `httpx.AsyncClient`，把模型、字体、CMap 三个下载任务 `asyncio.gather` 并发跑。

同步包装只有一行：

[assets.py:461-462](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/assets/assets.py#L461-L462) —— `warmup()` 用 `run_coro` 把协程塞进新线程执行。

`download_all_fonts_async` 体现了一个优雅的「全量已就绪则早退」模式：

[assets.py:396-421](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/assets/assets.py#L396-L421) —— 用 `for...else`：遍历所有字体，**任何一个**校验不过就 `break`；如果全部通过（没 break），`else` 分支直接 `return`（无需再择优、再下载）。否则才去 `get_fastest_upstream_for_font` 拿一次最快的上游，然后 `gather` 并发下载每个缺失字体。

> 这个 `for...else` 是「全部 OK 就跳过整个择优」的关键——第二次 `--warmup` 会非常快，因为根本不会发起任何网络请求。CMap 的全量下载 `download_all_cmaps_async` 是同样套路：[assets.py:424-446](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/assets/assets.py#L424-L446)。

CLI 端，`--warmup` 是个「快捷模式」，被安排在生成/恢复离线包**之后**、翻译服务校验**之前**，所以它不需要 `--openai` 也不需要 API key：

[main.py:57-61](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L57-L61) —— 参数定义，`action="store_true"`，说明是「只下载并校验所需资源后退出」。

[main.py:482-485](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L482-L485) —— 运行期：调 `warmup()` 后打印完成并 `return`，**不会**走到下面「必须选择翻译服务」的校验（[main.py:488](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L488)）。这就是为什么 `babeldoc --warmup` 不用 key。

tiktoken 缓存目录在 `const.py` 被 import 时就准备好了（这是 u1-l4 提到的「import 时副作用」）：

[const.py:42-44](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/const.py#L42-L44) —— 创建 `~/.cache/babeldoc/tiktoken` 并写入环境变量 `TIKTOKEN_CACHE_DIR`，让 tiktoken 知道去哪找/存词表，从而离线也能加载。

#### 4.3.4 代码实践

**实践目标**：跑一次 `--warmup`，亲眼看到缓存目录被填满。

**操作步骤**：

1. 先清空缓存（可选，便于观察）：
   ```bash
   rm -rf ~/.cache/babeldoc
   ```
2. 运行预热：
   ```bash
   babeldoc --warmup
   ```
3. 查看缓存结构：
   ```bash
   ls -la ~/.cache/babeldoc
   ls ~/.cache/babeldoc/fonts   | head    # 字体
   ls ~/.cache/babeldoc/models            # 模型（应有 1 个 .onnx）
   ls ~/.cache/babeldoc/cmap    | head    # CMap（应有上百个 .json）
   ls ~/.cache/babeldoc/tiktoken          # tiktoken 词表
   ```

**需要观察的现象**：

- 终端日志会打印类似 `Fastest font upstream determined: ...`、`Downloading fonts from ...`、`Download doclayout onnx model from ... success`。
- 缓存目录下出现 `fonts/`、`models/`、`cmap/`、`tiktoken/` 四个子目录。
- **再次**运行 `babeldoc --warmup`，日志几乎瞬间结束（因为 `download_all_fonts_async` 的 `for...else` 早退），证明「全量校验通过就不发网络请求」。

**预期结果**：第一次耗时取决于网速（模型约几十 MB，字体合计上百 MB）；第二次极快。

**待本地验证**：不同地区「最快上游」可能不同；若你处于 GitHub 访问受限的网络，应看到 `huggingface` 或 `modelscope` 胜出，模型下载尤其不会走 GitHub。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `babeldoc --warmup` 不需要提供 `--openai-api-key`？
**参考答案**：因为 `main()` 里 `--warmup` 分支在「翻译服务校验」（`if not args.openai: parser.error(...)`）**之前**就 `return` 了，根本走不到需要 key 的代码。

**练习 2**：第二次运行 `--warmup` 为什么会快很多？
**参考答案**：`download_all_fonts_async` / `download_all_cmaps_async` 用 `for...else` 检测到「所有文件都通过 `verify_file`」就直接 `return`，不发起任何下载，也不做上游择优。

**练习 3**：`async_warmup` 里为什么要单独 `encoding_for_model("gpt-4o")`？
**参考答案**：tiktoken 在第一次加载某编码时需要它的 BPE 词表；提前触发加载（并落盘到 `TIKTOKEN_CACHE_DIR`），是为了让后续翻译/术语抽取阶段的 token 计数（u6-l2/u6-l4）不再被首次加载阻塞。

---

### 4.4 离线包生成与恢复

#### 4.4.1 概念说明

很多企业/科研环境是**内网或气隙（air-gapped）**，目标机器根本连不上 GitHub/HuggingFace。BabelDOC 为此提供了一对命令：

- `--generate-offline-assets <目录>`：在**有网**的机器上，把所有资源（已经 warmup 过、校验过的）打成一个 zip。
- `--restore-offline-assets <zip 或目录>`：在**无网**的目标机器上，从 zip 里把资源还原进缓存目录。

这套机制有一个很有意思的设计：**离线包的文件名里编码了「文件清单的哈希」**，所以你不能随便改它的名字——改了就恢复不了。

#### 4.4.2 核心流程

生成与恢复都围绕一个「权威文件清单」展开：

1. `generate_all_assets_file_list()`：把 fonts/models/tiktoken/cmap 四类资源整理成 `{类别: [{name, sha3_256}, ...]}`。
2. `get_offline_assets_tag(file_list)`：对这份清单做 SHA3-256，得到一个 tag，作为 zip 文件名的一部分：`offline_assets_<tag>.zip`。
3. **生成**：先 warmup 确保齐全 → 逐个校验 → 压进 zip（路径形如 `fonts/<name>`、`models/<name>`）。
4. **恢复**：逐个检查缓存里是否已有且校验通过（已有就跳过），否则从 zip 解出 → 校验 → 落盘。若传入的是目录，自动按 tag 找对应 zip；tag 不匹配则拒绝。

#### 4.4.3 源码精读

权威清单的构造（这就是离线包「包含哪些文件」的唯一真相来源）：

[assets.py:465-498](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/assets/assets.py#L465-L498) —— `generate_all_assets_file_list` 初始化 `fonts/models/tiktoken/cmap` 四个空列表，分别从 `EMBEDDING_FONT_METADATA`、`CMAP_METADATA`、`TIKTOKEN_CACHES` 填入 `{name, sha3_256}`，模型则硬编码那一个 ONNX。**注意它遍历的是 import 时被 `__cleanup_unused_font_metadata` 裁剪后的字典**，所以离线包只含真正用得到的字体。

tag 的计算——这是「文件名不可改」的根源：

[assets.py:577-591](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/assets/assets.py#L577-L591) —— 把文件清单用 orjson 序列化（`OPT_SORT_KEYS` 保证键顺序稳定，`OPT_INDENT_2` + `OPT_APPEND_NEWLINE` 保证字节级稳定），再算 SHA3-256。也就是说，**tag = 当前这套清单内容的指纹**。

> 数学上：设清单序列化后的字节为 \(b\)，则
>
> \[
> \text{tag} = \mathrm{SHA3\text{-}256}(b)
> \]
>
> 只要清单里任何一项（增删一个字体、换一个哈希）变化，\(b\) 变，tag 就完全不同。文件名 `offline_assets_<tag>.zip` 因此自带「版本指纹」。

生成流程：先 warmup（保证齐全），再校验、压缩：

[assets.py:501-528](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/assets/assets.py#L501-L528) —— `generate_offline_assets_package_async` 先 `await async_warmup()`，再算 tag 定输出路径（`<dir>/offline_assets_<tag>.zip`），用 `zipfile.ZIP_DEFLATED` + `compresslevel=9` 高压缩写入；**写入前对每个文件再 `verify_file` 一次**，损坏则 `exit(1)`，绝不把坏文件打进包。

恢复流程：按清单逐个还原，已校验的跳过：

[assets.py:531-574](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/assets/assets.py#L531-L574) —— `restore_offline_assets_package_async`：若 `input_path` 是目录，自动拼 `offline_assets_<tag>.zip`（[assets.py:539-540](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/assets/assets.py#L539-L540)）；若直接给 zip，则**用正则从文件名解析出 tag，与本机算出的期望 tag 比对，不一致就 critical 退出**（[assets.py:545-554](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/assets/assets.py#L545-L554)）。然后遍历清单：缓存里已校验通过则 `continue`（`nothing_changed` 记账），否则从 zip 解出 → **解出后再校验一次**，损坏则提示「离线包损坏，请删除后重试」。

> 三个层次的校验形成了完整的完整性保证：① 打包前校验（生成端不夹带坏文件）；② 文件名 tag 校验（恢复端拒绝版本不符的包）；③ 解出后校验（恢复端拒绝 zip 内损坏）。这与 README 第 247-256 行的「Tip」一一对应：包名不可改（因为哈希编码在名字里）、所有资产在打包和恢复时都用 SHA3-256 校验。

CLI 的接线。这两个命令排在 `main()` 最前面，优先级甚至高于 `--warmup`，且各自执行后立即退出：

[main.py:90-99](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L90-L99) —— `--generate-offline-assets`（接收输出目录）与 `--restore-offline-assets`（接收 zip 或目录）的参数定义。

[main.py:468-480](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/main.py#L468-L480) —— 生成先于恢复、两者都先于 warmup；各自调 `generate_offline_assets_package(Path(...))` / `restore_offline_assets_package(Path(...))` 后 `return`，互斥且不进入翻译流程。

#### 4.4.4 代码实践

**实践目标**：在有网的机器上生成一个离线包，解压查看其内容结构，再在「假装无网」的情况下恢复它。

**操作步骤**：

1. 先预热，确保缓存齐全（上一节已完成可跳过）：
   ```bash
   babeldoc --warmup
   ```
2. 生成离线包到指定目录：
   ```bash
   babeldoc --generate-offline-assets /tmp/babeldoc-offline
   ls /tmp/babeldoc-offline      # 应看到 offline_assets_<一长串哈希>.zip
   ```
3. 解压查看内容结构：
   ```bash
   cd /tmp/babeldoc-offline && unzip -l offline_assets_*.zip | head -40
   unzip -l offline_assets_*.zip | awk '{print $4}' | awk -F/ '{print $1}' | sort | uniq -c
   ```
   观察顶层分类（`fonts/`、`models/`、`cmap/`、`tiktoken/`）以及各类的文件数量。
4. 模拟离线恢复：清空缓存后从包还原：
   ```bash
   rm -rf ~/.cache/babeldoc
   babeldoc --restore-offline-assets /tmp/babeldoc-offline   # 传目录，会自动找 zip
   ls ~/.cache/babeldoc/fonts | head
   ```

**需要观察的现象**：

- 生成命令的日志会先打印 `Downloading all assets...`（内部调 warmup），再打印 `Generating offline assets package...`，最后 `Offline assets package generated at ...`。
- 解压后顶层恰好四类目录，与 `generate_all_assets_file_list` 的四个键对应；`models/` 下只有 1 个 `.onnx`，`fonts/` 下是被字体族引用的那一批（注意 `LXGWWenKaiMonoTC` 等未引用字体不会出现，因为被 `__cleanup_unused_font_metadata` 删了）。
- 试着把 zip **改名**（如 `mv offline_assets_xxx.zip my-assets.zip`）后再 `--restore-offline-assets my-assets.zip`，会因 tag 不匹配而报 `Offline assets tag mismatch` 并退出。

**预期结果**：恢复后 `~/.cache/babeldoc/` 下的字体/模型/CMap 与直接 warmup 得到的一致；恢复日志若所有文件已存在会显示无变化，否则打印 `Offline assets package restored from ...`。

**待本地验证**：不同 BabelDOC 版本的清单不同，tag 也不同，跨版本混用离线包会被 tag 校验拦下——这正是设计意图（保证版本一致）。

#### 4.4.5 小练习与答案

**练习 1**：为什么离线包的文件名 `offline_assets_<tag>.zip` 不能改？
**参考答案**：`<tag>` 是「文件清单内容的 SHA3-256」。恢复时若直接传入 zip，代码会用正则从文件名里解析出 tag，与当前版本期望的 tag 比对，不一致就拒绝。改名会破坏这个解析与校验。

**练习 2**：`generate_offline_assets_package_async` 为什么开头要先 `await async_warmup()`？
**参考答案**：因为打包的素材必须先存在于本地缓存且校验通过。warmup 负责「把所有资源下全」，随后打包循环再逐个 `verify_file`，确保打进 zip 的每个文件都是完整无误的。

**练习 3**：恢复时，如果缓存里某个文件已经存在且校验通过，会发生什么？
**参考答案**：直接 `continue` 跳过，不解压、不覆盖（[assets.py:562-563](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/assets/assets.py#L562-L563)）。这样恢复是幂等的、增量的，重复恢复不会重写好文件。

---

## 5. 综合实践

**任务**：为你的团队搭建一套「内网 BabelDOC」分发流程，并验证其完整性。

**背景**：你的服务器在内网，无法访问 GitHub/HuggingFace。但你有一台能联网的开发机。

**要求**：

1. 在开发机上：`babeldoc --warmup` → `babeldoc --generate-offline-assets ./pkg`，得到一个 `offline_assets_<tag>.zip`。
2. 把 zip 拷到内网机器，用 `unzip -l` 统计其中四类资源的文件数量，并用本讲学到的知识解释：为什么 `fonts/` 下的字体数量比 `embedding_assets_metadata.py` 源码里静态看到的 `EMBEDDING_FONT_METADATA` 条目少？（提示：import 时的 `__cleanup_unused_font_metadata`。）
3. 在内网机器上：`rm -rf ~/.cache/babeldoc` 后 `babeldoc --restore-offline-assets ./pkg`，确认 `~/.cache/babeldoc/{fonts,models,cmap,tiktoken}` 都被还原。
4. 验证完整性：故意篡改 zip 里某个字体（比如用 `zip` 命令替换其中一个文件为坏数据），再 `--restore-offline-assets`，观察是否被「解出后校验」拦下并报 `Offline assets package is corrupted`。
5. 写一段总结，说明这套流程里**一共出现了几次 SHA3-256 校验**、分别在哪个环节（提示：生成端打包前、文件名 tag、恢复端解出后；外加 verify_file 本身）。

**预期结果**：能成功在内网还原全部资源；篡改后能被校验拦截；能说清校验发生在哪些环节。

**待本地验证**：步骤 4 的篡改与拦截行为，因涉及手动改 zip，建议在临时目录里做，避免污染正常缓存。

---

## 6. 本讲小结

- BabelDOC 翻译所需的四类外部资源——**字体 / ONNX 模型 / CMap / tiktoken**——随包只携带「清单 + SHA3-256」，二进制按需下载到 `~/.cache/babeldoc/`。
- **校验基石**是 `verify_file`：流式（1MB/块）算 SHA3-256 比对，文件缺失或损坏一律返回 `False`，触发重下；`download_file` 在此之上加了 tenacity 重试，对坏文件会自动删除并重试 3 次。
- **多上游择优**：`get_fastest_upstream_for_font` 用 `asyncio.as_completed` 让 GitHub/HuggingFace/ModelScope 赛跑，取最先返回者并缓存；**GitHub 只存字体不存模型**，所以模型下载单独 `exclude_upstream=["github"]`。
- **warmup** 用 `asyncio.gather` 一次下全模型/字体/CMap 并预热 tiktoken；`download_all_*` 的 `for...else` 让「全量已就绪」时零网络请求；CLI 的 `--warmup` 不需要 API key。
- **离线包**：`generate_all_assets_file_list` 是权威清单，其 SHA3-256 即文件名里的 tag（不可改）；生成端「打包前校验」、恢复端「tag 校验 + 解出后校验」三重把关，服务于气隙部署。
- 全模块的 `async` 函数经 `run_coro`（新线程 + 独立事件循环）桥接给同步的 CLI 与翻译主链路调用。

---

## 7. 下一步学习建议

- 想看资源**被怎么用**：字体见 u7-l2（`FontMapper` / `has_char`）、模型见 u5-l2（`OnnxModel.handle_document`）、CMap 见 u4-l4（CID 字体后端）、tiktoken 见 u6-l2/u6-l4（批处理 token 计费）。
- 想理解 CLI 参数如何汇聚成配置：回到 u1-l4（`TranslationConfig`），注意 `--warmup` 等快捷模式在 `main()` 里的短路顺序。
- 想继续「工程化与扩展」单元：下一篇 u8-l2 讲分片翻译与结果合并（`SplitManager` / `ResultMerger`），它们与本章的资源/缓存机制一起，决定了大规模 PDF 在生产环境的可部署性。
- 进阶阅读源码：对照 `babeldoc/assets/assets.py` 的 `__main__` 段（[assets.py:602-610](https://github.com/funstory-ai/BabelDOC/blob/980fd2821d54cbabd270349fe509e8177c35e4c3/babeldoc/assets/assets.py#L602-L610)），里面留有 `warmup()` / `generate_offline_assets_package()` 的手动调用注释，可作脚本化分发的起点。
