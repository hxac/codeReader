# 图像解码后端：libjpeg-turbo 与并行媒体解码

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 Dynamo 前端解码一张 JPEG 时的**后端选择顺序**：先探测格式 → libjpeg-turbo 优先 → 按条件回退到 `image::ImageReader`。
2. 解释 libjpeg-turbo 相对纯 Rust PIL 系解码的性能来源（SIMD 优化的 C 库），以及它的**兼容性边界**：CMYK/YCCK JPEG 必须回退。
3. 区分两类"失败"：**可以回退的拒绝（NotHandled）** 与 **不可回退的错误（Err，如超限额）**，理解为什么限额绝不允许被另一个后端绕过。
4. 说出 Python 侧如何开启前端解码（`--frontend-decoding` + `MediaDecoder`）、后端开关环境变量（`DYN_MM_ENABLE_LIBJPEG` 等），以及"并行"发生在哪两层（请求内 `join_all` + 解码卸载到 rayon CPU 池）。
5. 运行 `benchmarks/multimodal/media_decode/run_image.sh` 基准，对比两条解码路径的时延与并发吞吐。

本讲聚焦**解码器内部**；解码产物（像素张量）如何经 NIXL 注册、发给编码 worker，属于 E/P/D 多模态链路，见 u8-l9。

## 2. 前置知识

**图像解码在做什么。** 多模态请求里的图片通常以 JPEG/PNG 等**压缩格式**传输（URL 或 base64）。视觉模型需要的却是解压后的像素矩阵——形状为 \([H, W, C]\)（高、宽、通道数）、类型为 uint8 的张量。把压缩字节还原成像素矩阵的过程就是图像解码，它是纯 CPU 密集操作，一张 4K JPEG 可能要解出 \(3840 \times 2160 \times 3 \approx 24.9\) MB 像素。

**为什么解码器值得优化。** 在传统架构里，图片由推理引擎（vLLM/SGLang 等）里的 Python 解码（PIL）。当请求里图片多、并发高时，Python 进程的解码会挤占引擎 CPU、拖长首 token 时间。Dynamo 的"并行媒体解码"把这一步挪到 Rust 前端：在 CPU 线程池上并发解码，再经 NIXL 把像素直传后端（见 u8-l9）。解码器每快一点，每个 worker 就省一点 CPU。

**libjpeg-turbo 与 TurboJPEG API。** libjpeg-turbo 是经典 libjpeg 的 SIMD（AVX2/NEON）加速实现，是业界最快的 JPEG 软件解码器之一；TurboJPEG 是它自带的简洁 C API（`tjInitDecompress` / `tjDecompressHeader3` / `tjDecompress2` / `tjDestroy` 四个函数就够解码用）。Rust 生态的 `image` crate 则是纯 Rust 实现（PIL 的同类物），格式覆盖广但 JPEG 慢。本讲的核心就是：**JPEG 用 libjpeg-turbo，其余一切格式及 turbojpeg 啃不动的输入用 `image::ImageReader` 兜底**。

**dlopen 动态加载。** 一般 Rust 调 C 库要在编译期链接。Dynamo 改用运行时 `dlopen` 打开 `libturbojpeg.so.0`：好处是没装这个库的镜像照样能跑（自动回退），坏处是要手写函数指针类型与 `unsafe`。这是本讲 FFI 部分的背景。

**与 u4-l3 的衔接。** u4-l3 讲过媒体抓取（loader 与 SSRF 防护）和 OpenAI 预处理整体流程；本讲钻进其中"字节 → 像素"这一小步的内部实现。如果你还记得 `PreprocessedRequest` 里多模态数据要变成 RDMA 描述符，本讲的解码正是它的上游。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [lib/llm/src/preprocessor/media/decoders/image.rs](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/media/decoders/image.rs) | `ImageDecoder`：解码编排层，负责格式探测、后端选择、限额校验、回退策略 |
| [lib/llm/src/preprocessor/media/decoders/image/backends.rs](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/media/decoders/image/backends.rs) | 后端抽象：`ImageDecodeBackend` trait、公共类型（请求/结果/拒绝原因） |
| [lib/llm/src/preprocessor/media/decoders/image/backends/image_reader.rs](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/media/decoders/image/backends/image_reader.rs) | 兜底后端：包装纯 Rust `image::ImageReader`，支持所有格式 |
| [lib/llm/src/preprocessor/media/decoders/image/backends/turbojpeg.rs](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/media/decoders/image/backends/turbojpeg.rs) | 快速后端：只认 JPEG，把工作转交给 jpeg_turbo 模块 |
| [lib/llm/src/preprocessor/media/jpeg_turbo.rs](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/media/jpeg_turbo.rs) | FFI 封装：dlopen libturbojpeg、头解析、CMYK 拒绝、限额、解码 |
| [lib/llm/src/preprocessor/media/decoders.rs](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/media/decoders.rs) | `Decoder` trait（含 `decode_async` 卸载到 rayon）与 `MediaDecoder` 配置聚合 |
| [components/src/dynamo/common/utils/media_decoder.py](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/components/src/dynamo/common/utils/media_decoder.py) | Python 侧前端解码器选项构造（max_alloc 等） |
| [components/src/dynamo/vllm/multimodal_utils/media_config.py](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/components/src/dynamo/vllm/multimodal_utils/media_config.py) | vLLM worker 侧把 `--frontend-decoding` 变成 `MediaDecoder` + `MediaFetcher` 配置 |
| [lib/llm/benches/image_decode.rs](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/benches/image_decode.rs) | Criterion 基准：单张时延 + 4K×100 张并发扫描（c1/c8/c32） |
| [benchmarks/multimodal/media_decode/run_image.sh](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/benchmarks/multimodal/media_decode/run_image.sh) | 基准入口脚本：设环境变量后调 `cargo bench` |

辅助阅读：[lib/llm/src/preprocessor/media/README.md](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/media/README.md)（模块级文档，含 JPEG 解码选项说明）、[benchmarks/multimodal/media_decode/README.md](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/benchmarks/multimodal/media_decode/README.md)。

## 4. 核心概念与源码讲解

### 4.1 解码编排：ImageDecoder 的选择顺序与限额

#### 4.1.1 概念说明

`ImageDecoder` 是前端图像解码的**编排层**。它自己不解码，只做四件事：

1. **格式探测**：从字节嗅探出 `ImageFormat`；
2. **后端选择**：JPEG 且 libjpeg-turbo 可用且未被禁用 → 先走 turbojpeg；
3. **限额守门**：宽/高/总分配字节三道限额，超了直接报错（不回退）；
4. **回退裁决**：turbojpeg "拒绝处理"时，决定是否允许落到 image_reader。

