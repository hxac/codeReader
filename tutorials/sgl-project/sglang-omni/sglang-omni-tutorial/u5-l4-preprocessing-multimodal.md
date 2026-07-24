# 预处理与多模态输入

> 本讲承接 **u3-l1（Stage 抽象与 IO 外壳）**。u3-l1 讲清了「Stage 是个 IO 外壳，把所有计算都 dispatch 给 scheduler」。
> 本讲下钻到 **管线第一个阶段里真正干的活**——把用户在 HTTP 请求里塞进来的「一坨异构输入」
> （文件路径、HTTP 链接、base64、PIL 图像、numpy 数组……）规范化成「一段干净的、可被模型吃下去的管线状态」。
> 这套逻辑住在 `sglang_omni/preprocessing/` 子包里，是**模型无关**的：无论后面接 Qwen3-Omni、TTS 还是 ASR，
> 前面这层「归一化 + 取数 + 防越界 + 算缓存键」都是同一套代码。

## 1. 本讲目标

读完本讲，你应当能够：

1. 说出预处理层的**统一契约 `MediaIO`**——它如何把「bytes / base64 / 文件」三种来源收敛成同一个加载对象，以及 image / audio / video 三个模态各如何实现它。
2. 理解 `ensure_*_list_async` 这一族函数如何把「单个或多个、本地或远程、原始或已处理」的输入**并发地**归一成一个统一列表。
3. 读懂 `MultiModalResourceConnector` 的**访问控制**：它如何用 `allowed_local_media_path` 挡住任意本地文件读取、用域名白名单 + 不安全地址拒绝（SSRF 防护）挡住内网探测。
4. 解释 `cache_key` 为什么必须在「转换前」用**原始输入**计算，以及 `reference_path_cache_key` 如何在不读全文的前提下稳定地哈希一个参考音频文件。
5. 动手写一个 GPU-free 的 pytest，验证「路径越界」和「本地加载未开启」两种情况都被拒绝。

## 2. 前置知识

本讲默认你已经读过：

- **u3-l1**：Stage 是 IO 外壳，所有计算 dispatch 给 scheduler；预处理阶段就是一个**非 AR 阶段**，由 `SimpleScheduler`（见 u4-l1）驱动，它的 `compute_fn` 里调用的就是本讲的工具。
- **u2-l2**：HTTP 层把外部 `ChatCompletionRequest` 译成内部 `GenerateRequest`，并把 `images / audios / videos` 等**多模态引用塞进 `metadata`**——这些引用正是本讲的输入。
- **u5-l3**：TTS 接入里提到的「radix cache key 必须由 embedding 内容派生」「张量设备纪律」，与本章 `cache_key` 一脉相承。

先对齐几个本讲反复出现的术语：

- **模态（modality）**：输入的种类，本讲覆盖 image / audio / video / text 四种。
- **MediaIO**：媒体 I/O 的抽象基类，每种模态各有一个实现（`ImageMediaIO` / `AudioMediaIO` / `VideoMediaIO`）。
- **connector（连接器）**：`MultiModalResourceConnector`，负责「从某个 URL 取到字节流」，再把字节流交给 `MediaIO` 解码。
- **SSRF（Server-Side Request Forgery）**：服务端请求伪造。用户给一个 `http://169.254.169.254/...`（云元数据）或 `http://127.0.0.1:xxxx` 的链接，诱导服务器去访问内网。connector 里的「域名白名单 + 不安全地址拒绝」就是防这个。
- **缓存键（cache key）**：一段输入的「身份证」。参考音频编码这类昂贵产物会用它做 LRU 缓存的键（见 u6-l5），相同键直接复用、不同键绝不串台。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [sglang_omni/preprocessing/base.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/preprocessing/base.py) | 统一契约 `MediaIO`（抽象基类）+ `_is_url` 工具。全包最薄、最关键的一层。 |
| [sglang_omni/preprocessing/resource_connector.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/preprocessing/resource_connector.py) | 「取数 + 访问控制」中枢 `MultiModalResourceConnector`：HTTP/data/file 三协议、域名白名单、SSRF 防护、本地路径越界防护、连接池与线程池。 |
| [sglang_omni/preprocessing/cache_key.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/preprocessing/cache_key.py) | 从原始输入派生稳定缓存键：`hash_media_item` / `compute_media_cache_key` / `reference_path_cache_key`（带 stat 记忆化的文件哈希）。 |
| [sglang_omni/preprocessing/image.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/preprocessing/image.py) | 图像模态实现：`ImageMediaIO`、`ensure_image_list_async`、`compute_image_cache_key`。 |
| [sglang_omni/preprocessing/audio.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/preprocessing/audio.py) | 音频模态实现：`AudioMediaIO`（含 WAV 手写解析 + PyAV 回退 + 线性重采样）、`ensure_audio_list_async`。 |
| [sglang_omni/preprocessing/video.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/preprocessing/video.py) | 视频模态实现：`VideoMediaIO`（含可选音轨抽取）、`compute_video_cache_key`（把解码参数也并入键）。 |
| [sglang_omni/preprocessing/text.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/preprocessing/text.py) | 文本模态：chat template 加载、`normalize_messages`、`append_modality_placeholders`。 |
| [sglang_omni/models/qwen3_omni/components/preprocessor.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/components/preprocessor.py) | 真实模型预处理阶段：示范「先算 cache key、再并发 `ensure_*_list_async`」的正确用法。 |
| [sglang_omni/serve/speech_service.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/serve/speech_service.py) | 生产侧如何**带安全策略**构造 connector（开 `reject_unsafe_remote_addresses`）。 |
| [tests/unit_test/preprocessing/test_cache_key.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/tests/unit_test/preprocessing/test_cache_key.py) | `reference_path_cache_key` 的 GPU-free 单测：内容追踪、同尺寸改写、记忆化、`trust_stat` 快路径。 |