为什么限额不能被回退绕过？设想一张 8000×8000 的图：如果 turbojpeg 因超限报错、然后系统悄悄换 image_reader 把它解出来，那防御就形同虚设。所以"资源超限"必须是**硬错误**，而"这个后端搞不定这种输入"才是**软拒绝**。

#### 4.1.2 核心流程

```
decode(EncodedMediaData)
  ├─ into_bytes()            # 若 b64_encoded 则先做 base64 解码
  ├─ warn_if_libjpeg_unavailable()   # 进程级一次警告
  ├─ image::guess_format()   # 嗅探格式（PNG/JPEG/WebP/BMP…）
  ├─ 组装 ImageDecodeRequest { bytes, format, limits }
  ├─ if enable_libjpeg && turbojpeg.supports(format)   # supports == 只认 JPEG
  │    ├─ turbojpeg.try_decode
  │    │    ├─ Decoded(img)          → 完成
  │    │    └─ NotHandled(reason)
  │    │         ├─ ensure_fallback_allowed()   # CI 守卫环境变量下：直接报错
  │    │         └─ decode_required_backend(image_reader) → 完成/报错
  └─ else
       └─ decode_required_backend(image_reader) → 完成/报错
```

限额的数学很简单：设图像高 \(H\)、宽 \(W\)、通道数 \(C\)，则解码需要分配

\[ nbytes = H \times W \times C \]

若 `max_alloc` 存在且 \(nbytes > max\_alloc\)，或 \(W > max\_image\_width\)、\(H > max\_image\_height\)，立即失败。默认 `max_alloc = 128 MB`。

#### 4.1.3 源码精读

后端总开关是一个**前端进程本地**的环境变量，默认开启，只有显式写 `0`/`false` 才关闭（解析交给 `parse_bool_opt`，非法值也视为开）：

[lib/llm/src/preprocessor/media/decoders/image.rs:L22-L37](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/media/decoders/image.rs#L22-L37) — 定义 `DYN_MM_ENABLE_LIBJPEG`（总开关）与 `DYNAMO_REQUIRE_LIBJPEG_TURBO_TEST`（CI 守卫：启用的 JPEG 测试必须走 TurboJPEG、绝不许回退），以及 `libjpeg_enabled` 的默认值逻辑。

限额结构体与校验函数：

[lib/llm/src/preprocessor/media/decoders/image.rs:L42-L84](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/media/decoders/image.rs#L42-L84) — `ImageDecoderLimits` 三字段（`max_image_width` / `max_image_height` / `max_alloc`，默认 128 MB），`validate_output` 用 `checked_mul` 防乘法溢出后做三项检查。

注意 `enable_libjpeg` 字段被 `#[serde(skip)]` 排除在配置序列化之外——它**只能**来自环境变量，不属于模型部署卡（MDC）或请求配置：

[lib/llm/src/preprocessor/media/decoders/image.rs:L86-L104](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/media/decoders/image.rs#L86-L104) — `ImageDecoder` 结构体；`enable_libjpeg` 上有注释 "Frontend-local backend selection; never part of model or request configuration"。

解码主流程（本模块的心脏）：

[lib/llm/src/preprocessor/media/decoders/image.rs:L132-L162](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/media/decoders/image.rs#L132-L162) — `decode()`：`into_bytes` → 一次性警告 → `guess_format` → 组请求 → 按 `enable_libjpeg && turbojpeg.supports(format)` 分派，`NotHandled` 时经 `ensure_fallback_allowed` 再落 image_reader。

回退守卫与"必答后端"：

[lib/llm/src/preprocessor/media/decoders/image.rs:L164-L188](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/media/decoders/image.rs#L164-L188) — `ensure_fallback_allowed`：当 `DYNAMO_REQUIRE_LIBJPEG_TURBO_TEST` 已设时，本应回退的输入直接报错（保证 CI 真的在测 turbojpeg 而不是悄悄测了兜底）；`decode_required_backend`：image_reader 若也拒绝，则把拒绝原因包成错误上抛——它是最后一道，没有再下一级。

libjpeg-turbo 缺库时的一次性警告：

[lib/llm/src/preprocessor/media/decoders/image.rs:L211-L231](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/media/decoders/image.rs#L211-L231) — `warn_if_libjpeg_unavailable` 用 `Once` 保证每进程只警告一次；`MediaLoader::new` 构造时也会调它（见 [lib/llm/src/preprocessor/media/loader.rs:L457-L458](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/media/loader.rs#L457-L458)），所以缺库在启动阶段就能在日志里看到。

#### 4.1.4 代码实践

1. **实践目标**：验证 `DYN_MM_ENABLE_LIBJPEG` 的默认开启语义，以及它不是序列化配置字段。
2. **操作步骤**：
   - 阅读测试 [lib/llm/src/preprocessor/media/decoders/image.rs:L459-L481](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/media/decoders/image.rs#L459-L481)：`test_libjpeg_defaults_on_unless_explicitly_disabled` 与 `test_libjpeg_selection_is_not_media_config`。
   - 运行：`cargo test -p dynamo-llm test_libjpeg -- --nocapture`。
   - 再阅读 [lib/llm/src/preprocessor/media/decoders/image.rs:L423-L455](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/media/decoders/image.rs#L423-L455) 的 `test_with_runtime_preserves_server_config`，确认运行时 `media_io_kwargs` 无法覆盖 MDC 限额。
3. **需要观察的现象**：四个断言 `libjpeg_enabled(None/"", "invalid"/"0")` 分别通过；给 JSON 塞 `"enable_libjpeg": false` 会得到 "unknown field" 错误。
4. **预期结果**：默认开、显式 0 才关、配置面不可见，三条语义全部被测试锁死。
5. 本机无 Rust 工具链时，此实践退化为纯源码阅读，结论已由测试代码背书。

#### 4.1.5 小练习与答案

**练习 1**：一台机器没装 `libturbojpeg`，请求里来了一张 PNG。会触发回退吗？会打警告吗？
**答案**：不会"回退"——`turbojpeg.supports(Png)` 本来就是 `false`，PNG 直接走 image_reader，根本不进入 turbojpeg 分支。警告也不会打：`warn_if_libjpeg_unavailable` 只在 `enable_libjpeg` 为真时判断可用性，但注意它是在 `decode()` 一进来就无条件调用的（[L134](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/media/decoders/image.rs#L134)），所以只要解过任何一张图，缺库警告就会打一次，与当前输入是不是 PNG 无关。

**练习 2**：为什么 `decode_required_backend` 在 image_reader 返回 `NotHandled` 时要报错，而不是返回空图？
**答案**：image_reader 是最后一道防线且 `supports` 恒为 `true`（见 4.2），它拒绝只剩一种合理解释——输入确实解不动（损坏数据）。此时返回错误让请求以无效参数失败，比伪造像素喂给模型安全得多。

**练习 3**：`max_alloc` 限 128 MB 时，最多能解多大的 RGB 图？
**答案**：由 \(H \times W \times 3 \le 128 \times 1024 \times 1024\)，约 4369×4369 的正方形 RGB 图是上限（更精确地 \(H \times W \le \lfloor 134217728/3 \rfloor = 44739242\) 像素）。

### 4.2 后端抽象：ImageDecodeBackend trait 与两个实现

#### 4.2.1 概念说明

`backends.rs` 把"一个图像解码后端"抽象成四个能力的 trait：叫什么名字、可不可用、支不支持某格式、试着解一次。编排层（4.1）只依赖这个 trait，不依赖任何具体库。这带来两个收益：

- **可测试**：拒绝原因（`BackendDecline`）是一等公民，回退路径可以被单元测试精确覆盖；
- **可扩展**：将来加 nvJPEG 硬解（README 的 TODO 里明确列了这一项）就是再实现一个 trait。

两个现成实现是对偶的：`TurboJpegBackend` 窄而快（只 JPEG），`ImageReaderBackend` 广而慢（全格式、恒可用）。

#### 4.2.2 核心流程

```
ImageDecodeBackend trait
  ├─ name()            → "libjpeg_turbo" / "image_reader"
  ├─ availability()    → Available/Unavailable（turbojpeg 查 dlopen 结果；image_reader 恒 Available）
  ├─ supports(format)  → turbojpeg: format == Jpeg；image_reader: 恒 true
  └─ try_decode(req)   → Result<ImageDecodeOutcome>
                          ├─ Decoded(DecodedImage)     成功
                          └─ NotHandled(BackendDecline) 拒绝（唯一可回退的结果）
BackendDecline = Unavailable | UnsupportedFormat(fmt) | DecodeFailed
```

关键设计：`ImageDecodeOutcome` 只有 `Decoded` 能触发后续流程，`NotHandled` 是**唯一**允许回退的结果；任何 `Err`（限额、分配失败）都会终止整个解码——注释里写明 "Resource-limit and allocation failures must be returned as errors so another backend cannot bypass them"。

#### 4.2.3 源码精读

trait 与结果类型：

[lib/llm/src/preprocessor/media/decoders/image/backends.rs:L103-L138](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/media/decoders/image/backends.rs#L103-L138) — `BackendDecline` 三种拒绝原因（含 `Display` 实现）、`ImageDecodeOutcome`（带"只有 NotHandled 可回退"的注释）、`ImageDecodeBackend` trait 四方法。

公共结果结构与防御性校验：

[lib/llm/src/preprocessor/media/decoders/image/backends.rs:L69-L101](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/media/decoders/image/backends.rs#L69-L101) — `DecodedImage::new` 在构造时就调 `limits.validate_output` 并断言 `pixels.len() == H*W*C`，任何后端想塞进一个超限或长度错的缓冲区都会在这里被抓。

两个后端以静态单例暴露：

[lib/llm/src/preprocessor/media/decoders/image/backends.rs:L17-L26](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/media/decoders/image/backends.rs#L17-L26) — `image_reader_backend()` / `turbojpeg_backend()` 返回 `&'static dyn ImageDecodeBackend`，无状态、零开销分发。

turbojpeg 后端实现（窄门）：

[lib/llm/src/preprocessor/media/decoders/image/backends/turbojpeg.rs:L15-L50](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/media/decoders/image/backends/turbojpeg.rs#L15-L50) — 先查 `supports`（只 JPEG）、再查 `availability`（dlopen 是否成功），顺序有讲究：不支持格式时连可用性都不查（有专门测试锁定，见 [backends.rs:L186-L200](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/media/decoders/image/backends.rs#L186-L200)）；`decode_jpeg` 返回 `Ok(None)` 的所有情形统一映射为 `NotHandled(DecodeFailed)` 交给编排层裁决。

[lib/llm/src/preprocessor/media/decoders/image/backends/turbojpeg.rs:L52-L65](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/media/decoders/image/backends/turbojpeg.rs#L52-L65) — 通道数收敛：turbojpeg 只产出 1（灰度 L8）或 3（RGB8）通道，其它通道数直接报错（turbojpeg 路径不可能产生它，属于防御性断言）。

image_reader 后端实现（广门）：

[lib/llm/src/preprocessor/media/decoders/image/backends/image_reader.rs:L29-L56](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/media/decoders/image/backends/image_reader.rs#L29-L56) — 把 Dynamo 的限额翻译成 `image::Limits` 挂到 reader 上，按 `channel_count` 分派到 `into_luma8/into_luma_alpha8/into_rgb8/into_rgba8`，支持 1/2/3/4 通道——这正是它能兜住 CMYK JPEG 的原因（`image` crate 会先转 RGB 再输出 3 通道）。

#### 4.2.4 代码实践

1. **实践目标**：用源码回答"什么输入走哪个后端"，产出一张决策表。
2. **操作步骤**：
   - 通读 [backends.rs](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/media/decoders/image/backends.rs) 与两个后端文件；
   - 运行后端层单测：`cargo test -p dynamo-llm backends::`（含 `turbojpeg_declines_unsupported_format_before_availability_check`、`decoded_image_validates_buffer_length_and_limits`）；
   - 填表（示例行已给出）：

| 输入 | libturbojpeg 已装 + 默认开关 | 未装 libturbojpeg |
|------|------|------|
| RGB JPEG | turbojpeg | image_reader（回退） |
| 灰度 JPEG | turbojpeg（输出 L8, 1 通道） | image_reader |
| CMYK JPEG | image_reader（turbojpeg 拒绝） | image_reader |
| PNG / WebP / BMP | image_reader | image_reader |

3. **需要观察的现象**：单测全绿；表格中"CMYK 一行 turbojpeg 拒绝"的依据能在 4.3 的源码里指出具体行。
4. **预期结果**：任何输入在任何环境下都有唯一确定的执行路径，无未定义分支。
5. 若本机未装 libturbojpeg，与 turbojpeg 相关测试会打印 skipping 而非失败——这也是 `DYNAMO_REQUIRE_LIBJPEG_TURBO_TEST` 存在的意义（设了它，缺库即 panic）。

#### 4.2.5 小练习与答案

**练习 1**：`ImageDecodeBackend` 为什么把 `availability()` 和 `supports()` 分成两个方法，而不是合成一个"能不能解这个输入"？
**答案**：两者语义不同且检查顺序有信息量：`supports` 是纯格式判断（不看环境），`availability` 是环境判断（dlopen 是否成功）。编排层用 `supports(format)` 先过滤掉非 JPEG——这样"没装库"只影响 JPEG 输入的路径，PNG 永远不会因为缺库而产生额外的拒绝/回退噪声；测试也据此锁定了"不支持格式先于可用性检查"的顺序。

**练习 2**：`ImageReaderBackend::try_decode` 里 `image::Limits::no_limits()` 后又逐项赋值，为什么？
**答案**：`no_limits()` 造出一个全无限的基础对象，再按 Dynamo 自己的 `ImageDecoderLimits` 显式打开宽/高/alloc 三项。这保证 `image` crate 未来的默认限额变化不会悄悄改变 Dynamo 的守门行为——限额只由 Dynamo 配置决定。

**练习 3**：如果让你加一个 `NvJpegBackend`（GPU 硬解），最少要动哪些文件？
**答案**：新增 `backends/nvjpeg.rs` 实现 `ImageDecodeBackend` 四方法，在 `backends.rs` 注册静态单例和访问函数；若要让它参与选择，还需在 `image.rs` 的 `decode()` 里调整分派顺序（比如 nvjpeg → turbojpeg → image_reader 的三级链）。`DecodedImage`/`BackendDecline` 等类型无需改动——这正是 trait 抽象的价值。

### 4.3 FFI 封装：jpeg_turbo.rs 的 dlopen 与 CMYK 边界

#### 4.3.1 概念说明

`jpeg_turbo.rs` 是唯一与 C 库对话的文件。它解决四个问题：

1. **可选依赖**：用 `dlopen` 而非编译期链接，缺库的镜像也能运行（回退）；
2. **头解析先行**：先 `tjDecompressHeader3` 拿到宽高/子采样/色彩空间，再决定要不要分配输出缓冲——限额检查发生在分配**之前**；
3. **CMYK/YCCK 边界**：TurboJPEG 无法把 CMYK/YCCK JPEG 直接转成 RGB，必须在分配前"拒绝"（返回 `Ok(None)`），让 image_reader 处理；
4. **RAII 生命周期**：`TurboJpegHandle` 在 Drop 时调 `tjDestroy`，任何提前返回都不泄漏 C 句柄。

返回值约定值得记住：`Ok(None)` = "我不处理，请回退"；`Err` = "限额/分配被拒，不许回退"。这条约定与 4.2 的 `ImageDecodeOutcome` 语义一一对应。

#### 4.3.2 核心流程

```
decode_jpeg(bytes, max_w, max_h, max_alloc)
  ├─ is_jpeg? FF D8 FF 魔数，不是 → Ok(None)
  ├─ turbojpeg() 全局 OnceLock 里取库，缺 → Ok(None)
  ├─ tjDecompressHeader3 → (w, h, subsamp, colorspace)
  │    失败或 w/h ≤ 0 → Ok(None)
  ├─ colorspace ∈ {TJCS_CMYK, TJCS_YCCK} → Ok(None)   ← 分配前拒绝
  ├─ 宽高超限 → bail!(Err)                             ← 不可回退
  ├─ colorspace == TJCS_GRAY ? (TJPF_GRAY, 1ch) : (TJPF_RGB, 3ch)
  ├─ nbytes = w*h*ch（checked_mul），溢出/超 max_alloc → bail!
  ├─ Vec::try_reserve_exact(nbytes) 失败 → bail!
  ├─ tjDecompress2(..., TJFLAG_LIMITSCANS) rc≠0 → Ok(None)
  └─ buf.set_len(nbytes) → Ok(Some(DecodedJpeg))
```

#### 4.3.3 源码精读

模块文档明确写出设计动机：

[lib/llm/src/preprocessor/media/jpeg_turbo.rs:L4-L9](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/media/jpeg_turbo.rs#L4-L9) — "The shared library is loaded with `dlopen` instead of linked at build time so Dynamo keeps working in images that do not install libturbojpeg."

dlopen 候选与符号解析：

[lib/llm/src/preprocessor/media/jpeg_turbo.rs:L96-L136](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/media/jpeg_turbo.rs#L96-L136) — 依次尝试 `libturbojpeg.so.0` / `libturbojpeg.so` / macOS 变体；只解析 `tjInitDecompress`、`tjDecompressHeader3`、`tjDecompress2`、`tjDestroy` 四个符号；结果缓存在 `OnceLock`，`available()` 由此而来（并经 [media.rs:L19-L22](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/media.rs#L19-L22) 导出给基准用）。

头解析 + CMYK 拒绝（本模块最重要的三行）：

[lib/llm/src/preprocessor/media/jpeg_turbo.rs:L171-L188](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/media/jpeg_turbo.rs#L171-L188) — `tjDecompressHeader3` 填出四个出参后，`matches!(colorspace, TJCS_CMYK | TJCS_YCCK)` 直接 `return Ok(None)`，注释写明 "Decline before allocating the RGB output so ImageReader handles them"。

限额检查（发生在任何分配之前）：

[lib/llm/src/preprocessor/media/jpeg_turbo.rs:L190-L226](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/media/jpeg_turbo.rs#L190-L226) — 宽高检查 `bail!`；`checked_mul` 算 \(nbytes = w \times h \times c\)，溢出与超 `max_alloc` 均 `bail!`；`try_reserve_exact` 分配失败也 `bail!`——这一整段全部是不可回退的 `Err`。

真正的解码调用：

[lib/llm/src/preprocessor/media/jpeg_turbo.rs:L227-L249](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/media/jpeg_turbo.rs#L227-L249) — `tjDecompress2` 把像素直接写进预分配的 `Vec` 缓冲（`buf.as_mut_ptr()`），成功后 `set_len`；stride 传 0 表示紧密排布；标志位 `TJFLAG_LIMITSCANS`（常量 32768）约束渐进式 JPEG 的扫描处理开销。

**像素奇偶校验（parity）测试**是这套 FFI 正确性的锚点：测试用 Pillow 生成的 JPEG（quality=87/subsampling=2）与 Pillow `convert("RGB")` 的期望像素做**逐字节**比对，保证"换后端不换模型输入"：

[lib/llm/src/preprocessor/media/decoders/image.rs:L530-L565](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/media/decoders/image.rs#L530-L565) — `test_libjpeg_turbo_pixels_match_pil_vllm_decode_when_available`：断言 `decoded.data == expected_rgb`，同时验证经由 `ImageDecoder::decode()` 的完整路径元数据（format=Jpeg、color_type=Rgb8）。

[lib/llm/src/preprocessor/media/decoders/image.rs:L610-L642](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/media/decoders/image.rs#L610-L642) — `pil_parity_fixture`（include_str 加载 [lib/llm/tests/data/media/](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/tests/data/media) 下的 `pil_parity_17x11.jpg.b64` / `.rgb.b64`）与 `cmyk_jpeg_fixture`（`cmyk_2x2.jpg.b64`，Pillow 生成的 2×2 CMYK JPEG）。

CMYK 拒绝时序的专门测试（用 `max_alloc = Some(0)` 证明拒绝发生在限额检查**之前**）：

[lib/llm/src/preprocessor/media/decoders/image.rs:L568-L582](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/media/decoders/image.rs#L568-L582) — `test_libjpeg_turbo_declines_cmyk_before_output_allocation`：若 CMYK 走到了 RGB 输出分配，max_alloc=0 必然 `Err`；测试断言得到的是 `Ok(None)`，即拒绝先于分配。

#### 4.3.4 代码实践

1. **实践目标**：亲手制造一个 CMYK JPEG，确认它在 Dynamo 里只能走 image_reader，并理解"拒绝先于分配"。
2. **操作步骤**：
   - 写一个小脚本（**示例代码**，非项目原有）：
     ```python
     from PIL import Image
     img = Image.new("CMYK", (2, 2), (0, 82, 156, 8))  # 青/品红/黄/黑
     img.save("/tmp/cmyk_2x2.jpg", quality=90)          # JPEG 本身支持 CMYK
     ```
   - 阅读仓库自带的对照物 `lib/llm/tests/data/media/cmyk_2x2.jpg.b64` 与 [image.rs:L568-L582](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/media/decoders/image.rs#L568-L582)；
   - 运行三个边界测试：`cargo test -p dynamo-llm test_libjpeg_turbo -- --nocapture`（覆盖 parity、CMYK、灰度形状三个用例）。
3. **需要观察的现象**：本机装有 libturbojpeg 时三个测试执行（无 skipping）；CMYK 用例断言 `decoded.is_none()`；灰度用例断言输出 shape 为 `[9, 8, 1]`（[image.rs:L585-L608](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/media/decoders/image.rs#L585-L608)）。
4. **预期结果**：CMYK 与灰度在 turbojpeg 路径上的行为差异（一个回退、一个直出 1 通道）被测试钉死；未装库时打印 skipping（除非设了 `DYNAMO_REQUIRE_LIBJPEG_TURBO_TEST`，那会 panic——正是该环境变量的用途）。
5. 上述测试运行结果：**待本地验证**（取决于本机是否安装 libturbojpeg）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `decode_jpeg` 里 CMYK 返回 `Ok(None)` 而宽高超限返回 `Err`？
**答案**：CMYK 是"能力边界"——TurboJPEG 处理不了但 image_reader 能，属于可回退的拒绝；宽高超限是"安全策略"——Dynamo 明令禁止这个尺寸，任何后端都不许解，回退绕过会架空限额。二者的区分正是 4.2 中 `NotHandled` vs `Err` 的语义。

**练习 2**：`decode_jpeg` 已经在 [L191-L215](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/media/jpeg_turbo.rs#L191-L215) 检查过限额，`DecodedImage::new` 为什么还要再校验一次？
**答案**：纵深防御（defense in depth）。`validate_output` 是编排层的公共契约，任何后端（含未来的 nvJPEG）构造 `DecodedImage` 都必须过它；turbojpeg 后端的第二次校验还验证了缓冲区长度与宣称的宽高通道数一致（`pixels.len() == expected_len`），能抓住 FFI 层 `set_len` 与实际解码尺寸不符这类错误。

**练习 3**：`TurboJpegHandle` 的 `Drop` 实现为什么安全？如果去掉它会怎样？
**答案**：句柄是 `NonNull<c_void>`，RAII 守卫唯一拥有它，任何提前 `return`（包括 `bail!` 走的错误路径）都会触发 Drop 调 `tjDestroy`；结构体还借出 `&TurboJpeg`，保证 `Library`（持有 C 符号）活得比句柄久。去掉它，每次提前返回都会泄漏 TurboJPEG 的解码句柄及其内部缓冲区——高并发图片流下内存会持续上涨。

### 4.4 Python 侧开关与并行解码路径

#### 4.4.1 概念说明

Rust 解码器由 Python 侧配置"激活"。链路是：worker 命令行加 `--frontend-decoding` → worker 注册模型时把 `MediaDecoder`/`MediaFetcher` 配置写进模型部署卡 → 前端的 OpenAI 预处理器据此构造 `MediaLoader` → 请求到达时抓取 + 并发解码 + NIXL 注册。注意方向：**开关加在 worker 上而不是 frontend 上**，因为解码能力是 worker 在注册时"广播"出去的。

"并行"有两层，都在本讲的源码范围内：

1. **请求内并行**：一条请求的多张图片用 `futures::future::join_all` 同时抓取+解码；
2. **解码卸载并行**：`decode_async` 把重活丢进 rayon CPU 线程池（`tokio_rayon::spawn`），不阻塞 tokio 异步线程。

#### 4.4.2 核心流程

```
worker: --frontend-decoding
  → media_config.create_frontend_media_config(True)
      → MediaDecoder().enable_image({"limits": {"max_alloc": 128MB}})
      → enable_frontend_video_decoding(...)   # 无 media-ffmpeg 特性则警告
      → MediaFetcher().timeout_ms(30000) + DYN_MM_ALLOW_INTERNAL 门控
  → 注册进 MDC → 前端构造 MediaLoader
请求到达 preprocessor:
  收集所有 image_url → fetch_tasks
  → join_all(fetch_tasks.map(loader.fetch_and_decode_media_part))   ← 第一层并行
       └─ SSRF 检查 → HTTP 拉取/base64 解码
          → decoder.with_runtime(media_io_kwargs)   # 限额仍以 MDC 为准
          → decode_async → tokio_rayon::spawn(rayon 池解码 + content_hash)  ← 第二层并行
  → 像素张量 → NIXL 注册 → RDMA 描述符（见 u8-l9）
```

#### 4.4.3 源码精读

Python 侧的解码器选项（与 Rust 默认值 128 MB 精确对齐）：

[components/src/dynamo/common/utils/media_decoder.py:L12-L21](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/components/src/dynamo/common/utils/media_decoder.py#L12-L21) — `DEFAULT_FRONTEND_IMAGE_DECODER_MAX_ALLOC = 128 * 1024 * 1024`，`build_frontend_image_decoder_options` 产出 `{"limits": {"max_alloc": ...}}`，正是 media README 里 `decoder.enable_image(...)` 的参数形状。

video 开关的优雅降级：

[components/src/dynamo/common/utils/media_decoder.py:L51-L62](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/components/src/dynamo/common/utils/media_decoder.py#L51-L62) — `enable_frontend_video_decoding` 用 `getattr` 探测绑定是否带 `enable_video`（由 Rust `media-ffmpeg` feature 决定），没有就打警告返回。

vLLM worker 侧的总装：

[components/src/dynamo/vllm/multimodal_utils/media_config.py:L16-L33](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/components/src/dynamo/vllm/multimodal_utils/media_config.py#L16-L33) — `create_frontend_media_config(enabled)`：关闭时返回 `(None, None)`；开启时组装 decoder + fetcher（30 秒超时；`DYN_MM_ALLOW_INTERNAL=1` 才允许直连 IP/端口——与 u4-l3 的 SSRF 防线衔接）。

`enable_image` 穿过 PyO3 边界：

[lib/bindings/python/rust/llm/preprocessor.rs:L24-L30](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/bindings/python/rust/llm/preprocessor.rs#L24-L30) — Python dict 经 `pythonize::depythonize` 反序列化成 `ImageDecoder` 配置；由于 `enable_libjpeg` 被 `#[serde(skip)]`，dict 里**放不进去**这个字段（`deny_unknown_fields` 会拒绝）——后端选择只能靠环境变量，这是有意设计。

`Decoder::decode_async`（第二层并行的落点）：

[lib/llm/src/preprocessor/media/decoders.rs:L18-L38](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/media/decoders.rs#L18-L38) — trait 默认实现：轻量 clone 配置 → `tokio_rayon::spawn` 把 `decode` 与 `compute_content_hash` 一起丢进 rayon 池。注释点明 "compute heavy -> rayon"。`MediaDecoder` 聚合结构（L43-L59）同时挂 image/video 两个可选解码器。

请求内第一层并行：

[lib/llm/src/preprocessor.rs:L2950-L2957](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor.rs#L2950-L2957) — 收集完 `fetch_tasks` 后 `futures::future::join_all(...)` 并发执行 `fetch_and_decode_media_part`；任一失败则整个请求失败（L2959-L2961）。

单个媒体部件的处理（SSRF → 拉取 → 解码）：

[lib/llm/src/preprocessor/media/loader.rs:L544-L564](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/media/loader.rs#L544-L564) — 图片分支：MDC 未启用图片则报 "Model does not support image inputs"；`check_if_url_allowed_with_dns` 做 SSRF 检查；`EncodedMediaData::from_url` 处理 http(s) 与 data: URL（data URL 保持 base64，最终由 `into_bytes` 解开，见 [common.rs:L52-L58](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/src/preprocessor/media/common.rs#L52-L58)）；`with_runtime` 合并请求级参数但限额以 MDC 为准。

容器镜像层（系统依赖）：

[container/templates/dynamo_runtime.Dockerfile:L89-L92](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/container/templates/dynamo_runtime.Dockerfile#L89-L92) — apt 安装 `libturbojpeg` 并用 `ldconfig -p | grep -q 'libturbojpeg.so.0'` 自检；vLLM/SGLang/TensorRT-LLM 各 runtime 模板同样安装（如 [sglang_runtime.Dockerfile:L27-L32](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/container/templates/sglang_runtime.Dockerfile#L27-L32)）。media README 同时警告：独立 frontend 镜像（`Dockerfile.frontend`）不含 NIXL/UCX 与 libturbojpeg，前端解码不被支持。

#### 4.4.4 代码实践

1. **实践目标**：整理前端解码的完整开关清单，并确认"后端选择不进配置"这一约束。
2. **操作步骤**：
   - 通读 [media_config.py](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/components/src/dynamo/vllm/multimodal_utils/media_config.py) 与 [media_decoder.py](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/components/src/dynamo/common/utils/media_decoder.py)，在 Python 里（**示例代码**）验证：
     ```python
     import os, serde_json  # 或直接读 Rust 测试结论
     from dynamo.llm import MediaDecoder
     d = MediaDecoder(); d.enable_image({"limits": {"max_alloc": 1 << 27}})
     # 尝试 d.enable_image({"enable_libjpeg": False}) 应报 unknown field —— 待本地验证
     ```
   - 写出清单（参考答案见练习 1）。
3. **需要观察的现象**：`enable_image` 只接受 `limits`（及未来解码参数）；含 `enable_libjpeg` 的 dict 被拒绝。
4. **预期结果**：三个环境变量 + 一个 CLI 开关各司其职（见练习 1 表）。
5. PyO3 行为：**待本地验证**；Rust 侧对应测试 `test_libjpeg_selection_is_not_media_config` 已在 4.1 实践中覆盖同一约束。

#### 4.4.5 小练习与答案

**练习 1**：列出前端图像解码相关的全部开关及作用域。
**答案**：

| 开关 | 层 | 作用 |
|------|------|------|
| `--frontend-decoding`（worker 命令行） | Python/部署 | 启用前端解码，worker 注册时广播 decoder 配置；不要加在 `dynamo.frontend` 上 |
| `DYN_MM_ENABLE_LIBJPEG`（frontend 进程） | Rust | `0`/`false` 关掉 turbojpeg 快路径，默认开 |
| `DYN_MM_ALLOW_INTERNAL`（worker 进程） | Python | `1` 允许抓取直连 IP/端口（放宽 SSRF 防线） |
| `DYNAMO_REQUIRE_LIBJPEG_TURBO_TEST`（测试/CI） | Rust | 禁止回退：本应落 image_reader 的 JPEG 直接报错 |
| `DYN_MULTIMODAL_LOADER_CACHE_GB`（frontend 进程） | Rust | 前端媒体缓存预算（缓存解码+注册后的描述符），默认 0 关闭 |
| `DYN_MM_VIDEO_NUM_FRAMES`（frontend 进程） | Python | 视频默认采样帧数（默认 32，非图片但同文件） |

**练习 2**：`decode_async` 为什么要 clone 一份 decoder 再进 rayon？
**答案**：注释写明是 "light clone (only config params)"——decoder 只携带限额与开关等小配置，clone 廉价；clone 进闭包才能把 `'static` 的值交给 rayon 线程池（不借用 async 任务的栈），同时避免多线程共享同一实例的可变借用问题。重解码本身则在池内并行。

**练习 3**：如果 worker 没加 `--frontend-decoding`，请求里的 `image_url` 会怎样？
**答案**：`create_frontend_media_config(False)` 返回 `(None, None)`，MDC 无 decoder，前端 `MediaLoader` 不构造（preprocessor 里 `media_loader` 为 None）；图片 URL 走"透传"路径原样发给后端，由引擎里的 Python（PIL）解码——即回到传统架构。`fetch_and_decode_media_part` 的 "Model does not support image inputs" 报错只在"有 loader 但 MDC 未启用 image"这种不一致时出现。

### 4.5 基准测试：media_decode benchmark

#### 4.5.1 概念说明

一个"快多少"的claim需要可复现的测量。这套基准由两层组成：

- **入口脚本** `benchmarks/multimodal/media_decode/run_image.sh`：设好两个环境变量（开 4K 并发扫描 + 强制要求 libturbojpeg），透传 Criterion 参数；
- **Criterion 实现** `lib/llm/benches/image_decode.rs`：两个基准组——单张 2400×1080 JPEG 的时延，和 100 张 3840×2160 JPEG 在 c1/c8/c32 三档 rayon 并发下的吞吐。

脚本放在 `benchmarks/` 而 criterion 目标留在 crate 里，是仓库的刻意分工："Rust Criterion 实现留在所属 crate，脚本只负责设置对比环境"。

#### 4.5.2 核心流程

```
run_image.sh
  export RUN_IMAGE_DECODE_SWEEP=1          # 打开 4K 并发组（默认跳过）
  export DYNAMO_REQUIRE_LIBJPEG_TURBO_TEST=1  # 缺库即 panic，不许悄悄测兜底
  exec cargo bench -p dynamo-llm --bench image_decode -- "$@"

bench 内部：
  libjpeg_turbo_available_or_skip()  → 缺库且未 require 则跳过
  image_decoders() = [
    ("image_reader",     ImageDecoder + with_libjpeg_for_benchmark(false)),
    ("libjpeg_turbo",    ImageDecoder + with_libjpeg_for_benchmark(true)),
  ]
  组1 image_decode_jpeg_2400x1080：iter_batched 单张时延，吞吐=压缩字节数
  组2 image_decode_jpeg_3840x2160_batch_100：rayon 线程池 c1/c8/c32，
      Flat 采样、sample_size=10、吞吐=元素数(张)
```

注意 `with_libjpeg_for_benchmark` 是个 `#[doc(hidden)]` 的测试/基准专用 setter——它必须存在，恰恰因为 `enable_libjpeg` 不在序列化配置里（4.1/4.4 的设计回声）。

#### 4.5.3 源码精读

入口脚本：

[benchmarks/multimodal/media_decode/run_image.sh:L12-L15](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/benchmarks/multimodal/media_decode/run_image.sh#L12-L15) — 两个 export 加 `exec cargo bench`，`"$@"` 允许透传基准名过滤或 `--save-baseline`。

被测对象与并发档位：

[lib/llm/benches/image_decode.rs:L15-L20](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/benches/image_decode.rs#L15-L20) — 环境变量名与三档并发 `[1, 8, 32]`、4K 尺寸、批大小 100。

[lib/llm/benches/image_decode.rs:L122-L133](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/benches/image_decode.rs#L122-L133) — 两个命名解码器，唯一差异就是 libjpeg 开关，保证对比公平。

单张时延组：

[lib/llm/benches/image_decode.rs:L22-L42](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/benches/image_decode.rs#L22-L42) — `iter_batched` 每轮重新构造输入（防缓存失真），`Throughput::Bytes` 让 Criterion 顺带报 MB/s。

并发扫描组（用显式 rayon 池模拟"CPU worker pool"）：

[lib/llm/benches/image_decode.rs:L44-L94](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/benches/image_decode.rs#L44-L94) — 对每个解码器 × 每档并发，`ThreadPoolBuilder::new().num_threads(c)` 建池后 `pool.install(|| inputs.into_par_iter().for_each(decode))`；`SamplingMode::Flat` + `sample_size(10)` 适合长耗时基准。

缺库守卫与确定性测试图：

[lib/llm/benches/image_decode.rs:L96-L120](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/benches/image_decode.rs#L96-L120) — `libjpeg_turbo_available_or_skip`（require 时 panic）；`make_jpeg` 用 `ImageBuffer::from_fn` 生成确定性像素模式、quality=87 编码——与 parity fixture 的编码参数同档，结果可复现。

结果解读约定见 [benchmarks/multimodal/media_decode/README.md:L21-L38](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/benchmarks/multimodal/media_decode/README.md#L21-L38)：输出在 `target/criterion/`；4K 扫描在 Cargo bench 层是 opt-in，`cargo test --all-targets` 不会在 CI 里跑它。

#### 4.5.4 代码实践（本讲主实践）

1. **实践目标**：量化 turbojpeg 与 image_reader 两条路径的单张时延差与并发扩展性，并产出后端选择/开关清单。
2. **操作步骤**：
   ```bash
   # 0) 安装运行时库（Debian/Ubuntu；容器镜像里已预装）
   sudo apt-get install -y libturbojpeg0   # 提供 libturbojpeg.so.0
   # 1) 跑基准（脚本会强制 libturbojpeg 可用并打开 4K 扫描）
   ./benchmarks/multimodal/media_decode/run_image.sh
   # 2) 只跑单张组 / 保存基线对比
   ./benchmarks/multimodal/media_decode/run_image.sh image_decode_jpeg_2400x1080
   ./benchmarks/multimodal/media_decode/run_image.sh --save-baseline before
   # 3) 结果在 target/criterion/ 下，浏览器打开 index.html 或读 .json
   ```
   然后完成两份书面产出：
   - **对比表**：两条路径在 c1/c8/c32 的每批耗时与每张均摊耗时；
   - **清单**（依据 4.1–4.4 源码）：后端优先级（turbojpeg → image_reader，仅当格式为 JPEG 且开关开且库可用）、禁用开关（`DYN_MM_ENABLE_LIBJPEG=0`；缺库自动降级并打一次警告）、CMYK 路径（头解析发现 `TJCS_CMYK/TJCS_YCCK` → 分配前 `Ok(None)` → 回退 image_reader，由 `image` crate 转 RGB 输出 3 通道）。
3. **需要观察的现象**：单张组 `libjpeg_turbo` 的耗时显著低于 `image_reader`（SIMD 的意义）；并发组从 c1→c8 接近线性加速，c8→c32 受核数/内存带宽限制趋于平缓；两条路径的输出 shape 相同（`[H, W, 3]`）。
4. **预期结果**：得到一份带数字的对比报告 + 一份准确的开关/回退清单。具体加速比**待本地验证**（依赖机器与 libturbojpeg 版本，不预设倍数）。
5. 无 libturbojpeg 且无法安装时：脚本会因 `DYNAMO_REQUIRE_LIBJPEG_TURBO_TEST=1` 直接 panic——这本身就是一次对 4.1 守卫逻辑的验证；此时可退而运行 `cargo test -p dynamo-llm test_libjpeg` 与 `cargo bench -p dynamo-llm --bench image_decode`（后者会打印 skipping）。

#### 4.5.5 小练习与答案

**练习 1**：为什么并发组要自己 `ThreadPoolBuilder::new().num_threads(c)` 建池，而不是直接 `into_par_iter()`？
**答案**：默认 rayon 池线程数等于全局 CPU 数，测不出并发度的影响。显式建 1/8/32 线程的池，才能分别度量"单线程解码""8 路并行""32 路并行"三档下每张图的均摊成本，回答"前端解码在线程池上扩展性如何"这个问题——这也复现了生产路径中 `decode_async` 经 `tokio_rayon` 跑在 rayon 池上的执行模型。

**练习 2**：基准里两个解码器都是 `ImageDecoder::default()`，区别只在 `with_libjpeg_for_benchmark(bool)`。为什么不直接对比"turbojpeg crate vs image crate"两个底层库？
**答案**：基准的目的是度量**Dynamo 配置下的端到端解码行为**（含格式嗅探、限额校验、结果包装），而不是裸库极限。用同一个 `ImageDecoder::decode()` 入口、只拨动一个开关，测得的差值才真实反映"用户把 `DYN_MM_ENABLE_LIBJPEG` 从 1 改成 0"会经历的代价。

**练习 3**：`iter_batched` 每轮都 `jpeg.clone()` 重建输入，会不会让测量失真？
**答案**：会带来一点输入构造开销，但这正是 `iter_batched` + `BatchSize::SmallInput/LargeInput` 的分工——准备工作（clone）被计时器排除在测量迭代之外，只有 `decoder.decode(data)` 计入。排除准备段是为了避免"上一轮解码结果留在缓存里"的失真，代价是可控的内存拷贝。

## 5. 综合实践

**任务：为你的环境写一份《前端图像解码决策手册》。**

1. **决策矩阵**：用 Pillow（**示例代码**）生成 4 个文件——RGB JPEG、灰度 JPEG、CMYK JPEG、PNG：
   ```python
   from PIL import Image
   Image.new("RGB", (64, 48), (10, 20, 30)).save("/tmp/rgb.jpg")
   Image.new("L", (64, 48), 128).save("/tmp/gray.jpg")
   Image.new("CMYK", (64, 48), (0, 82, 156, 8)).save("/tmp/cmyk.jpg")
   Image.new("RGB", (64, 48), (1, 2, 3)).save("/tmp/x.png")
   ```
   对每个文件，在"libturbojpeg 可用 + `DYN_MM_ENABLE_LIBJPEG=1`"与"库不可用（或 `=0`）"两种环境下，指出：走哪个后端（引用 backends.rs / image.rs / jpeg_turbo.rs 的具体行为依据）、输出通道数、若超 `max_alloc` 会发生什么。用一张表呈现 8 种组合。
2. **测量**：按 4.5.4 跑 `run_image.sh`，把单张时延与 c1/c8/c32 三档数据填进对比表；若无 libturbojpeg，记录脚本 panic 行为并解释它与 `DYNAMO_REQUIRE_LIBJPEG_TURBO_TEST` 的关系。
3. **验证**：跑 `cargo test -p dynamo-llm test_libjpeg`（parity/CMYK/灰度/限额四组用例），把每个测试对应到你矩阵里的哪一行。
4. **收尾追问**（写进手册末尾）：为什么 CMYK 必须在分配输出缓冲**之前**拒绝？为什么限额绝不允许经回退绕过？如果你的自定义镜像没装 libturbojpeg，用户会看到什么现象（提示：一次性 warning + 自动降级，前端吞吐回到 image_reader 水平）？

## 6. 本讲小结

- **选择顺序**：`decode()` 先 `guess_format`，JPEG 且开关开且 turbojpeg 可用才走快路径，否则/被拒时落 `image::ImageReader`；后者 `supports` 恒真，是最后的必答后端。
- **两类失败**：`NotHandled(BackendDecline)`（不支持/不可用/解不动）可回退；`Err`（宽高/分配超限）不可回退——限额不能被换后端架空，这条约定贯穿编排层、trait 结果类型和 FFI 返回值三层。
- **FFI 设计**：dlopen 四个 TurboJPEG 符号使缺库可降级；`tjDecompressHeader3` 先行让 CMYK/YCCK 在分配前 `Ok(None)` 拒绝、限额在分配前 `bail!`；RAII 句柄防泄漏；与 Pillow 的逐字节 parity fixture 保证换后端不换模型输入。
- **Python 接线**：`--frontend-decoding` 在 worker 侧生成 `MediaDecoder`/`MediaFetcher` 配置注册进 MDC；`enable_libjpeg` 被 `#[serde(skip)]` 挡在配置面之外，只能用 `DYN_MM_ENABLE_LIBJPEG` 控制。
- **并行两层**：请求内 `join_all` 并发处理多张图，`decode_async` 经 `tokio_rayon` 把重解码卸载到 rayon CPU 池。
- **可测量**：`run_image.sh` + Criterion 双组基准（单张时延 / 4K×100 张 c1-c32 扫描），并以 `DYNAMO_REQUIRE_LIBJPEG_TURBO_TEST` 防止"悄悄测了兜底"。

## 7. 下一步学习建议

- **本讲下游**：解码出的像素张量如何 `into_rdma_descriptor` 注册 NIXL、编码 worker 如何按 canonical `content_hash` 做嵌入缓存、E/P/D 拓扑如何传播 `FirstResponseGuard`——全部在 **u8-l9（前端图像解码与 E/P/D 多模态分离）**展开，那是本讲的直接续篇。
- **横向扩展**：想给路由/后端写插件或理解 filter–score–pick 策略框架，可跳到 u6-l5；那里同样大量使用"trait + 注册"的抽象手法。
- **性能深化**：仓库 media README 的 TODO 列表（NVDEC 视频硬解、nvJPEG 图像硬解、内存 slab 预分配）是潜在的二次开发选题；阅读 [lib/llm/benches/video_decode.rs](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/lib/llm/benches/video_decode.rs)（需 `media-ffmpeg` feature）可对照学习另一个 Criterion 基准的写法。
- **官方文档**：[parallel-media-decoding](https://github.com/ai-dynamo/dynamo/blob/b4338ab87e90fc6edd496879b80ed045c7339967/docs/fern/pages/use-cases/multimodal-serving/parallel-media-decoding.md) 给出各后端启用矩阵（vLLM/SGLang/TRT-LLM 的 Agg 拓扑）与镜像要求，适合作为部署侧速查。