---

## 4. 核心概念与源码讲解

### 4.1 预处理基类 MediaIO：把「三种来源」收敛成一个对象

#### 4.1.1 概念说明

用户提交一段多媒体输入时，**来源形态五花八门**：

- 一段 `data:image/png;base64,iVBOR...` 的 data URL；
- 一个 `https://...` 的远程链接；
- 一个 `file:///data/voices/alice.wav` 的本地文件引用；
- 已经被上游解码好的 PIL 图像、numpy 数组、torch 张量。

但模型只关心**最终对象**（一张 RGB PIL 图、一个 float32 音频数组、一段 `(T,C,H,W)` 视频张量）。`MediaIO` 就是这层「来源多态 → 对象单一」的抽象：它规定每个模态必须会从 **bytes / base64 / file** 三种来源加载，至于「怎么从这三种来源里选一种」则交给 connector（见 4.3）。这样设计的好处是：**解码逻辑与取数逻辑彻底解耦**，换一个传输后端不影响解码，换一个解码器也不影响取数。

#### 4.1.2 核心流程

```
        bytes              base64 data URL        file path
         │                     │                     │
         ▼                     ▼                     ▼
   load_bytes(data)      load_base64(type,data)  load_file(path)
         └─────────────────────┴─────────────────────┘
                               │
                               ▼
                    模态专属的「已加载对象 _M」
              (PIL.Image / (np.ndarray, sr) / (Tensor, fps, audio))
```

`MediaIO` 是 `Generic[_M]`：`_M` 是该模态「加载后的对象类型」。image 用 `Image.Image`，audio 用 `tuple[np.ndarray, float]`，video 用 `tuple[Tensor, float, Any | None]`。三个 `@abstractmethod` 中 `load_http_bytes` 有默认实现（直接转调 `load_bytes`），子类按需覆盖以拿到 MIME 类型。

#### 4.1.3 源码精读

基类本身极薄，但它是全包的「宪法」：

[sglang_omni/preprocessing/base.py:25-49](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/preprocessing/base.py#L25-L49) 定义 `MediaIO` 抽象基类，声明 `load_bytes` / `load_http_bytes`（带默认实现，转调 `load_bytes`）/ `load_base64` / `load_file` 四个入口。

[sglang_omni/preprocessing/base.py:14-22](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/preprocessing/base.py#L14-L22) 是全包共享的 `_is_url`：用 `urlparse` 判断 scheme 是否属于 `http/https/data/file`，决定一个字符串「该当 URL 处理还是当本地路径处理」。

图像模态的实现是基类最直白的落地：

[sglang_omni/preprocessing/image.py:37-57](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/preprocessing/image.py#L37-L57) `ImageMediaIO` 的三个加载方法：`load_bytes` 从 `BytesIO` 打开并 `convert(image_mode)`；`load_base64` 先 `b64decode` 再复用 `load_bytes`；`load_file` 直接 `Image.open(filepath)`。三者都把 `UnidentifiedImageError` 包装成 `ValueError`，让上层统一捕获。

音频模态更复杂一点，但骨架相同：

[sglang_omni/preprocessing/audio.py:163-189](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/preprocessing/audio.py#L163-L189) `AudioMediaIO.load_bytes / load_file`：先试**手写的 WAV 解析** `_parse_wav_bytes`（无需外部依赖、支持 PCM/IEEE-float），失败再回退到 **PyAV** `_decode_audio_bytes_av`（支持 WebM/Opus/MP3/OGG/FLAC），最后 `_resample_linear` 到目标采样率。注意三个方法都返回 `(audio, target_sr)` 元组——这就是该模态的 `_M`。

#### 4.1.4 代码实践

**目标**：亲手实现一个最小 `MediaIO`，体会「三种来源同一个对象」。

**步骤**：

1. 阅读上面的 `ImageMediaIO` 与 `AudioMediaIO` 实现。
2. 在交互式 Python（或临时脚本）里写一个「大写文本」的 `MediaIO[str]`：

```python
# 示例代码：一个最小的 MediaIO 实现，仅用于理解契约
from sglang_omni.preprocessing.base import MediaIO

class UpperTextIO(MediaIO[str]):
    def load_bytes(self, data: bytes) -> str:
        return data.decode("utf-8").upper()
    def load_base64(self, media_type: str, data: str) -> str:
        import base64
        return self.load_bytes(base64.b64decode(data))
    def load_file(self, filepath) -> str:
        return self.load_bytes(open(filepath, "rb").read())

io = UpperTextIO()
print(io.load_bytes(b"hello"))          # HELLO
print(io.load_base64("text/plain", "aGk="))  # HI
```

**预期结果**：`load_bytes` / `load_base64` 两条路径都产出大写字符串，证明三种来源被同一个 `MediaIO` 收敛。

**需要观察的现象**：如果你漏写其中任意一个 `@abstractmethod`，Python 会在**实例化时**抛 `TypeError: Can't instantiate abstract class`——这正是「宪法」的强制力。

> 待本地验证：上述示例不依赖 GPU，可在任意装有 sglang_omni 的 venv 里直接跑。

#### 4.1.5 小练习与答案

**练习 1**：`load_http_bytes` 为什么有默认实现，而 `load_bytes` 是纯抽象？
**答案**：绝大多数模态不关心 HTTP 响应的 MIME 头，直接把 body 当 bytes 处理即可，所以默认实现就是 `return self.load_bytes(data)`；只有需要用 `media_type` 选不同解码分支的子类才覆盖它。把它设为抽象会强迫每个子类都写一遍无意义的转发。

**练习 2**：`AudioMediaIO.load_file` 为什么先试 `_parse_wav_bytes` 再回退 PyAV，而不是直接用 PyAV？
**答案**：手写 WAV 解析零外部依赖、更快、且对最常见格式（16/32-bit PCM、float WAV）足够；PyAV 作为兜底覆盖压缩格式（MP3/Opus 等）。先快后全，避免对简单文件引入不必要的解封装开销。

---

### 4.2 多模态规范化：ensure_*_list_async 与「并发取数」

#### 4.2.1 概念说明

真实请求里，输入很少是「单个、干净」的。一次 chat 可能同时给 2 张图、1 段音频、1 个视频，它们有的是本地路径、有的是 URL、有的已经被客户端解码成对象。`ensure_image_list_async` / `ensure_audio_list_async` / `ensure_video_list_async` 这族函数就是**归一化器**：吃进「任意可迭代或单个、来源混杂」的输入，吐出「统一列表，每个元素都是已加载对象」。关键设计有两点：**URL 项并发拉取**（`asyncio.gather`），以及**已处理对象原样透传**（PIL 图、numpy 数组直接进列表，不重复解码）。

#### 4.2.2 核心流程

```
输入: [path, http_url, PIL.Image, data_url, ...]
        │
        ▼  第一遍扫描：分类
   ┌────┴────────┬──────────────┬───────────────┐
   │本地路径(同步)│ URL(建 task)  │已是对象(透传)  │
   ▼             ▼              ▼
load_*_path    fetch_*_async   直接 append
   │             │
   │      asyncio.gather(*tasks)   ← 并发拉取 + 解码
   │             │
   └─────────────┴── 按原 idx 回填 normalized 列表
                       │
                       ▼
              统一的「已加载对象列表」
```

两遍式遍历是细节：第一遍记录每个 URL 项在列表里的**原始下标**和它对应的 task；`gather` 拿到结果后按下标回填，保证顺序不变。

#### 4.2.3 源码精读

[sglang_omni/preprocessing/image.py:69-126](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/preprocessing/image.py#L69-L126) `ensure_image_list_async` 的两遍扫描：第一遍对每个 `str/Path` 项判断 `_is_url`，是 URL 就 `create_task(fetch_image_async(...))` 并记 `url_indices`、占位 `None`；不是 URL 就同步 `load_image_path`；非字符串项（已是 PIL 图）直接透传。第二遍 `asyncio.gather` 并发等所有 URL，按下标回填。

[sglang_omni/preprocessing/image.py:90-94](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/preprocessing/image.py#L90-L94) 这里有个**防循环依赖**的常见手法：`media_connector` 默认 `None` 时才在函数内部 `from .resource_connector import get_global_resource_connector` 延迟导入，避免 `image.py` 与 `resource_connector.py` 互相 import 时崩溃。

真实模型预处理阶段（Qwen3-Omni）示范了「三个模态并发」的标准用法：

[sglang_omni/models/qwen3_omni/components/preprocessor.py:499-527](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/components/preprocessor.py#L499-L527) 先在 **499-502 行算三个 cache key**（见 4.4，必须在转换前算），再在 **514-527 行用一个 `asyncio.gather` 并发跑** `ensure_image_list_async` / `ensure_video_list_async` / `ensure_audio_list_async`——三个模态的取数与解码完全并行，互不阻塞。

#### 4.2.4 代码实践

**目标**：用 `ensure_image_list_async` 观察「混合输入被归一」。

**步骤**：

1. 准备一张本地图片 `a.png`，再找一个公开 http(s) 图片链接。
2. 运行：

```python
# 示例代码
import asyncio
from PIL import Image
from sglang_omni.preprocessing import ensure_image_list_async

async def main():
    local = Image.open("a.png")          # 已是 PIL 对象
    out = await ensure_image_list_async(["a.png", "https://example.com/x.png", local])
    print(type(out), len(out), [type(x).__name__ for x in out])

asyncio.run(main())
```

**预期结果**：输出三个 `Image.Image`，顺序与输入一致；本地路径项与已传 PIL 项都不会触发网络请求。

**需要观察的现象**：把日志调到 DEBUG（见 4.3 connector 里 `> 1MB` 时会打下载耗时），能看到只有那一项 URL 触发了下载。

> 待本地验证：具体耗时与是否联网相关；若 `MultiModalResourceConnector` 配了域名白名单且 `example.com` 不在内，会抛 `ValueError: Domain ... is not allowed.`（见 4.3）。

#### 4.2.5 小练习与答案

**练习**：为什么 `ensure_image_list_async` 要用「两遍扫描 + 下标回填」，而不是顺序 `await` 每一项？
**答案**：顺序 `await` 会把多个 URL 的下载**串行化**，首字节时延 = 所有下载时间之和。两遍扫描把所有 URL 先建成 task，再用一次 `asyncio.gather` 并发等待，下载时间≈最慢的一项；下标回填保证了输出顺序与输入一致。

---

### 4.3 resource_connector：本地与远程媒体的访问控制（防越界 / 防 SSRF）

#### 4.3.1 概念说明

`MultiModalResourceConnector` 是预处理层的**安全 + 取数中枢**。它把「把一个 URL 变成字节流」这件危险的事拆成三步，每一步都带校验：

1. **协议分流**：`http(s)` / `data` / `file` 各走一条路径。
2. **访问控制**：
   - `file://` 必须落在 `allowed_local_media_path` 目录**内**，否则拒绝（防路径穿越读 `/etc/passwd`）；
   - 未配置 `allowed_local_media_path` 时，本地文件加载**整体禁用**；
   - `http(s)` 受域名白名单 `allowed_media_domains` 约束，并可选 `reject_unsafe_remote_addresses` 拒绝解析到 loopback/private/link-local 等内网地址（防 SSRF）。
3. **大小与重定向约束**：`max_bytes` 限响应体积，重定向每跳都重新校验域名，最多 `_MAX_HTTP_REDIRECTS=5` 跳。

设计哲学：**取数与解码分离**。connector 只负责「安全地拿到 bytes（和 MIME）」，然后把 bytes 交给对应 `MediaIO.decode`；解码放到全局线程池 `global_thread_pool` 里跑，不阻塞 asyncio 事件循环。

#### 4.3.2 核心流程

```
load_resource(url, media_io)
        │
        ▼ urlparse
 scheme?
 ├─ http* → _load_http_bytes(每跳 _assert_url_allowed + max_bytes + ≤5 redirects)
 │              → media_io.load_http_bytes(data, media_type)
 ├─ data   → _load_data_url(必须 ";base64")  → media_io.load_base64(type, data)
 └─ file   → _load_file_url:
              ① 未配 allowed_local_media_path → RuntimeError(禁用)
              ② netloc 非空且非 localhost → ValueError
              ③ Path(url2pathname(path)).resolve() 必须 .relative_to(allowed) 否则 ValueError
              → media_io.load_file(filepath)
```

**本地越界防护的三道闸**（任意一道命中即拒绝）：

1. **构造时**：`allowed_local_media_path` 经 `resolve_allowed_local_media_path` 校验必须是**存在且是目录**。
2. **禁用闸**：未配置时 `_load_file_url` 直接 `RuntimeError("Local file loading is disabled.")`。
3. **穿越闸**：`.resolve()` 展开所有 `..` / 符号链接后再 `relative_to(allowed)`，跳出目录即 `ValueError`。

#### 4.3.3 源码精读

[sglang_omni/preprocessing/resource_connector.py:67-71](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/preprocessing/resource_connector.py#L67-L71) `resolve_allowed_local_media_path`：`expanduser().resolve()` 规范化后，要求 `exists() and is_dir()`，否则 `ValueError`。这一步在构造 connector 时就拦下「指向文件而非目录」「指向不存在路径」的非法配置。

[sglang_omni/preprocessing/resource_connector.py:286-300](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/preprocessing/resource_connector.py#L286-L300) `_load_file_url` 本地越界防护核心：禁用闸（288-289）、netloc 闸（291-293）、**穿越闸（294-299）**——`Path(url2pathname(url_spec.path)).resolve()` 先把 URL 路径转成本机路径并展开 `..`，再 `.relative_to(self.allowed_local_media_path)`，越界抛 `ValueError`。注意 `.resolve()` 是关键：它把 `allowed/../secret` 提前规约成绝对路径，让穿越无所遁形。

[sglang_omni/preprocessing/resource_connector.py:241-266](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/preprocessing/resource_connector.py#L241-L266) `_assert_url_allowed` 远程访问控制：无白名单且 `allow_remote_media_without_domains=False` 时一律拒绝（247-253）；有白名单则用 `_is_allowed_remote_domain` 做后缀匹配（254-258）；若 `reject_unsafe_remote_addresses=True`，对解析出的每个 IP 调 `_unsafe_remote_address_category`，命中 loopback/private/link-local/reserved/multicast/unspecified 即拒绝（260-266）——这是防 SSRF 的核心。

[sglang_omni/preprocessing/resource_connector.py:109-131](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/preprocessing/resource_connector.py#L109-L131) `_resolve_remote_addresses`：先尝试把主机名当字面 IP，否则 `getaddrinfo` 解析全部 A/AAAA 记录，返回一个 IP 集合。SSRF 检查针对的是**解析后的真实 IP**，而不只是主机名字符串（否则 `127.0.0.1.nip.io` 这类会绕过）。

[sglang_omni/preprocessing/resource_connector.py:398-426](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/preprocessing/resource_connector.py#L398-L426) `_load_http_bytes`：**每跳重定向都重新 `_assert_url_allowed`**（408 行在循环内），`follow_redirects=False` 手动跟跳，最多 5 跳，配合 `_read_limited_response_bytes` 的 `max_bytes` 体积上限——防止重定向绕过域名白名单、防止超大响应打爆内存。

[sglang_omni/preprocessing/resource_connector.py:27-28](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/preprocessing/resource_connector.py#L27-L28) `global_thread_pool`：8 worker 的全局线程池，CPU 密集的解码（重采样、视频抽帧）丢到这里跑，`atexit` 注册优雅关闭。

生产侧的真实构造方式（带安全策略）：

[sglang_omni/serve/speech_service.py:119-123](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/serve/speech_service.py#L119-L123) `/v1/audio/speech` 的参考音频 connector 显式开启 `reject_unsafe_remote_addresses=True`——参考音频来自用户提交的 URL，SSRF 风险最高，故默认收紧。

CLI 侧把 flag 翻译成 connector 配置：

[sglang_omni/cli/serve.py:242-248](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/cli/serve.py#L242-L248) `_validate_allowed_local_media_path` 在启动期就用 `resolve_allowed_local_media_path` 校验目录合法性，非法直接 `typer.BadParameter` 拒绝启动，而非等到运行时才报错。

[sglang_omni/cli/serve.py:929-949](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/cli/serve.py#L929-L949) `--allowed-local-media-path`（默认 `None`＝本地加载禁用）与 `--allowed-media-domain`（可重复，默认任意公网域名放行）两个 flag 的声明与帮助文案。

#### 4.3.4 代码实践

**目标**：阅读 `resolve_allowed_local_media_path` 与 `_load_file_url`，亲手验证「越界路径」与「本地加载未开启」两种情况都被拒绝。

**步骤**：

1. 对照上面 67-71 行与 286-300 行的源码，理解三道闸。
2. 在 `tests/unit_test/preprocessing/` 下新建 `test_resource_connector_local.py`，写入下面的 GPU-free 测试（无需联网、无需 GPU）：

```python
# SPDX-License-Identifier: Apache-2.0
"""验证 MultiModalResourceConnector 的本地 file:// 越界与禁用防护。"""
from urllib.parse import urlparse

import pytest

from sglang_omni.preprocessing.base import MediaIO
from sglang_omni.preprocessing.resource_connector import (
    MultiModalResourceConnector,
    resolve_allowed_local_media_path,
)


class _DummyIO(MediaIO[bytes]):
    """只用于探测是否被放行；放行才会调用 load_file。"""
    def load_bytes(self, data: bytes) -> bytes:
        return data
    def load_base64(self, media_type: str, data: str) -> bytes:
        return data.encode()
    def load_file(self, filepath) -> bytes:
        return b"ALLOWED:" + str(filepath).encode()


def test_resolve_allowed_local_media_path_rejects_bad_inputs(tmp_path):
    # 不存在的路径 -> ValueError
    with pytest.raises(ValueError):
        resolve_allowed_local_media_path(tmp_path / "nope")
    # 指向文件而非目录 -> ValueError
    f = tmp_path / "a.txt"
    f.write_text("x")
    with pytest.raises(ValueError):
        resolve_allowed_local_media_path(f)


def test_file_url_traversal_is_rejected(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    secret = tmp_path / "secret.txt"            # 在 allowed 的上一级
    secret.write_text("root-only")

    conn = MultiModalResourceConnector(allowed_local_media_path=str(allowed))

    # 用一个 ".." 试图跳出 allowed 指向 secret
    evil = urlparse(f"file://{allowed}/../{secret.name}")
    with pytest.raises(ValueError, match="not within allowed directory"):
        conn._load_file_url(evil, _DummyIO())


def test_file_url_disabled_when_unconfigured(tmp_path):
    # 未配置 allowed_local_media_path -> 本地加载被整体禁用
    conn = MultiModalResourceConnector()        # allowed_local_media_path 默认 None
    with pytest.raises(RuntimeError, match="Local file loading is disabled"):
        conn._load_file_url(urlparse("file:///etc/passwd"), _DummyIO())


def test_file_url_inside_allowed_is_loaded(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    legit = allowed / "ok.wav"
    legit.write_bytes(b"fake wav")

    conn = MultiModalResourceConnector(allowed_local_media_path=str(allowed))
    out = conn._load_file_url(urlparse(f"file://{legit}"), _DummyIO())
    assert out.startswith(b"ALLOWED:")          # 放行：load_file 被调用
```

**需要观察的现象 / 预期结果**：四个用例全绿。

- `test_resolve...`：非目录、不存在都抛 `ValueError`。
- `test_file_url_traversal_is_rejected`：`file://.../allowed/../secret.txt` 经 `.resolve()` 规约成 `.../secret.txt`，`relative_to(allowed)` 失败 → `ValueError`。**关键在于 `.resolve()` 在比较前就展开了 `..`**。
- `test_file_url_disabled_when_unconfigured`：未配置直接 `RuntimeError`，连越界检查都不走。
- `test_file_url_inside_allowed_is_loaded`：合法路径放行，`load_file` 被调用。

**运行命令**：

```bash
pytest tests/unit_test/preprocessing/test_resource_connector_local.py -v
```

> 待本地验证：该测试不依赖 GPU 与网络，只需可 import `sglang_omni`。若你发现穿越用例**没**抛错，说明 `allowed` 的真实父目录恰好可被 `relative_to` 容纳——请检查 `tmp_path` 与 `secret` 是否真为父子关系。

#### 4.3.5 小练习与答案

**练习 1**：为什么穿越检查用 `filepath.relative_to(allowed)` 而不是字符串 `startswith(str(allowed))`？
**答案**：字符串前缀匹配会被目录名前缀撞车——`/data/allowed` 会错误放行 `/data/allowed-secret/x`。`Path.relative_to` 是路径语义比较，只有真正是子路径才不抛异常；配合 `.resolve()` 还能展开 `..` 与符号链接。

**练习 2**：`reject_unsafe_remote_addresses` 为什么检查的是**解析后的 IP**而不是主机名字符串？
**答案**：攻击者可以用 `169-254-169-254.nip.io` 这类把 IP 编码进域名的主机名绕过字符串黑名单。`_resolve_remote_addresses` 先 `getaddrinfo` 拿到真实 IP，再判类别（loopback/private/...），才能堵住这类绕过。这也意味着该检查有 DNS 依赖与 TOCTOU（解析后又被改指向）的固有窗口，所以它是**纵深防御的一层**而非唯一手段。

**练习 3**：重定向为什么要在**每一跳**重新调用 `_assert_url_allowed`？
**答案**：若只在首跳校验，攻击者可用一个公网合法域名做 302 跳转到 `http://127.0.0.1/...`，借服务器之手访问内网。每跳都校验，跳转目标也必须通过白名单 + SSRF 检查。

---

### 4.4 cache_key：在转换前用「原始输入」算稳定缓存键

#### 4.4.1 概念说明

预处理之后，有些产物**非常昂贵**且**可复用**——最典型的是「参考音频编码」（声音克隆里把一段参考音频过一遍编码器得到的 embedding，见 u6-l5）。这类产物通常配一个 LRU 缓存，键就是「输入的身份」。`cache_key` 子模块负责把**异构输入**算成一段稳定的字符串键。它有两条硬规则：

1. **必须在转换前算**。原始输入是文件路径或 URL 字符串，哈希它们极廉价；一旦被 `ensure_*_list_async` 解码成大数组，再哈希就要读全量像素/采样，代价陡增。所以 Qwen3-Omni 预处理里「算 key」严格排在「解码」之前（见 499-502 行）。
2. **键必须覆盖所有影响产物的输入**。视频的解码参数（fps、max_frames、像素预算）会改变帧数进而改变编码输出长度，所以 `compute_video_cache_key` 把解码参数也并进键——否则一个请求的 `video_embeds` 长度会和占位符对不上。

#### 4.4.2 核心流程

```
原始输入(路径/URL/PIL/ndarray/Tensor/bytes)
        │
        ▼  hash_media_item: 按类型分派
   file:hash_file_sampled(path)   ← 只读 head+tail+size，不读全文
   url:hash_bytes(str)
   pil: mode|size|hash(tobytes)
   np:  dtype|shape|hash(tobytes)
   pt:  dtype|shape|hash(cpu bytes)
   bytes: hash_bytes(...)
        │
        ▼  compute_media_cache_key: 把多个 item 的 hash 用 xxh3 再 join
   "<prefix>:<joined_hash>"        ← prefix ∈ image/audio/video
```

参考音频文件还有一条专门的**带记忆化**路径 `reference_path_cache_key`：用 `(size, mtime_ns, ctime_ns)` 的 stat 元组做 memo key，稳定文件第二次查询直接命中、不重读字节。

#### 4.4.3 源码精读

[sglang_omni/preprocessing/cache_key.py:163-209](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/preprocessing/cache_key.py#L163-L209) `hash_media_item` 按 Python 类型分派哈希策略：`str/Path` 区分 URL 与本地文件（本地文件走 `hash_file_sampled` 采样哈希）；PIL/numpy/tensor 各取 `mode|size` / `dtype|shape` 元信息 + 内容 `tobytes()` 哈希；不支持的类型返回 `None`（让调用方跳过缓存）。返回值都带类型前缀（`file:` / `url:` / `pil:` / `np:` / `pt:` / `bytes:`），天然分区命名空间。

[sglang_omni/preprocessing/cache_key.py:29-52](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/preprocessing/cache_key.py#L29-L52) `hash_file_sampled`：只读文件**头 8KB + 尾 8KB + 文件大小**拼起来哈希，不读全文。理由是压缩格式（JPEG/PNG/WAV/MP4）的内容变化通常会反映到体积或头尾字节，绝大多数改动都能被捕获，而读取成本与文件大小解耦。

[sglang_omni/preprocessing/cache_key.py:212-235](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/preprocessing/cache_key.py#L212-L235) `compute_media_cache_key`：把列表里每个 item 过一遍 `hash_media_item`，**任意一个返回 None 就整体返回 None**（宁可不放缓存也不可错键）；全部有效则用 `_hash_joined`（`xxh3_64` of `"|".join(parts)`）压成一个键，并加 `prefix:` 前缀。

[sglang_omni/preprocessing/cache_key.py:125-160](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/preprocessing/cache_key.py#L125-L160) `reference_path_cache_key`：参考音频专用。memo key 是 `路径:size:mtime_ns:ctime_ns`（133-137 行）；默认路径下用「sentinel（采样字节哈希）」做二次校验，memo 命中且 sentinel 不变才复用（148-151 行），彻底校验时才 `read_bytes` 全文哈希（153-156 行），最后把 `(memo_key → sentinel, digest)` 存回 memo（157-159 行）。`trust_stat=True` 是更激进的快路径：memo 命中即跳过 sentinel 读取，仅信任 stat 元组。

[sglang_omni/preprocessing/cache_key.py:55-58](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/preprocessing/cache_key.py#L55-L58) memo 是全局 `OrderedDict` + 锁 + 1024 上限的 LRU，跨请求共享，稳定参考音频只哈希一次。

视频键的「参数并入」是「键必须覆盖所有影响产物的输入」的最佳示范：

[sglang_omni/preprocessing/video.py:386-410](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/preprocessing/video.py#L386-L410) `compute_video_cache_key` 在 `compute_media_cache_key` 的基础上，把 `fps/max_frames/min_px/max_px/total_px` 拼成 `decode_sig` 追加到键尾（403-410 行），并在 docstring 里点明：解码参数改变帧数、改变编码输出长度，不并入键就会导致 `video_embeds` 长度与 prompt 占位符对不上。

现有单测覆盖了这条键的正确性，可作为参考：

[tests/unit_test/preprocessing/test_cache_key.py:7-36](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/tests/unit_test/preprocessing/test_cache_key.py#L7-L36) 验证「同内容稳定、内容变则键变、同尺寸改中段不串台、URL 与缺失文件返回 None」。

#### 4.4.4 代码实践

**目标**：体会「键必须覆盖所有影响产物的输入」——亲手构造一个会串台的键，然后修正它。

**步骤**：

1. 阅读上面的 `compute_video_cache_key` 与 `hash_media_item`。
2. 在交互式 Python 跑：

```python
# 示例代码
from sglang_omni.preprocessing import compute_image_cache_key, compute_video_cache_key

# 同一张图片的路径，键稳定
print(compute_image_cache_key(["a.png"]))
print(compute_image_cache_key(["a.png"]))   # 两次相同

# 视频键把解码参数并入：同文件、不同 fps -> 不同键（否则会串台！）
k1 = compute_video_cache_key(["v.mp4"], fps=2.0,  max_frames=8)
k2 = compute_video_cache_key(["v.mp4"], fps=10.0, max_frames=8)
print(k1 != k2)   # True —— fps 不同，产物不同，键必须不同
```

**预期结果**：`compute_image_cache_key` 两次返回相同字符串；视频键 `k1 != k2` 为 `True`。

**需要观察的现象**：把 `compute_video_cache_key` 换成不并参数的 `compute_media_cache_key(["v.mp4"], prefix="video")`，两次会得到**相同**键——这就是「串台」的根源：两个本应不同的产物被当成同一个缓存条目。

> 待本地验证：`a.png` / `v.mp4` 需真实存在；若用不存在路径，`hash_media_item` 会把它当 URL 哈希，键仍稳定但语义不同。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `compute_media_cache_key` 在**任意一个** item 返回 `None` 时整体返回 `None`，而不是跳过该项继续算？
**答案**：返回 `None` 表示「这个 item 类型不支持稳定哈希」。若跳过它继续算，列表 `[A, 不支持]` 与 `[A]` 会得到同一个键——把一个本应不同的产物当成相同，造成串台（一个请求的参考音频编码被另一个请求复用）。整体放弃缓存（返回 None 让调用方 bypass）是「宁可慢、不可错」的安全选择。

**练习 2**：`reference_path_cache_key` 的 memo 为什么用 `(size, mtime_ns, ctime_ns)` 做 key，还要再用 sentinel 二次校验？
**答案**：stat 元组是「文件身份」的廉价近似——大小或修改时间变了，内容几乎必然变了，可借此跳过字节读取。但它不是内容本身：理论上存在「同 size、同 mtime/ctime、内容却不同」的极端情况（如时钟回拨后覆盖写）。sentinel（采样字节哈希）是对 memo 命中条目的二次确认，堵住这个缝隙，代价仅一次少量字节读。

**练习 3**：`hash_media_item` 给每种类型加前缀（`file:` / `pil:` / ...）有什么好处？
**答案**：天然分区命名空间，避免跨类型哈希碰撞——比如一个文件路径字符串的哈希和一个 numpy 数组的哈希理论上可能撞值，加了类型前缀后即使数值相同也落到不同的键上，杜绝串台。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「带安全策略 + 缓存键」的参考音频加载。

**任务**：写一个异步函数 `load_reference_audio(audio_ref, allowed_dir)`，要求：

1. 用一个**开启了本地目录 `allowed_dir` 与域名白名单 `["cdn.example.com"]`、并拒绝不安全远程地址**的 `MultiModalResourceConnector`。
2. 在**取数之前**用 `compute_audio_cache_key` 算出缓存键并打印（模拟「缓存键先于解码」）。
3. 通过 connector 的 `fetch_audio_async` 取数（`target_sr=16000`）。
4. 演示三种输入各走哪条路：一个 `file://` 在 `allowed_dir` 内的合法引用（成功）、一个 `file://` 用 `..` 越界的引用（被拒）、一个 `http://127.0.0.1/...`（被 SSRF 拦）。

**参考骨架**（示例代码，需你补全断言）：

```python
# 示例代码
import asyncio
from sglang_omni.preprocessing import compute_audio_cache_key
from sglang_omni.preprocessing.resource_connector import MultiModalResourceConnector

async def load_reference_audio(audio_ref: str, allowed_dir: str) -> None:
    conn = MultiModalResourceConnector(
        allowed_local_media_path=allowed_dir,
        allowed_media_domains=["cdn.example.com"],
        allow_remote_media_without_domains=False,
        reject_unsafe_remote_addresses=True,
    )
    key = compute_audio_cache_key(audio_ref)          # ① 先算键（廉价）
    print("cache_key =", key)
    audio, sr = await conn.fetch_audio_async(audio_ref, target_sr=16000)  # ② 再取数
    print("loaded", audio.shape, sr)

# 自行用 4.3.4 的 tmp_path 技巧构造合法/越界 file:// 与内网 http:// 三种引用，
# 分别调用上面的函数，断言后两者抛 ValueError、前者成功。
```

**验收标准**：

- 合法 `file://` 路径：打印出非 None 的 `cache_key`，并成功拿到 `(audio, sr)`。
- 越界 `file://`：`fetch_audio_async` 抛 `ValueError(... not within allowed directory)`。
- 内网 `http://127.0.0.1/...`：抛 `ValueError(... resolves to a loopback address)`。
- 能解释：为什么 `compute_audio_cache_key` 必须在 `fetch_audio_async` **之前**调用。（答：键基于原始引用字符串，廉价且稳定；放到解码后就要哈希整段采样数组，昂贵且失去「先查缓存再决定是否解码」的意义。）

## 6. 本讲小结

- **`MediaIO` 是全包宪法**：每种模态实现 `load_bytes / load_base64 / load_file` 三入口，把「来源多态」收敛成「对象单一」，解码与取数彻底解耦。
- **`ensure_*_list_async` 是归一化器**：两遍扫描 + `asyncio.gather` 并发拉取 URL + 已处理对象透传，输出顺序稳定的「已加载对象列表」。
- **`MultiModalResourceConnector` 是安全中枢**：`file://` 靠 `allowed_local_media_path` + `.resolve()` + `relative_to` 三道闸防路径穿越；`http(s)` 靠域名白名单 + 解析后 IP 类别检查防 SSRF；重定向每跳重校验、`max_bytes` 限体积。
- **取数与解码分离**：connector 只安全取 bytes，解码丢全局线程池，不阻塞 asyncio 循环。
- **cache_key 必须在转换前算**：基于原始引用（路径/URL）廉价哈希；`hash_media_item` 按类型分派并加前缀防串台；`reference_path_cache_key` 用 stat memo + sentinel 兼顾速度与正确性。
- **键必须覆盖所有影响产物的输入**：`compute_video_cache_key` 把 fps/帧数/像素预算并入键，否则 `video_embeds` 长度会与占位符对不上——这是「宁可不放缓存，不可错键」纪律的具体体现。

## 7. 下一步学习建议

- **u6-l5（参考音频编码缓存服务）**：本讲的 `cache_key` 与 `reference_path_cache_key` 正是那个缓存服务的键来源；读完 u6-l5 你会看到「键 → byte-budget LRU → single-flight → hook」的完整闭环。
- **u5-l2（Qwen3-Omni 端到端管线）**：本讲引用的 `qwen3_omni/components/preprocessor.py` 就是 Qwen3-Omni 的 preprocessing stage，结合 u5-l2 可看清「归一化后的多模态张量如何被 thinker 消费」。
- **继续阅读源码**：`sglang_omni/preprocessing/audio.py` 的 `_parse_wav_bytes`（手写 WAV 解析）与 `_resample_linear`（线性重采样）是练手「无依赖解码」的好材料；`sglang_omni/preprocessing/text.py` 的 `normalize_messages` / `append_modality_placeholders` 则补齐了文本模态的归一化。
- **动手扩展**：试着仿照 `ImageMediaIO` 为一种新输入（如 raw PCM blob）写一个 `MediaIO`，并接入 `ensure_*_list_async` 的两遍扫描模式，检验你对本讲四个模块的理解。
