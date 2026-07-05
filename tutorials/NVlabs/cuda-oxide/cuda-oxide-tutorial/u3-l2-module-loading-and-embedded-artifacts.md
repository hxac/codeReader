# 模块加载与内嵌制品

## 1. 本讲目标

本讲要回答一个贯穿 cuda-oxide 运行时的核心问题：

> 编译期生成的 PTX（或 cubin/NVVM IR/LTOIR）是**怎样**被装进最终的可执行文件里，运行时又是**怎样**被宿主程序找回来并交给 CUDA 驱动的？此外，编译时定下的**浮点策略（是否允许 FMA 收缩）**又是怎样随这串字节一路传递到最终机器码生成的？

读完本讲，你应当能够：

- 画出 `.oxart` 制品段的二进制 wire 格式，并说出每一段字节的含义，包括 v1 与 v2 两种版本号的区别；
- 解释「锚符号握手（anchor handshake）」为什么是 lib crate 场景下避免 bundle 被链接器裁剪的关键，并追踪它跨 `#[cuda_module]` 宏与 codegen 后端两侧的实现；
- 跟踪运行时从「当前可执行文件」中发现、解析、加载内嵌 bundle 的完整调用链；
- 区分 `cuda-core` 与 `cuda-host` 两层在模块加载上的职责分工；
- 说明 `ArtifactCompileOptions`（FMA 收缩策略）如何从 `CUDA_OXIDE_NO_FMA` 一路流进 bundle 头部、`.options`/`.target` 边车文件，并在运行时二次编译时约束最终 cubin，理解「默认策略为何仍写 v1」的设计取舍。
- 说明为什么含签约 kernel 的 `#[cuda_module]` 会把全部 loader 标记为 `unsafe`，以及这条「一次性绑定证明」如何与 `PreparedLaunch` 受检启动衔接，而未签约模块的 loader 仍是安全的。

本讲承接 [u2-l1](u2-l1-kernel-and-cuda-module-macros.md)（宏生成的 `load()` 三步走）与 [u3-l1](u3-l1-cuda-core-safe-wrappers.md)（`CudaContext`/`CudaModule` 的 RAII 封装），把视角从「类型与句柄」下沉到「字节与链接器契约」。

> **格式演进（PR #326）**：`.oxart` 制品升级到 **v2**——头部新增 `compile-options` 位域，并引入 `ArtifactCompileOptions`（FMA 收缩策略）与 `.options`/`.target` 边车文件，同时**保持对 v1 的向后兼容**。本讲在 4.1 讲格式演进、在 4.5 专门讲解 FMA 策略与边车。这部分代码本轮未变动。
>
> **安全演进（本轮 PR #318，启动契约）**：当一个 `#[cuda_module]` 内含签约 kernel（带 `#[launch_contract]`）时，宏会把该模块**所有** loader（`load`/`load_async`/`load_named`/`load_async_named`/`from_module`）生成为 `unsafe fn`——调用方需一次性证明「绑定的字节确实是本模块声明 ABI/资源语义的产物」，证明之后由它产出的 `PreparedLaunch` 受检启动才是安全的；未签约模块的 loader 仍是安全函数。本讲在 4.4 专门讲解这条 loader 的 unsafe 边界。

## 2. 前置知识

阅读本讲前，建议你已经了解：

- **ELF 目标文件与「段（section）」**：Linux 上的可执行文件由若干段组成。cuda-oxide 把设备代码塞进一个名为 `.oxart` 的自定义数据段，运行时再用解析器把这个段读回来。你只要知道「ELF 文件 = 一堆带名字的段」即可。
- **归档成员（archive member）的惰性提取**：Rust 的 `.rlib` 本质是一个静态归档（`.a`）。链接器在处理归档时**不会**把每个成员都链接进来，只有当某个成员定义了一个能被「未解析引用」命中的符号时，它才会被提取（pull in）。本讲的「锚符号」机制正是建立在这条规则上。
- **`--gc-sections` / dead-strip**：链接器会删除最终二进制中「没人引用」的段以缩减体积。`.oxart` 段如果被当作普通数据，就可能在发布构建中被这样裁掉。
- **CUDA 驱动的模块加载**：`cuModuleLoadData` 接收一段 PTX/cubin 字节，返回一个 `CUmodule` 句柄（在 cuda-oxide 里被安全封装成 `CudaModule`，见 u3-l1）。
- **FMA 与「乘加收缩（contraction）」**：浮点里 `a*b+c` 可以被编译器合并成一条「融合乘加（fused multiply-add, FMA）」指令。FMA 只在最后做一次舍入，精度更高、速度更快，但**结果位级不同**于「先乘后加」。有些数值代码（差分演化、bit-exact 回归测试）要求严格禁用这种隐式合并。`--no-fmad` / `CUDA_OXIDE_NO_FMA=1` 就是关闭收缩的开关。本讲的 v2 制品正是为了让「关闭 FMA」这条策略不丢失地传到最终 cubin。
- **u2-l1 引入的 `load()` 三步走**：anchor 握手 → `load_embedded_module` 解析 PTX → 逐个 kernel 调 `cuModuleGetFunction`。本讲深入第 1、2 步的内部实现。

## 3. 本讲源码地图

| 文件 | 层 | 作用 |
|------|----|------|
| `crates/oxide-artifacts/src/lib.rs` | 制品格式层 | 定义 `.oxart` 段的 wire 格式（含 v1/v2 与 `compile-options` 位域）、`ArtifactCompileOptions`（FMA 策略）与 `.options` 边车文本、序列化/反序列化、把 blob 包成宿主目标文件、定义锚符号 |
| `crates/rustc-codegen-cuda/src/lib.rs` | 编译器后端 | 编译期收集设备代码，按 payload 种类构造 `ArtifactCompileOptions`，调用 `oxide-artifacts` 打包成 `.oxart` 段并产出宿主目标文件，交给 rustc 链接 |
| `crates/rustc-codegen-cuda/src/device_codegen.rs` | 编译器后端 | 读取 `CUDA_OXIDE_NO_FMA` 得出 `allow_fma_contraction`，向下注入流水线 |
| `crates/cuda-macros/src/lib.rs` | 宏层 | `#[cuda_module]` 在生成的 `load_named()` 里发出对锚符号的**引用**完成握手；若模块含签约 kernel（`#[launch_contract]`），则把 `load`/`load_named`/`from_module`（及 async 版本）全部生成为 `unsafe fn`（本轮 #318） |
| `crates/reserved-oxide-symbols/src/lib.rs` | 命名契约 | 定义锚符号的前缀与构造规则，保证后端与宏两侧拼出同一个名字 |
| `crates/cuda-core/src/embedded.rs` | 运行时（底层） | 从「当前可执行文件的字节」中发现 `.oxart` 段、解析成 `OwnedArtifactBundle`（含 `compile_options`）、挑出可加载的 payload |
| `crates/cuda-host/src/embedded.rs` | 运行时（上层） | 在 `cuda-core` 之上挑选 payload、用 `bundle.compile_options` 决定 NVVM/LTOIR 二次编译的 FMA 策略、交给驱动加载成 `CudaModule` |
| `crates/cuda-host/src/ltoir.rs` | 运行时（上层） | 把 `allow_fma_contraction` 翻译成 libNVVM/nvJitLink 的 `-fma=1/0` 选项 |
| `crates/mir-importer/src/pipeline.rs` | 编译器中间层 | NVVM IR 模式下，在 `.ll` 旁写出 `.options` 边车 |

一句话总结分工：`oxide-artifacts` 管「字节怎么排 + 策略怎么编码」，后端 + 宏管「字节怎么进/出二进制」，`cuda-core` 管「从二进制里把字节找出来」，`cuda-host` 管「把字节连同策略变成可执行的 GPU 模块」。

## 4. 核心概念与源码讲解

本讲拆成五个最小模块，对应设备代码「打包（含 FMA 策略）→ 装入二进制 → 运行时找回 → 加载执行 → 策略与边车」的全生命周期。

---

### 4.1 `.oxart` bundle 格式（v1/v2）

#### 4.1.1 概念说明

设备代码（PTX 文本、cubin 二进制、NVVM IR、LTOIR）归根到底是一串字节。要把它们随宿主程序一起发布，最简单的办法就是把这串字节嵌进可执行文件的某个数据段。但光塞进去还不够——运行时需要知道：

- 这串字节是**哪种** payload（PTX 还是 cubin？）；
- 它是为**哪个设备架构**编译的（`sm_80`？`sm_100a`？）；
- 它里面**导出了哪些入口符号**（哪些是 kernel，哪些是普通 `#[device]` 函数？）；
- 它属于**哪个 bundle**（一个二进制里可能并存多个 crate 各自的 bundle）；
- 它带着**什么编译策略**（是否允许 FMA 收缩？——v2 新增）。

`oxide-artifacts` crate 定义了一个与具体加速器后端无关的「制品 bundle」wire 格式来回答这些问题。这个格式被存进名为 `.oxart` 的 ELF 段里（`oxart` = **ox**ide **art**ifact）。设计目标有三点：自描述（带 magic 与版本号）、可串联（一个段里可以背靠背放多个 bundle）、**策略可演化**（v2 在头部预留了 `compile-options` 位域，而默认策略的 bundle 仍写成 v1 以兼容老读取器）。

#### 4.1.2 核心流程

一个 bundle 的内存布局如下（小端字节序）。注意头部 `[24:32]` 这 8 字节：v1 里它是保留零填充，v2 里它是 `compile-options` 位域。

```
┌──────────────── HEADER（固定 32 字节）────────────────┐
│ [ 0: 8]  magic        "OXIDEART"                      │
│ [ 8:10]  version      u16 = 1（默认策略/历史）或 2     │
│ [10:12]  header_len   u16 = 32                        │
│ [12:16]  total_len    u32（整个 blob 的字节数）        │
│ [16:18]  name_len     u16                             │
│ [18:20]  target_len   u16                             │
│ [20:22]  payload_cnt  u16                             │
│ [22:24]  entry_cnt    u16                             │
│ [24:32]  compile_opts u64（v2：策略位域；v1：保留 0）  │
├──────────────── 变长区 ────────────────────────────────┤
│ bundle 名字（name_len 字节）                            │
│ target 字符串（如 "sm_90"，target_len 字节）            │
│ payload_cnt × 24 字节的「payload 记录表」               │
│ entry_cnt   × 24 字节的「entry 记录表」                 │
│ 每个 payload 的名字（8 字节对齐）+ 数据（8 字节对齐）    │
│ 每个 entry 的符号字符串（8 字节对齐）                    │
└────────────────────────────────────────────────────────┘
```

`compile-options` 位域目前只定义了最低位 `OPTION_NO_FMA_CONTRACTION = 1 << 0`：该位为 1 表示「禁用 FMA 收缩」，为 0（默认）表示「允许收缩」。高位保留，遇到未知位读取器会拒绝（见 4.1.3）。

每个 **payload 记录**（24 字节）描述一段设备代码：

| 偏移 | 长度 | 字段 |
|------|------|------|
| 0 | 2 | `kind`（`0x100` PTX / `0x110` NvvmIr / `0x120` Ltoir / `0x200` Cubin） |
| 2 | 2 | 保留（0） |
| 4 | 4 | `data_offset`：数据在 blob 中的绝对偏移 |
| 8 | 4 | `data_len`：数据字节数 |
| 12 | 4 | `name_offset`：payload 名字偏移（如 `"vecadd.ptx"`） |
| 16 | 2 | `name_len` |

每个 **entry 记录**（24 字节）描述一个导出符号：

| 偏移 | 长度 | 字段 |
|------|------|------|
| 0 | 2 | `kind`（`1` Kernel / `2` DeviceFunction） |
| 2 | 2 | `flags`：bit0 = 是否带 `metadata` |
| 4 | 8 | `metadata`：可选的 64 位元数据 |
| 12 | 4 | `symbol_offset`：符号字符串偏移 |
| 16 | 2 | `symbol_len` |

序列化时先写一份占位 header，再追加名字/target/两张定长记录表，再追加所有变长字符串与数据（顺便回填记录表里的偏移），最后回填 header 里的 `total_len`、计数、版本号与 `compile-options` 位域。解析则是严格的逆过程，并对每一步做长度校验，任何截断或非法 magic 都会返回 `ArtifactError` 而非 panic。

**v1 与 v2 的写入选择（关键）**：序列化器在写头部时做一个判断——如果 `compile_options` 等于默认零值（即 FMA 收缩开启，没有任何非默认策略），就写 **v1**；否则写 **v2**。这样默认 bundle 永远是 v1，老读取器能继续读；而携带了「必须遵守」策略（如禁用 FMA）的 bundle 是 v2，老读取器会因为不认识版本号而**拒绝**它，而不是静默忽略掉这条策略。这是「安全胜过无声」的设计。

注意长度约束：计数与长度字段多为 `u16`，偏移与 payload 长度为 `u32`。任何超出都会被 `checked_u16`/`checked_u32` 拒绝，因此格式本身限制了单个 bundle 的规模上限（约 \(2^{16}\) 个 payload、单 payload 约 \(2^{32}\) 字节）。

#### 4.1.3 源码精读

格式常量与 FMA 策略位集中在文件顶部：

[crates/oxide-artifacts/src/lib.rs:14-26](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/oxide-artifacts/src/lib.rs#L14-L26) — 定义了 `.oxart` 段名、`.oxlink`（仅锚符号占位段）段名、8 字节 magic `"OXIDEART"`、`ARTIFACT_VERSION = 2` 与 `LEGACY_ARTIFACT_VERSION = 1`，三组定长尺寸（header 32、payload 记录 24、entry 记录 24），以及 FMA 策略位 `OPTION_NO_FMA_CONTRACTION` 与已知位掩码 `KNOWN_COMPILE_OPTIONS`。

策略位域的封装是 `ArtifactCompileOptions`（详见 4.5），这里先看它如何参与头部读写。序列化的核心是 `build_artifact_blob`：

[crates/oxide-artifacts/src/lib.rs:383-418](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/oxide-artifacts/src/lib.rs#L383-L418) — 先 `validate_spec`（拒绝空名/空 target/空 payload），预留 32 字节 header，追加 bundle 名字与 target，再预留两张记录表的空间；随后遍历每个 payload，先写它的名字串（8 字节对齐）再写数据（8 字节对齐），用 `checked_u32` 把真实偏移回填进记录表。

回填 header 字段，**含版本号选择与 compile-options 位域**：

[crates/oxide-artifacts/src/lib.rs:437-466](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/oxide-artifacts/src/lib.rs#L437-L466) — 写入 magic 后，按「默认策略→v1，否则→v2」选出 `version`（L442-L446），再写 `header_len`、`total_len`、name/target 长度与 payload/entry 计数，最后在 `[24:32]` 写入 `compile_options.bits()`（L466）。注释（L439-L441）明确解释了「为何默认仍写 v1」。

反序列化由 `parse_artifact_section` 驱动，它处理「一个段里串联多个 blob + 末尾零填充」的真实情况：

[crates/oxide-artifacts/src/lib.rs:471-483](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/oxide-artifacts/src/lib.rs#L471-L483) — 循环里先读 `artifact_blob_total_len` 决定切多长；遇到「整段都是 0」的尾部填充则提前 `break`，避免把 ELF 段对齐的零字节误判成坏 blob。

单个 blob 的严格解析，**含 v1/v2 的策略读取差异**：

[crates/oxide-artifacts/src/lib.rs:485-518](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/oxide-artifacts/src/lib.rs#L485-L518) — 校验 magic、版本号（v1 或 v2 都接受，其余 `UnsupportedVersion`）、`header_len`；用 `total_len` 截断 blob 后读计数。注意 L514-L518：**v1 bundle 的 `compile_options` 一律按默认（FMA 收缩开启）处理，忽略那 8 个保留字节**；v2 才用 `from_bits` 解析位域，遇到未知位返回 `UnsupportedCompileOptions`。

payload 与 entry 记录的逐条解析：

[crates/oxide-artifacts/src/lib.rs:537-576](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/oxide-artifacts/src/lib.rs#L537-L576) — 按记录表里的偏移取出名字串与数据；entry 还会读 `flags` 判断是否带 `metadata`。每一步都过 `require_len`/`read_slice`，截断会得到 `Truncated` 而非越界 panic。

锁定向后兼容行为的两个关键测试：

[crates/oxide-artifacts/src/lib.rs:926-944](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/oxide-artifacts/src/lib.rs#L926-L944) — `artifact_blob_round_trips_ptx_payload` 断言默认 PTX bundle 的版本号是 **v1**（L929），且 `fma_contraction_enabled()` 为真（L935）。

[crates/oxide-artifacts/src/lib.rs:988-1009](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/oxide-artifacts/src/lib.rs#L988-L1009) — `legacy_v1_bundle_defaults_to_fma_contraction` 故意把 v1 头部 `[24:32]` 写成非零的 `OPTION_NO_FMA_CONTRACTION`，断言读取器**仍然按默认（FMA on）处理**（新读取器必须忽略 v1 的保留字节，而不是赋予新含义）；`version_2_rejects_unknown_compile_option_bits` 则断言 v2 遇到未知策略位会报 `UnsupportedCompileOptions`。

#### 4.1.4 代码实践

**实践目标**：用 `oxide-artifacts` 的公开 API 亲手构造一个 bundle，再把它读回来，并观察「默认策略→v1、关闭 FMA→v2」的版本差异。

**操作步骤**（这是一个「最小调用示例」，可作为 `oxide-artifacts` 的 doctest 风格练习）：

```rust
// 示例代码：演示 build/parse 往返与版本差异，非项目原有代码
use oxide_artifacts::{
    build_artifact_blob, parse_artifact_blob,
    ArtifactBundleSpec, ArtifactEntrySpec, ArtifactPayloadSpec,
    ArtifactEntryKind, ArtifactPayloadKind, ArtifactCompileOptions,
};

// 默认策略（FMA on）→ 序列化为 v1
let spec_default = ArtifactBundleSpec::new("demo", "sm_90")
    .with_payload(ArtifactPayloadSpec::new(
        ArtifactPayloadKind::Ptx, "demo.ptx", b".version 8.0\n... PTX ...",
    ))
    .with_entry(ArtifactEntrySpec::new("vecadd", ArtifactEntryKind::Kernel));
let blob_v1 = build_artifact_blob(&spec_default).unwrap();

// 关闭 FMA 收缩 → 序列化为 v2
let spec_nofma = ArtifactBundleSpec::new("demo", "sm_90")
    .with_compile_options(ArtifactCompileOptions::new().with_fma_contraction(false))
    .with_payload(ArtifactPayloadSpec::new(
        ArtifactPayloadKind::NvvmIr, "demo.ll", b"... NVVM IR ...",
    ));
let blob_v2 = build_artifact_blob(&spec_nofma).unwrap();
```

**需要观察的现象**：
1. 读 `blob_v1[8..10]`（小端 u16）应等于 `1`（v1）；`blob_v2[8..10]` 应等于 `2`（v2）。
2. `blob_v1[0..8]` 与 `blob_v2[0..8]` 都应等于 `b"OXIDEART"`，`[12..16]` 都等于各自 `blob.len()`。
3. 读回后 `parse_artifact_blob(&blob_v2).unwrap().compile_options.fma_contraction_enabled()` 应为 `false`；而 v1 的始终为 `true`。
4. 把 payload 字节改成空 `b""`，`build_artifact_blob` 应返回 `ArtifactError::EmptyPayload`——印证「空 payload 被拒」。

**预期结果**：往返完全相等，且版本号随策略切换。这正是单元测试 `artifact_blob_round_trips_ptx_payload`（默认→v1）与 `artifact_blob_round_trips_non_ptx_payload_kinds`（关闭 FMA→v2）断言的内容，见 [crates/oxide-artifacts/src/lib.rs:946-985](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/oxide-artifacts/src/lib.rs#L946-L985)。运行该测试：`cargo test -p oxide-artifacts artifact_blob_round_trips`（具体测试是否能在本机通过待本地验证，取决于是否装齐 nightly 工具链）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 magic `"OXIDEART"` 要占满 8 字节，而不是更短的 `"OX"`？
**答案**：8 字节 magic 既能在 `read_artifact_bundles_from_object_bytes` 扫描段时可靠定位 blob 起始，又恰好让 header 前 8 字节自然对齐；过短的 magic 在真实二进制里更容易被随机数据撞中，误判率更高。

**练习 2**：一个 v2 bundle 的 `[24:32]` 字节里出现了当前未定义的高位（如 `1 << 63`），读取器会怎样？为什么要这样设计？
**答案**：`from_bits` 检测到 `bits & !KNOWN_COMPILE_OPTIONS != 0`，返回 `ArtifactError::UnsupportedCompileOptions`。这样设计是为了**前向安全**：未来若新增策略位，旧读取器不会把「带新策略的 bundle」当成「无策略 bundle」静默加载，从而避免用错误的浮点契约生成机器码。`tests::version_2_rejects_unknown_compile_option_bits` 守护这一行为。

**练习 3**：`parse_artifact_blob` 在校验 `total_len > bytes.len()` 时返回什么错误？为什么不 panic？
**答案**：返回 `ArtifactError::Truncated("blob")`。运行时面对的是用户编出的二进制文件，数据可能损坏或不完整，用 `Result` 把错误向上传给宿主程序是安全且可恢复的；panic 会让整个 GPU 程序崩溃。

---

### 4.2 artifact 锚符号（anchor handshake）

#### 4.2.1 概念说明

打包好 `.oxart` 字节只是第一步，真正的难题是：**怎样保证这些字节在最终二进制里不被链接器删掉？**

这里有两种会「丢字节」的场景：

1. **lib crate 走 `.rlib` 归档**：当一个 `#[cuda_module]` 所在的 crate 被编译成库（而不是最终二进制），后端产出的那个「装着 `.oxart` 段的小目标文件」就成了 `.rlib` 归档里的一个成员。如前置知识所述，链接器**只**在「该成员定义的符号能命中某个未解析引用」时才把它提取出来。一个纯数据段、不含任何符号的目标文件，会被链接器**整段忽略**——结果运行时 `load()` 在二进制里找不到任何 `.oxart` 段，报 `ModuleNotFound`。这正是项目历史上 issue #72 的根因。

2. **`--gc-sections` 死代码裁剪**：即便成员被提取进来了，如果整段没有任何东西引用它，发布构建（`-C opt-level=3`）的链接器仍可能把它当作无用段裁掉。

锚符号（anchor）机制同时解决这两点：后端在 `.oxart` 段的开头**定义**一个全局符号 `cuda_oxide_artifact_anchor_246e25db_<pkg>_<ver>`；宏在宿主侧的 `load()` 里**引用**同一个符号。这一「定义—引用」握手让链接器相信该段是「被需要的」，从而既触发归档提取、又抵御 `--gc-sections`。

#### 4.2.2 核心流程

握手分两端，跨同一次 rustc 调用协同：

```
        ┌──────── codegen 后端（定义锚）─────────┐
        │  把 PTX 包成 .oxart 段                    │
        │  在段首定义全局符号                       │
        │     cuda_oxide_artifact_anchor_246e25db_ │
        │        <CARGO_PKG_NAME>_<CARGO_PKG_VER>   │
        │  段上加 SHF_GNU_RETAIN（抗 gc-sections）  │
        └─────────────────┬─────────────────────────┘
                          │ 同一次 rustc 编译
        ┌─────────────────▼ 宏（引用锚）────────────┐
        │  #[cuda_module] 生成的 load() 里插入：      │
        │    unsafe extern "C" {                    │
        │        #[link_name = "<同一个锚名>"]        │
        │        static CUDA_OXIDE_BUNDLE_ANCHOR: u8;│
        │    }                                       │
        │    black_box(addr_of!(ANCHOR))             │
        │  → 产生一条对锚的「未解析引用」             │
        └─────────────────┬──────────────────────────┘
                          │ 链接时
        ┌─────────────────▼ 链接器 ──────────────────┐
        │  宿主代码引用了锚 → 必须提取归档成员        │
        │  + SHF_GNU_RETAIN → 段不被 --gc-sections   │
        │  结果：.oxart 段安全留在最终二进制里        │
        └─────────────────────────────────────────────┘
```

为什么符号名里要带包名与版本？因为同一个依赖图里可能出现**两个不同版本**的同一个 crate（cargo 允许），它们的 bundle 必须各自独立保留，不能互相顶替。符号名里拼进 `CARGO_PKG_NAME` 与 `CARGO_PKG_VERSION`（非法字符统一替换为 `_`）正好让每个 (包, 版本) 组合拿到独一无二的锚。

还有一个 v2 变体：当设置了 owner 过滤环境变量 `CUDA_OXIDE_DEVICE_CODEGEN_CRATE` 时，锚名会额外拼进 crate 目标名与二进制名，避免「未选中的二进制」去满足「选中的库」的引用。后端会同时定义一个 **weak 的 legacy 别名**，让旧版本宏展开（只认 legacy 名）在混合版本构建里仍能链接。

#### 4.2.3 源码精读

锚符号前缀与构造规则定义在命名契约 crate：

[crates/reserved-oxide-symbols/src/lib.rs:117-134](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/reserved-oxide-symbols/src/lib.rs#L117-L134) — 顶部文档完整说明了 issue #72 的来龙去脉：纯数据目标文件不含符号会被归档忽略，`load()` 运行时报 `ModuleNotFound`；解法是后端定义一个带此前缀的全局符号、宏在 `load()` 里读它的地址。注意 `246e25db` 与 kernel 前缀用的是同一个固定哈希后缀（见 u2-l1），表明这些保留符号同属一个内部命名空间。

[crates/reserved-oxide-symbols/src/lib.rs:214-251](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/reserved-oxide-symbols/src/lib.rs#L214-L251) — `artifact_anchor_symbol`（L214）把包名、版本拼进符号，`push_symbol_sanitized`（L253）把所有非 `[A-Za-z0-9_]` 字符替换为下划线，确保结果永远是合法链接器符号（例如 `julia-lib 0.1.0` → `..._julia_lib_0_1_0`）；v2 构造器 `artifact_anchor_symbol_v2`（L228）追加 crate 名与（可选）二进制名。

后端一侧，写设备制品时同时定义锚符号：

[crates/rustc-codegen-cuda/src/lib.rs:834-865](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/lib.rs#L834-L865) — 先 `build_artifact_blob` 得到 `.oxart` 字节，再用 `artifact_anchor_symbol` 算出 legacy 锚名，调用 `oxide_artifacts::build_host_object_for_target(&blob, host_target, Some(&legacy_anchor))` 把字节连同锚符号一起包进宿主目标文件；owner 过滤场景则改用 `build_host_object_for_target_with_legacy_anchor` 同时给出 v2 强符号 + legacy 弱别名。注释（L843-L845）明确指出「没有锚，库 crate 的 bundle 会被 dead-strip，`load()` 在运行时报 ModuleNotFound」。

`.oxart` 段与锚符号如何写进目标文件，是 `oxide-artifacts` 的职责：

[crates/oxide-artifacts/src/lib.rs:627-659](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/oxide-artifacts/src/lib.rs#L627-L659) — `build_host_object_for_target` 的文档完整解释了锚对**库** crate 的意义（归档成员惰性提取），并指出 host 侧 `#[cuda_module]` 宏会发出匹配的引用来强制提取。

[crates/oxide-artifacts/src/lib.rs:728-749](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/oxide-artifacts/src/lib.rs#L728-L749) — `build_host_object_with_section` 给 `.oxart` 段打上 `SHF_ALLOC | SHF_GNU_RETAIN` 标志（`SHF_GNU_RETAIN` 就是抵御 `--gc-sections` 的关键），锚符号用 `SymbolScope::Linkage`（全局绑定可触发归档提取，但又对动态符号表隐藏，不泄漏到最终二进制的导出表）。

宏一侧的引用：

[crates/cuda-macros/src/lib.rs:1308-1320](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/src/lib.rs#L1308-L1320) — 为每个非泛型 kernel 生成一段 `unsafe extern "C"` 块，用 `#[link_name = #anchor_name]` 指向那个锚，再 `black_box(addr_of!(...))` 把锚地址「读」出来。`black_box` 防止优化器把这次读取连同对外部符号的引用一起删掉，从而保证未解析引用真实存在。这段 `#artifact_anchor_statements` 会插进 `load_named()`，所以只要用户程序调了 `load()`，就一定会带上这条引用。完整逻辑与各种边界（泛型 kernel、cfg-gated kernel、owner 过滤）集中在 `cuda_module_artifact_anchor_statements` 全函数 [crates/cuda-macros/src/lib.rs:1263-1325](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/src/lib.rs#L1263-L1325)，其顶部文档（[L1234-L1262](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/src/lib.rs#L1234-L1262)）完整复述了 issue #72 与 owner 过滤的设计动机。

#### 4.2.4 代码实践

**实践目标**：用一个静态库 + C 链接器，亲眼看到「有锚引用 → 归档成员被提取、`.oxart` 被保留」与「无引用 → 被丢弃」的差别。

**操作步骤**：

1. 直接阅读项目里现成的端到端验证测试 [crates/oxide-artifacts/src/lib.rs:1246-1310](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/oxide-artifacts/src/lib.rs#L1246-L1310)（`weak_legacy_alias_extracts_artifact_from_static_archive`）。它用 `ar crs` 把含 `.oxart`+弱锚的目标文件打成 `libartifact.a`，再写一行 C：`extern const unsigned char legacy_anchor; int main(void){ return legacy_anchor; }`，用 `cc` 链接成 `app`，最后 `read_artifact_bundles_from_object_bytes(&app)` 断言 bundle 被正确提取。
2. 运行该测试：`cargo test -p oxide-artifacts --features object-read --features object-write weak_legacy_alias_extracts_artifact_from_static_archive`（需要本机有 `ar` 与 `cc`；能否通过待本地验证）。
3. 思考实验：若把那段 C 里的 `return legacy_anchor;` 删掉（即不再引用锚），重跑会发生什么？

**需要观察的现象 / 预期结果**：
- 有引用时：`bundles.len() == 1`，`bundles[0].name == "demo"`——归档成员被提取，`.oxart` 段存活。
- 无引用时（思考实验）：链接器认为该成员没人需要，`bundles` 会是空的，对应 cuda-oxide 运行时就会报 `ModuleNotFound`。这正是 4.2.1 描述的 issue #72 场景。

> 如果本机没有 GPU 或工具链不全，以上为「源码阅读型实践」——读懂测试断言即可，无需真正运行。

#### 4.2.5 小练习与答案

**练习 1**：为什么锚符号要用 `SymbolScope::Linkage`（隐藏的链接级符号），而不是默认的导出（default/externally visible）符号？
**答案**：`Linkage` 既能满足「全局绑定 → 可被未解析引用命中 → 触发归档提取」这一硬需求，又不会把 `cuda_oxide_artifact_anchor_*` 这种内部符号泄漏进最终二进制的动态符号表，保持 ABI 干净。

**练习 2**：宏在 `load()` 里读取锚地址时为什么包了一层 `std::hint::black_box`？
**答案**：发布构建里优化器很强，可能判定「这个地址读出来从没被用过」而把整条外部符号引用删掉，连带让链接器认为 `.oxart` 段无人需要。`black_box` 是优化屏障，强迫编译器保留这次读取，从而保住未解析引用。

**练习 3**：一个只含泛型 `#[kernel]` 的 `#[cuda_module]`，宏会发出锚引用吗？为什么？
**答案**：不会。泛型 kernel 是在**消费方** crate 单态化后才生成 PTX 的，定义方 crate 本身不产出 `.oxart`。若此时仍发锚引用，会变成一条永远无法解析的未定义符号导致链接失败。宏在 [crates/cuda-macros/src/lib.rs:1287-1289](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/src/lib.rs#L1287-L1289) 用 `if !kernels.iter().any(|kernel| !kernel.is_generic)` 判断，只有存在非泛型 kernel 时才发引用。

---

### 4.3 运行时 bundle 发现

#### 4.3.1 概念说明

字节进了二进制、锚也保住了它，剩下的问题就是：**运行时怎么把它找回来？**

cuda-oxide 的设计是「自省当前可执行文件」——程序启动后，宿主运行时打开**自己**这个二进制文件（`/proc/self/exe` 或等价路径），像解析普通 ELF 一样扫描所有段，挑出名为 `.oxart` 的段，把里面的 blob 反序列化成 `OwnedArtifactBundle` 列表（v2 bundle 还会带 `compile_options` 字段）。这样发布时只需要拷贝一个二进制文件，PTX 已经「焊」在里面了，无需额外的 `.ptx`/`.cubin` 旁车文件。

这一职责被放在 `cuda-core`（底层、不依赖任何 NVVM/LTOIR 二次编译能力），而 `cuda-host`（上层）在它之上做 payload 挑选与可能的二次编译。

#### 4.3.2 核心流程

```
load() / load_embedded_module(ctx, name)
        │
        ▼
artifact_bundles_from_current_exe()           ← cuda-core
   1. std::env::current_exe()  得到自身路径
   2. std::fs::read()          把整个 ELF 读进内存
        │
        ▼
artifact_bundles_from_binary_path(bytes)      ← cuda-core
        │
        ▼
oxide_artifacts::read_artifact_bundles_from_object_bytes(bytes)
   1. object::File::parse(bytes)              ← 用 object crate 解析 ELF
   2. 遍历所有段，挑出 name == ".oxart"
   3. 对每个 .oxart 段调 parse_artifact_section()
   4. 收集成 Vec<OwnedArtifactBundle>（含 compile_options）
```

关键点：发现逻辑对「段名」敏感，对「文件路径」不敏感。`artifact_bundles_from_binary_path` 接受任意路径，因此单元测试可以直接拿一个手工构造的目标文件做往返，而不必真的链接一个可执行文件。

#### 4.3.3 源码精读

入口「从当前可执行文件发现」：

[crates/cuda-core/src/embedded.rs:53-58](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/embedded.rs#L53-L58) — `artifact_bundles_from_current_exe` 用 `std::env::current_exe()` 拿到自身路径，把读取失败包成 `EmbeddedModuleError::CurrentExe`。

读取并交给 `oxide-artifacts`：

[crates/cuda-core/src/embedded.rs:60-70](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/embedded.rs#L60-L70) — `artifact_bundles_from_binary_path` 读出整个文件字节，调 `read_artifact_bundles_from_object_bytes`，IO 错误映射成 `EmbeddedModuleError::Io { path, source }`，格式错误映射成 `Artifacts`。

实际扫描 ELF 段的代码：

[crates/oxide-artifacts/src/lib.rs:604-625](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/oxide-artifacts/src/lib.rs#L604-L625) — 用 `object` crate 解析字节流，遍历 `file.sections()`，**只**处理名字等于 `.oxart` 的段，对每个段的数据调 `parse_artifact_section`（即 4.1 里的串联解析器），把所有 bundle 汇总返回。这一函数是「运行时发现」与「wire 格式」之间的唯一粘合点。

端到端的集成验证：

[crates/cuda-core/src/embedded.rs:215-268](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/embedded.rs#L215-L268) — `artifact_bundles_from_binary_path_reads_linked_executable` 构造一个带锚的 `.oxart` 目标文件，用 `rustc` 的 `link-arg` 把它链进一个空 `main`，生成可执行文件 `host`，再断言从 `host` 里能读回那个 bundle。注释里特意点明「镜像生产环境：后端总是在制品目标文件里定义一个链接锚符号」。注意测试构造 `OwnedArtifactBundle` 时现在必须填 `compile_options` 字段（[第 228 行](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/embedded.rs#L228)），这是 v2 升级带来的可见变化。

#### 4.3.4 代码实践

**实践目标**：理解 `cargo oxide pipeline vecadd` 产出的中间产物里，`.ptx`（以及 NVVM IR 模式下的 `.ll`）是文件形态的「可见制品」，而真正进二进制的是 `.oxart` 段。

**操作步骤**：

1. 进入 vecadd 示例目录，运行 `cargo oxide pipeline vecadd`（参考 u1-l3）。该命令会打开全部诊断开关（`CUDA_OXIDE_VERBOSE`/`CUDA_OXIDE_SHOW_RUSTC_MIR`/`CUDA_OXIDE_DUMP_MIR`/`CUDA_OXIDE_DUMP_LLVM`）做一次 release 构建，见 [crates/cargo-oxide/src/commands.rs:1626-1695](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cargo-oxide/src/commands.rs#L1626-L1695)。
2. 构建结束后，`show_generated_artifacts` 会把 `vecadd.ll`（若存在）与 `vecadd.ptx` 打印到 stdout，见 [crates/cargo-oxide/src/commands.rs:3023-3047](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cargo-oxide/src/commands.rs#L3023-L3047)。这两份文件是设备代码的**文本形态**。
3. 对构建出的可执行文件（通常在该示例的 `target/release/` 下，名为 `vecadd`）执行 `readelf -S ./vecadd | grep oxart` 或 `objdump -h ./vecadd | grep oxart`，应能看到一个名为 `.oxart` 的段。

**需要观察的现象**：
- `.ptx` 文件里能看到 `.version`、`.target`、kernel 入口符号。
- `readelf` 输出里存在 `.oxart` 段，其大小与 PTX 字节数（加上 bundle header/记录表）量级相当。

**预期结果 / 待本地验证**：能定位到 `.oxart` 段即说明 4.2 的锚握手成功保住了它。vecadd 走 PTX 路径、用默认 FMA 策略，故其 bundle 是 **v1**（4.1.2 的版本选择规则）。若 `readelf` 看不到该段，多半是发布构建被 `--gc-sections` 裁掉（对应 SHF_GNU_RETAIN 未生效），或本机工具链版本不符（u1-l3 提到需 llc-21 以上）。具体段大小、偏移以本机实际输出为准。

> 提示：你看到的 `.ptx`/`.ll` 是文件，而 `.oxart` 是二进制内的段——本讲关心的「内嵌」指的是后者。两者内容同源（都是后端编出的设备代码），但承载形态不同。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `artifact_bundles_from_current_exe` 要读「整个文件」而不是用 `dlsym` 之类按符号查找？
**答案**：因为我们要的是整个 `.oxart` **段**（里面可能有多个 bundle、多段 payload），而不是某个单一符号的地址。按段扫描能一次性拿到全部设备代码，且不依赖动态符号表（锚符号是 `Linkage` 隐藏的，本来就不在动态表里）。

**练习 2**：`read_artifact_bundles_from_object_bytes` 遍历段时用 `name != ARTIFACT_SECTION_NAME` 跳过非 `.oxart` 段。如果二进制里有多个 `.oxart` 段会怎样？
**答案**：每个都会被独立解析，结果 `extend` 进同一个 `Vec`。这正是「跨 crate 的 bundle 各自独立内嵌」的运行时体现——一个二进制可以同时携带多个 crate 的设备代码。

---

### 4.4 `load_embedded_module`

#### 4.4.1 概念说明

发现到 `OwnedArtifactBundle` 列表后，最后一公里是：**挑出正确的 bundle、挑出可加载的 payload、交给 CUDA 驱动变成 `CudaModule`**。v2 之后还要多做一件事：**把 bundle 携带的 FMA 策略如实传给二次编译**，否则最终 cubin 的浮点契约就会和编译期意图不符。

这一步有两层实现：

- `cuda-core` 提供了一个**只认 cubin/PTX** 的极简 `load_embedded_module`——因为 `cuda-core` 是「不依赖 libNVVM/LTOIR 二次编译」的薄运行时；cubin/PTX 由驱动直接加载，不涉及 FMA 重编译。
- `cuda-host` 重新导出同名函数，但它的 `load_bundle` 额外支持 NVVM IR 与 LTOIR：遇到这两种 payload 时，会按当前 GPU 的执行架构把 IR 二次编译成 cubin 或 PTX，**并用 `bundle.compile_options.fma_contraction_enabled()` 决定二次编译是否允许 FMA 收缩**，再交给驱动。它还提供 `load_all_ptx_bundles_merged`，把跨 crate 的多份 PTX 合并成单个模块——这对**泛型 kernel** 至关重要。

回顾 u2-l1：`#[cuda_module]` 生成的 `load()` 三步走里，第 2 步「解析 PTX」实际调用的就是 `cuda-host` 的 `load_embedded_module`；第 3 步「逐 kernel 调 `cuModuleGetFunction`」用的就是本步返回的 `CudaModule`。

**签约模块的 loader 边界（本轮 PR #318）**：上面这套发现/加载流水线对「字节从哪来」有一个隐含假设——`LoadedModule` 持有的字节确实由本 `#[cuda_module]` 编译而来。对未签约模块这无所谓（它的启动方法本来就吃 raw `LaunchConfig`、每次启动由调用方自证）。但对**签约**模块（含 `#[launch_contract]` 的 kernel），其 `prepare_*` → `PreparedLaunch` 受检启动之所以能在制备后省去运行期形状校验，前提正是「这次绑定绑对了字节、ABI 与资源语义完全自洽」。这个前提无法在编译期或运行期自动验证，因此 #318 把签约模块**整条绑定路径**（`load`/`load_named`/`from_module` 及 async 版本）标为 `unsafe`，把证明责任前移到「一次性绑定」。详见 4.4.3 末尾与 4.4.4 的实践。

#### 4.4.2 核心流程

`cuda-host` 的加载优先级（按 payload 种类短路返回）：

```
load_bundle(ctx, bundle)
  ├─ 有 Cubin   → cuModuleLoadData(cubin)        直接加载（无需重编译）
  ├─ 有 PTX     → cuModuleLoadData(ptx)          直接加载（驱动 JIT）
  ├─ 有 NvvmIr  → 用 compile_options 选 FMA
  │              按 execution_route 编译成 cubin 或 ptx → 加载
  ├─ 有 Ltoir   → 用 compile_options 选 FMA
  │              链接成 cubin 或 ptx            → 加载
  └─ 全无        → UnsupportedPayload 错误
```

「执行路由（execution_route）」决定 NVVM IR / LTOIR 最终走 cubin 还是 PTX 桥接：它比较「bundle 编译时的目标架构」与「当前 GPU 实际执行架构」，得出 `Cubin` 或 `PtxBridge`。一个为 `sm_86` 编译的标准 payload 在 Blackwell GPU 上可能被转成 PTX 由驱动 JIT，这正是源码注释强调的兼容性路径。**无论走哪条路由，`allow_fma_contraction` 都会作为参数传给 libNVVM/nvJitLink**（见 4.5）。

#### 4.4.3 源码精读

`cuda-host` 的命名加载入口：

[crates/cuda-host/src/embedded.rs:52-63](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-host/src/embedded.rs#L52-L63) — `load_embedded_module(ctx, name)` 从当前可执行文件发现全部 bundle，按 `bundle.name == name` 找到目标 bundle，找不到则 `ModuleNotFound`，再交给 `load_bundle`。注意这里 `pub use cuda_core::embedded::{...}`（[第 9-13 行](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-host/src/embedded.rs#L9-L13)）复用了 `cuda-core` 的发现能力（并重新导出 `ArtifactCompileOptions`），避免重复实现。

payload 优先级、二次编译与 **FMA 策略透传**：

[crates/cuda-host/src/embedded.rs:136-193](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-host/src/embedded.rs#L136-L193) — `load_bundle` 依次尝试 Cubin → PTX → NvvmIr → Ltoir，命中即 `ctx.load_module_from_image(image)`（u3-l1 里讲过的 `CudaContext` 安全封装，底层是 `cuModuleLoadData`）。NVVM/LTOIR 分支会先用 `target_arch_for_bundle` 与 `ltoir::execution_arch_for_context` 算出编译/执行架构，再用 `execution_route` 决定产出 cubin 还是 PTX。**关键改动**（L151、L172）：在两条 IR 分支里都读取 `bundle.compile_options.fma_contraction_enabled()` 得到 `allow_fma_contraction`，并把它传给 `build_cubin_from_nvvm_ir_with_options` / `link_ltoir_to_cubin_with_options` 等带 `_with_options` 后缀的函数（L153-L164、L174-L185）。这是 `cuda-host` 区别于 `cuda-core` 的核心增值能力，也是 v2 制品的运行时落脚点。

跨 crate PTX 合并（泛型 kernel 的救星）：

[crates/cuda-host/src/embedded.rs:77-120](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-host/src/embedded.rs#L77-L120) — `load_all_ptx_bundles_merged` 把当前二进制里**所有** PTX bundle 拼成单个 CUDA 模块：第一份保留 `.version`/`.target`/`.address_size` 头，其余去掉重复头再拼接。文档解释了为什么需要它——泛型 kernel 在**消费方** crate 单态化，其 PTX 落在消费方的 bundle 而非定义方 bundle，只有把所有 PTX bundle 合并，才能保证任意 kernel 符号都能被 `cuModuleGetFunction` 找到。

`cuda-core` 的极简版本（只认 cubin/PTX）：

[crates/cuda-core/src/embedded.rs:79-106](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/embedded.rs#L79-L106) — `cuda-core` 版的 `load_embedded_module` 用 `loadable_payload` 只挑 cubin 或 PTX（[第 102-106 行](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/embedded.rs#L102-L106)），完全不带 NVVM/LTOIR 二次编译，因此也用不到 `compile_options`。这种「分层」让不需要 libNVVM 的场景可以只用 `cuda-core`。

错误模型：

[crates/cuda-host/src/embedded.rs:20-44](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-host/src/embedded.rs#L20-L44) — `EmbeddedModuleError` 用 `thiserror` 把「发现/格式错误（Core）」「找不到（ModuleNotFound）」「无可加载 payload（UnsupportedPayload）」「NVVM/LTOIR 编译失败（Ltoir）」「驱动拒绝（Driver）」分门别类，调用方可以精确区分「bundle 没找到」与「bundle 在但驱动加载失败」。

**签约模块的 loader 为何变 `unsafe`（本轮 PR #318）**：宏根据「模块是否含签约 kernel」（`has_launch_contract`）对每个 loader 生成两个分支——签约走 `unsafe fn`，未签约走安全 `fn`。

[crates/cuda-macros/src/lib.rs:741-758](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/src/lib.rs#L741-L758) — 签约模块的 `load()`：`pub unsafe fn load(ctx)`，内部转发到 `load_named`。其 `# Safety` 文档要求「非泛型模块选中的 bundle 必须确实由本 `cuda_module` 编译；泛型模块的合并 PTX 集必须包含每个匹配特化且无冲突入口」。

[crates/cuda-macros/src/lib.rs:768-786](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/src/lib.rs#L768-L786) — 签约模块的 `load_named()`：这是真正嵌入 `#artifact_anchor_statements`（4.2 的锚握手）与 `#module_loader`（调 `load_embedded_module`）的地方，最后 `unsafe { from_module(module) }` 把绑定也并入同一条 unsafe 边界。

[crates/cuda-macros/src/lib.rs:799-820](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/src/lib.rs#L799-L820) — 签约模块的 `from_module()`：把一个外部 `Arc<CudaModule>` 绑定成 `LoadedModule`，`# Safety` 文档明确「每个被加载的 kernel 都必须具有本 `cuda_module` 声明的精确 ABI 与资源语义，仅符号名匹配并不充分」。`load_async`/`load_async_named` 在 `async` feature 下同理为 `unsafe`（[L690-L721](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/src/lib.rs#L690-L721)）。

注意「不对称」：**未签约**模块的 loader 仍是安全函数（同名 `pub fn load`，见 [L759-L767](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/src/lib.rs#L759-L767)），因为它的启动方法本来就吃 raw `LaunchConfig`、由调用方每次启动自证；签约模块则把证明从「每次启动」前移到「一次性绑定」。这条边界由对偶测试对锁定：`contracted_module_requires_provenance_for_every_loader`（[L6405-L6424](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/src/lib.rs#L6405-L6424)）断言签约模块的 `load`/`load_named`/`from_module`（及 async 版本）展开后含 `pub unsafe fn` 与 `# Safety`；`uncontracted_module_preserves_safe_custom_loaders`（[L6426-L6442](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/src/lib.rs#L6426-L6442)）断言未签约模块同名 loader 仍是安全的。`cuda-host` 的 crate 文档示例也相应把 `module.vecadd(...)` 包进 `unsafe { }` 并附 `SAFETY:` 注释，见 [crates/cuda-host/src/lib.rs:72-84](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-host/src/lib.rs#L72-L84)。

#### 4.4.4 代码实践

**实践目标**：在 vecadd 的产物基础上，把 4.3 看到的 `.oxart` 段、4.1 的 wire 格式与 4.4 的加载流程串起来，亲手「指认」PTX payload 与 entry 记录各自在字节流里的位置。

**操作步骤**：

1. 先按 4.3.4 跑 `cargo oxide pipeline vecadd`，确认能拿到 `vecadd.ptx` 与带 `.oxart` 段的可执行文件。
2. 用 `objcopy` 把 `.oxart` 段单独抽出来（示例命令，需本机有 `objcopy`）：
   ```bash
   objcopy --dump-section .oxart=vecadd.oxart ./target/release/vecadd
   hexdump -C vecadd.oxart | head -n 8
   ```
3. 对照 4.1.2 的布局表「读」前 32 字节：
   - `[0:8]` 是否是 `OXIDEART`？
   - `[8:10]`（小端 u16）是否是 `1`（vecadd 默认策略 → v1）？
   - `[12:16]`（小端 u32）是否等于文件总长？
   - `[20:22]` 的 payload 计数、`[22:24]` 的 entry 计数是否符合预期（vecadd 通常 1 个 PTX payload + 1 个 kernel entry）？
4. 比对 `vecadd.ptx` 的字节数与 bundle 里某条 payload 记录的 `data_len` 字段：两者应当相等——这证明 `.ptx` 文件里的内容就是被嵌进 `.oxart` 的那段 payload。

**需要观察的现象**：
- `hexdump` 开头能看到 `OXIDEART` magic。
- payload 记录表里有一条 `kind = 0x100`（PTX）的记录，其 `data_len` 与 `vecadd.ptx` 大小一致。
- entry 记录表里有一条 `kind = 1`（Kernel）的记录，符号串为 `vecadd`（即 u2-l1 讲过的剥前缀 PTX 入口名）。

**预期结果 / 待本地验证**：能逐一指认 magic、version、total_len、payload 记录、entry 记录五类字段，即说明你已彻底掌握 `.oxart` 的 wire 格式与运行时加载链。若 `objcopy` 不可用，可改为阅读 [crates/oxide-artifacts/src/lib.rs:485-585](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/oxide-artifacts/src/lib.rs#L485-L585) 的解析顺序，按它读字段的次序手工对照 hexdump。

---

**补充实践：为含 `#[launch_contract]` 的模块写出 `unsafe load()` 并解释一次性证明**

承接 4.4.3 末尾的 #318 内容。

1. **实践目标**：亲手触发签约模块 loader 的 `unsafe` 边界，并写出带 `SAFETY:` 注释的一次性绑定证明。

2. **操作步骤**：
   - 新建一个最小示例（或在某个示例里）写一个签约 kernel（参见 u2-l1/u2-l4 的 `#[launch_contract]` 语法）：
     ```rust
     // 示例代码：签约模块的 loader 调用，非项目原有代码
     #[cuda_module]
     mod kernels {
         use super::*;
         #[kernel]
         #[launch_contract(domain = 1, block = (256, 1, 1))]
         pub fn reduce(a: &[f32], out: *mut f32) { /* ... */ }
     }
     ```
   - 在宿主 `main` 里调用 `kernels::load(&ctx)`。由于该模块含签约 kernel，宏把 `load` 生成为 `unsafe fn`，编译器会强制你把它包进 `unsafe { }`：
     ```rust
     // SAFETY: `kernels::load` selects this package's own embedded artifact,
     // which the cuda-oxide backend compiled from exactly this `cuda_module`.
     // The bound bytes therefore carry reduce's declared ABI and resource
     // semantics; after this one-time binding, prepare_reduce()/PreparedLaunch
     // launches are safe.
     let module = unsafe { kernels::load(&ctx)? };
     ```
   - 用 `cargo expand` 展开（或在 IDE 里 hover）确认：`load`/`load_named`/`from_module` 的签名都是 `pub unsafe fn`，且各带一段 `# Safety` doc。

3. **需要观察的现象**：
   - 去掉 `unsafe { }` 包裹时，编译器报「call to unsafe function」错误——这正是 4.4.3 引用的 `contracted_module_requires_provenance_for_every_loader` 守护的契约。
   - 对照一个**未签约**模块（把 `#[launch_contract]` 那行删掉重编）：其 `load` 退回安全 `fn`，无需 `unsafe` 包裹（`uncontracted_module_preserves_safe_custom_loaders` 守护）。

4. **预期结果**：能说清「这一次 `unsafe` 证明的是绑定正确性（字节来源 == 模块声明），之后 `module.prepare_reduce(...)` 产出的 `PreparedLaunch` 启动才是安全的」——即 #318 把证明从「每次启动」前移到「一次性绑定」。能否在本机完整运行待本地验证（取决于 GPU 与工具链）；即便不能运行，`cargo expand` 的展开结果与上述两个宏测试已足够印证签名差异。

#### 4.4.5 小练习与答案

**练习 1**：`cuda-host` 的 `load_bundle` 为什么把 Cubin 排在 PTX 前面？
**答案**：cubin 是已针对具体架构汇编好的二进制，驱动加载它无需任何 JIT，启动最快、兼容性最确定；PTX 还需驱动即时编译。同一 bundle 若同时带了 cubin 与 PTX，优先用 cubin 是性能与确定性兼顾的选择。

**练习 2**：为什么泛型 kernel 场景必须用 `load_all_ptx_bundles_merged` 而不是 `load_embedded_module`？
**答案**：泛型 kernel 在消费方 crate 单态化，PTX 落在消费方的 bundle；调用方通常只知道定义方 crate 的名字，`load_embedded_module(ctx, "定义方")` 找不到消费方 bundle 里的那份 PTX。`load_all_ptx_bundles_merged` 不按 crate 名筛选，而是把全部 PTX bundle 合并成一个模块，于是任何 kernel 符号都在同一个 `CudaModule` 里可解析。

**练习 3**：`EmbeddedModuleError::UnsupportedPayload` 与 `ModuleNotFound` 分别在什么场景下触发？
**答案**：`ModuleNotFound` 是「bundle 没找到」（名字对不上，或锚握手失败导致整段被裁）；`UnsupportedPayload` 是「bundle 找到了，但里面没有任何驱动能加载的 payload」。区分两者能帮你快速定位是「链接/嵌入」问题还是「payload 种类」问题。

**练习 4**（本轮 #318）：为什么同一个 `#[cuda_module]`，加不加 `#[launch_contract]` 会改变 `load()` 的签名安全性？这条 `unsafe` 证明的到底是什么？
**答案**：未签约模块的启动方法吃 raw `LaunchConfig`、每次启动由调用方自证形状，故 loader 可以是安全的；签约模块的 `prepare_*` → `PreparedLaunch` 受检启动把校验前移到「制备时一次性完成」，其安全性依赖「`LoadedModule` 绑定的字节确实由本模块声明编译而来、ABI/资源语义自洽」——这个前提无法自动验证，故 #318 把签约模块的 loader 标为 `unsafe`，由调用方在绑定时一次性证明。证明之后，受检启动本身才是安全的。这正是把 unsafe 边界从「每次启动」收敛到「一次性绑定」。

---

### 4.5 `ArtifactCompileOptions` 与 FMA 边车

> 本模块是 v2 升级（PR #326）的核心新增，回答：**编译期定下的「是否允许 FMA 收缩」如何不丢失地传到最终机器码？**

#### 4.5.1 概念说明

浮点 `a*b+c` 在 GPU 上可以被合并成一条 FMA 指令（只舍入一次）。这对多数程序是好事，但有两类场景必须**严格禁用**：

1. **bit-exact 回归测试 / 差分模糊测试**（见 [u7-l4](u7-l4-symbol-contract-artifacts-fuzzing.md)）：codegen 正确性测试需要 host 与 device 产生位级一致的结果，隐式 FMA 会破坏对比。
2. **数值方法对舍入敏感的代码**：某些迭代算法的收敛性依赖「先乘后加」的两次舍入。

cuda-oxide 用 `--no-fmad`（或 `CUDA_OXIDE_NO_FMA=1`）关闭收缩。难点在于：关闭 FMA 这件事**必须贯穿整条流水线传到最终 cubin 生成**——如果只在中间某层关闭、到最后链接时又默认开启，机器码的浮点行为就和源码意图不符了。而 NVVM IR / LTOIR 这种 payload 在**运行时**才由 `cuda-host` 二次编译成 cubin，编译期的策略如何「搭车」跟到运行时？

答案就是 `ArtifactCompileOptions`：它把策略编码进 bundle 头部（v2 的 `[24:32]` 位域），运行时 `load_bundle` 读出来再传给 libNVVM/nvJitLink。对于**文件形态**的互操作制品（`.ll`、`.ltoir`），由于没有 bundle 头可挂，cuda-oxide 额外写出 `.options` 与 `.target` **边车（sidecar）文件**来承载同一份策略。

#### 4.5.2 核心流程

策略的全链路流转（从命令行到最终 cubin）：

```
cargo oxide run --no-fmad   （或 CUDA_OXIDE_NO_FMA=1）
        │
        ▼
device_codegen: allow_fma_contraction = (CUDA_OXIDE_NO_FMA is None)
        │  注入 PipelineConfig
        ▼
mir-lower: 按 allow_fma_contraction 决定算术 op 的 contract 标志
        │
        ▼
后端 write_device_artifact_object:
        │  对 NvvmIr/Ltoir payload：
        │    compile_options = ArtifactCompileOptions::new()
        │                        .with_fma_contraction(allow_fma_contraction)
        │  对 Ptx/Cubin payload：compile_options = 默认（FMA on）
        ▼
build_artifact_blob: 默认→v1，禁用 FMA→v2，策略写入头部 [24:32]
        │  （NVVM IR 模式额外在 .ll 旁写 .options 边车）
        ▼
=== 制品嵌入二进制（4.2/4.3）===
        │
        ▼
运行时 load_bundle: 读 bundle.compile_options.fma_contraction_enabled()
        │
        ▼
ltoir: 把 allow_fma_contraction 翻译成 -fma=1 / -fma=0
        │  传给 libNVVM / nvJitLink
        ▼
最终 cubin 遵守编译期意图的 FMA 契约
```

注意一个不对称：**PTX 与 cubin payload 永远带默认策略（FMA on）**。这是因为 PTX/cubin 由驱动直接加载、不再经 libNVVM/nvJitLink 重编译，FMA 收缩早在 mir-lower 阶段就已「焊死」进 PTX 文本（通过 LLVM IR 的 `contract` flag），运行时没有再决策的机会，也就不需要在 bundle 里携带策略位。只有 NVVM IR / LTOIR 这类「运行时还要再编译」的 payload，才需要把策略带进 bundle。

#### 4.5.3 源码精读

**策略位的封装**：`ArtifactCompileOptions` 是一个封装 `u64` 的 newtype：

[crates/oxide-artifacts/src/lib.rs:42-97](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/oxide-artifacts/src/lib.rs#L42-L97) — `new()` 是零值（历史默认，FMA 收缩开启）；`with_fma_contraction(bool)` 置/清最低位；`fma_contraction_enabled()` 读最低位；`from_bits` 拒绝未知位。文档（L37-L41）明确：「零值保留历史默认，使 v2 之前的 bundle 完全兼容」。

**边车文本格式**：策略除了进 bundle 头，还能编码成 `.options` 文件文本：

[crates/oxide-artifacts/src/lib.rs:28-35](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/oxide-artifacts/src/lib.rs#L28-L35) — 边车文件以 `cuda-oxide-compile-options-v1` 为头，第二行是 `fma-contraction=on|off`。`sidecar_text()`（L79-L85）与 `from_sidecar_text()`（L88-L96）负责编解码，往返由测试 `compile_options_sidecar_round_trips_both_policies`（[L1012-L1021](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/oxide-artifacts/src/lib.rs#L1012-L1021)）锁定。还有一个常量 `COMPILE_OPTIONS_TARGET_MARKER = "compile-options=v1"`（L31），写在 `.target` 边车的第二行，**它的作用是让只认单行 target 的老读取器直接拒绝这个制品**，而不是把带策略的 target 当成普通 target 静默忽略。

**编译期：从环境变量到 bundle**：

[crates/rustc-codegen-cuda/src/device_codegen.rs:675-692](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/device_codegen.rs#L675-L692) — `allow_fma_contraction = std::env::var_os("CUDA_OXIDE_NO_FMA").is_none()`，即「没设 NO_FMA 就允许收缩」，并注入 `PipelineConfig` 向下传。

[crates/rustc-codegen-cuda/src/lib.rs:805-821](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/lib.rs#L805-L821) — `write_device_artifact_object` 按 payload 种类分流：NvvmIr/Ltoir 用 `with_fma_contraction(result.allow_fma_contraction)`，Ptx/Cubin 用默认；再用 `.with_compile_options(...)` 挂到 `ArtifactBundleSpec`。

**编译期：`.ll` 旁的 `.options` 边车**（NVVM IR 互操作模式）：

[crates/mir-importer/src/pipeline.rs:398-412](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/pipeline.rs#L398-L412) — `write_nvvm_compile_options_sidecar` 在写出 `<name>.ll` 的同时（调用点见 [L326](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/pipeline.rs#L326)），把 `ArtifactCompileOptions::new().with_fma_contraction(...)` 的 `sidecar_text()` 写到同目录的 `<name>.options`。这样脱离 bundle 头的「裸 NVVM IR 文件」也能携带 FMA 策略。（本轮 #314 把后段流水线迁入新 crate `cuda-oxide-codegen`，但 NVVM IR 边车写出仍留在 mir-importer 的导出阶段。）

**运行期：读 bundle 策略并透传**（已在 4.4.3 引用）：

[crates/cuda-host/src/embedded.rs:148-187](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-host/src/embedded.rs#L148-L187) — NvvmIr 与 Ltoir 两条分支都从 `bundle.compile_options.fma_contraction_enabled()` 取 `allow_fma_contraction`，传给 `ltoir` 模块的 `_with_options` 函数族。

**运行期：策略翻译成工具选项**：

[crates/cuda-host/src/ltoir.rs:451-462](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-host/src/ltoir.rs#L451-L462) — `nvjitlink_lto_options` 把 `allow_fma_contraction` 翻译成 nvJitLink 的 `-fma=1` 或 `-fma=0`；libNVVM 侧的 `nvvm_compile_options`（[L539](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-host/src/ltoir.rs#L539) 附近）做同样翻译。对裸 `.ll` 文件的互操作加载，`read_fma_contraction_option`（[L1140](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-host/src/ltoir.rs#L1140)）会读同目录 `.options` 边车来恢复策略。

#### 4.5.4 代码实践

**实践目标**：用 `cargo oxide pipeline` 对照观察「默认策略（v1）」与「`--no-fmad`（v2）」两种 bundle 的版本号差异，并定位 PTX payload、entry 记录与 compile-options 字段的位置；理解 v2 为何在默认策略下仍写 v1。

**操作步骤**：

1. **默认构建（得到 v1 bundle）**：在某个使用 libdevice 数学函数的示例（如 `libm_math` 或 `mathdx_ffi_test`，这类示例会走 NVVM IR 路径）目录下运行：
   ```bash
   cargo oxide pipeline <示例名>
   ```
   再对构建产物 `readelf -S ./target/release/<示例名> | grep oxart` 找到 `.oxart` 段，`objcopy --dump-section .oxart=out.oxart ...` 抽出后 `hexdump -C out.oxart | head -4`。读 `[8:10]` 应为 `01 00`（v1），`[24:32]` 为全零（默认策略）。
2. **禁用 FMA 构建（得到 v2 bundle）**：加 `--no-fmad` 重跑：
   ```bash
   cargo oxide pipeline <示例名> --no-fmad
   ```
   重新抽出 `.oxart`，`[8:10]` 应变为 `02 00`（v2），`[24:32]` 的最低字节应为 `01`（`OPTION_NO_FMA_CONTRACTION`）。
3. **定位字段**：在 v2 bundle 的 hexdump 里，按 4.1.2 的布局依次指认 magic(`[0:8]`)、version(`[8:10]`)、total_len(`[12:16]`)、payload_cnt(`[20:22]`)、entry_cnt(`[22:24]`)、compile-options(`[24:32]`)，以及紧随其后的 payload 记录表（`kind` 字段，NvvmIr 是 `00 01`）与 entry 记录表。
4. **观察边车（NVVM IR 模式专属）**：若该示例在 NVVM IR 模式下产出了 `<示例名>.ll`，应能在同目录看到 `<示例名>.options`，内容形如：
   ```
   cuda-oxide-compile-options-v1
   fma-contraction=off
   ```
   （`--no-fmad` 时为 `off`，默认为 `on`）。LTOIR 互操作命令 `cargo oxide emit-ltoir <示例名>` 还会额外写 `<示例名>.target`，第二行是 `compile-options=v1` 标记，见 [crates/cargo-oxide/src/commands.rs:1155-1176](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cargo-oxide/src/commands.rs#L1155-L1176)。

**需要观察的现象**：
- 同一份设备代码，默认构建产出 v1 bundle，`--no-fmad` 构建产出 v2 bundle——唯一区别是头部版本号与 `[24:32]` 策略位。
- v2 bundle 里 `[24:32]` 非零，对应「禁用 FMA」。
- `.options` 边车的 `fma-contraction` 行随 `--no-fmad` 在 `on`/`off` 间切换。

**预期结果 / 待本地验证**：能指认 compile-options 字段、能解释两种版本号差异，即说明你掌握了 v2 的策略编码。**关于「v2 为何在默认策略下仍写 v1」**：因为绝大多数 bundle（所有 PTX/cubin 示例，以及未关 FMA 的 NVVM IR 示例）都是默认策略，若一律写 v2，会把所有老读取器（只认 v1）拒之门外，破坏向后兼容；而只有携带了「必须遵守」的非默认策略时才升级到 v2，让老读取器通过「不认识版本号」而**安全拒绝**，而不是静默丢弃策略。这是「兼容优先、安全兜底」的取舍，见 [crates/oxide-artifacts/src/lib.rs:439-446](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/oxide-artifacts/src/lib.rs#L439-L446) 的注释。

> 若本机没有走 NVVM IR 路径的示例或工具链不全，可退化为「源码阅读型实践」：对照 [crates/oxide-artifacts/src/lib.rs:926-1009](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/oxide-artifacts/src/lib.rs#L926-L1009) 的三个测试，口头推演默认 PTX→v1、关闭 FMA 的 NvvmIr→v2、v1 忽略保留字节这三条行为。

#### 4.5.5 小练习与答案

**练习 1**：为什么 PTX payload 的 bundle 永远是默认策略（FMA on），即使加了 `--no-fmad`？
**答案**：PTX 由驱动直接加载，不再经 libNVVM/nvJitLink 重编译，运行时没有「再决策 FMA」的机会。`--no-fmad` 对 PTX 路径的效果早在 mir-lower 阶段就通过 LLVM IR 的 `contract` flag 焊进了 PTX 文本本身（见 [u6-l3](u6-l3-mir-lower-ops-deep-dive.md)），所以 bundle 不必再携带策略位——携带了也没人在运行时读它。bundle 头部的策略位只对「运行时还要再编译」的 NvvmIr/Ltoir 有意义。

**练习 2**：`.target` 边车第二行的 `compile-options=v1` 标记有什么用？
**答案**：它让「只认单行 target 的老读取器」在第二行撞上不认识的内容而**拒绝**整个制品，而不是把「带策略声明的多行 target」误读成「只有一行的普通 target」从而丢掉策略。这是一种用「格式不兼容」来强制「语义不被忽略」的防御性设计。

**练习 3**：假如未来要新增一个「是否启用快速数学（fast-math）」的策略位，需要改动 `ArtifactCompileOptions` 的哪些地方，才能保证旧读取器安全？
**答案**：定义新位常量（如 `OPTION_FAST_MATH = 1 << 1`）并加入 `KNOWN_COMPILE_OPTIONS` 掩码。旧读取器因为 `KNOWN_COMPILE_OPTIONS` 不含该位，会在 `from_bits` 里因「未知位」返回 `UnsupportedCompileOptions` 而拒绝 bundle——这正是练习期望的「安全拒绝而非静默忽略」。新读取器则扩展掩码并实现对应的 `with_fast_math`/`fast_math_enabled` 访问器。

---

## 5. 综合实践

把本讲五个模块串成一个端到端排查任务：

**场景**：你新写了一个 lib crate `my_kernels`，里面有一个 `#[cuda_module]` 和一个非泛型 `#[kernel]`（含浮点运算）。你给该 kernel 加了 `#[launch_contract(domain = 1, block = (256, 1, 1))]`，因此 `kernels::load(&ctx)` 现在是 `unsafe fn`。另一个二进制 crate 依赖它并调用 `kernels::load(&ctx)`。你用 `cargo oxide build --release --no-fmad` 构建（因为你需要 bit-exact 的浮点结果），运行后却收到 `ModuleNotFound`；即便能加载，你也怀疑最终 cubin 并没有真正禁用 FMA。

**任务**：用本讲学到的知识定位根因，并验证 FMA 策略确实一路传到了 cubin，同时给出签约模块 `load()` 的一次性绑定 `SAFETY:` 证明。

**参考排查路径**：

1. **先确认 bundle 是否真的进了二进制**（对应 4.3）：对最终可执行文件跑 `readelf -S | grep oxart`。
   - 若**没有** `.oxart` 段 → bundle 没被嵌入，问题在链接阶段，跳到第 2 步。
   - 若**有** → bundle 在，问题在加载/命名/FMA，跳到第 3 步。

2. **排查锚握手**（对应 4.2）：bundle 没进二进制，最可能是 lib crate 的归档成员没被提取。
   - 检查宏是否真的发出了锚引用：grep `my_kernels` 的展开（`cargo expand`）里有没有 `cuda_oxide_artifact_anchor_246e25db_my_kernels_...` 与 `black_box`。
   - 检查后端是否定义了同名锚：构建时打开 `CUDA_OXIDE_VERBOSE=1`，看日志里的 `Embedded artifact object complete` 路径，对该目标文件跑 `readelf -s` 应能看到该锚符号且为已定义全局。
   - 两侧名字对不上（例如版本号、包名替换规则不一致）会导致引用永远无法命中，归档成员被丢弃——这正是 issue #72。

3. **排查加载链与 FMA 策略**（对应 4.4、4.5）：bundle 在但 `load()` 报错或 FMA 行为可疑。
   - 若报 `ModuleNotFound`：检查传给 `load_embedded_module` 的 `name` 是否等于 bundle 的 `name`（默认是 `CARGO_PKG_NAME`）。
   - 若报 `UnsupportedPayload`：bundle 里没有 cubin/PTX（可能是只编了 NVVM IR 但用了 `cuda-core` 而非 `cuda-host`，或 execution_route 选不出合法目标）。
   - **验证 FMA 策略是否生效**：抽出 `.oxart` 段，检查 `[8:10]` 版本号——`--no-fmad` 下若 bundle 是 NVVM IR/LTOIR payload，应为 **v2** 且 `[24:32]` 最低字节为 `01`；若仍是 v1，说明策略没进 bundle（检查后端是否走了 `with_fma_contraction(false)` 分支）。最终在 ltoir 二次编译时，`-fma=0` 应出现在 libNVVM/nvJitLink 选项里（可用 `CUDA_OXIDE_VERBOSE=1` 或差分测试核对生成的 cubin 行为）。
   - **签约模块的 `unsafe load()` 证明**（对应 4.4 #318）：因为本场景 kernel 带 `#[launch_contract]`，`kernels::load` 是 `unsafe fn`。在调用处写一行 `SAFETY:` 注释，说明「选中的是本包自身内嵌的 artifact，由 cuda-oxide 后端从本 `cuda_module` 编译而来，故绑定字节携带该 kernel 声明的 ABI 与资源语义；此一次性绑定后，`prepare_*`/`PreparedLaunch` 受检启动是安全的」。若调用方拿到的不是本包 bundle（例如误用 `load_named` 指向了同名但不同源的字节），该证明即不成立。

4. **格式核验**（对应 4.1）：若怀疑 bundle 损坏，把 `.oxart` 段 dump 出来，确认 magic/version/计数合理，payload 记录的 `data_len` 与 payload 字节数一致，v2 bundle 的 `[24:32]` 只含已知的策略位。

**预期产出**：一张「症状 → 可能根因 → 验证命令 → 修复方向」的排查表、一条「`--no-fmad` 从命令行到 cubin 的策略传递证据链」，以及一段签约模块 `load()` 的一次性绑定 `SAFETY:` 证明。完成这三份交付物，你就把本讲的「格式（v1/v2 + 策略位）→ 锚 → 发现 → 加载（含签约 loader unsafe 边界）→ FMA 边车」五环完全打通了。

## 6. 本讲小结

- `.oxart` 是一个自描述的 wire 格式：固定 32 字节 header（含 `OXIDEART` magic、版本号、total_len、计数，以及 v2 在 `[24:32]` 的 `compile-options` 位域）+ 变长的名字/target + 两张各 24 字节的定长记录表（payload 与 entry）+ 对齐的数据区；`oxide-artifacts` 同时负责序列化、反序列化与「包成宿主目标文件」。
- **v2 与 FMA 策略**：头部 `[24:32]` 从 v1 的保留零填充升级为 `ArtifactCompileOptions` 位域（目前仅 `OPTION_NO_FMA_CONTRACTION`）。序列化器对默认策略仍写 **v1**（向后兼容），仅当携带非默认策略时才写 **v2**，让老读取器因「不认识版本号」而安全拒绝、而非静默丢策略。NVVM IR/LTOIR 文件形态制品则用 `.options`/`.target` 边车承载同一策略。
- **锚符号握手**是 lib crate bundle 不被链接器裁剪的关键：后端在 `.oxart` 段首定义全局锚 `cuda_oxide_artifact_anchor_246e25db_<pkg>_<ver>`（段加 `SHF_GNU_RETAIN`），`#[cuda_module]` 宏在 `load()` 里用 `black_box(addr_of!(#[link_name=...]))` 发出匹配引用，强制归档提取并抵御 `--gc-sections`。
- 运行时**自省当前可执行文件**：`cuda-core` 用 `object` crate 解析自身 ELF，扫描所有名为 `.oxart` 的段，调 `parse_artifact_section` 还原出 `OwnedArtifactBundle` 列表（含 `compile_options`）。
- **两层加载分工**：`cuda-core` 的 `load_embedded_module` 只认 cubin/PTX（薄运行时）；`cuda-host` 的 `load_bundle` 额外支持 NVVM IR/LTOIR 的二次编译，并**把 `bundle.compile_options` 的 FMA 策略透传给 libNVVM/nvJitLink 的 `-fma=1/0`**，还提供 `load_all_ptx_bundles_merged` 解决泛型 kernel 跨 crate 的 PTX 归并问题。
- **签约模块 loader 的 unsafe 边界（本轮 #318）**：当 `#[cuda_module]` 含签约 kernel（`#[launch_contract]`）时，宏把 `load`/`load_named`/`from_module`（及 async 版本）全部生成为 `unsafe fn`，调用方在绑定时一次性证明「字节即本模块声明产物」；未签约模块的 loader 仍是安全的。这条边界把证明从「每次 raw 启动」前移到「一次性绑定」，之后 `prepare_*` → `PreparedLaunch` 受检启动才是安全的。
- 错误模型清晰区分「没找到 bundle（`ModuleNotFound`）」「bundle 在但无可用 payload（`UnsupportedPayload`）」「编译/驱动失败（`Ltoir`/`Driver`）」，便于运行时定位。

## 7. 下一步学习建议

- 想看「加载完之后内核如何被启动」，继续 [u2-l4 从宿主启动内核](u2-l4-launching-kernels.md)，它讲解拿到 `CudaModule` 后的参数 marshalling 与 `cuLaunchKernel`。
- 想深入 NVVM IR/LTOIR 这条二次编译支线（本讲把 FMA 策略透传给了它的 `_with_options` 函数族），阅读 `crates/cuda-host/src/ltoir.rs`。它属于编译流水线的尾部，建议在学完 U4「编译流水线总览」、尤其是 **u4-l5「从 NVVM IR 到 cubin：libNVVM + nvJitLink + libdevice（含 FMA 契约）」** 后再回看。
- 想理解 FMA 收缩在**中间表示层**是如何被「焊死」的（PTX 路径为何不需要 bundle 带策略），阅读 [crates/mir-lower/src/convert/ops/arithmetic.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/ops/arithmetic.rs)，这在 [u6-l3 mir-lower 深潜](u6-l3-mir-lower-ops-deep-dive.md) 会系统讲解。
- 想理解「为什么有些 crate 的设备代码会被 owner 过滤跳过」，回到 [crates/rustc-codegen-cuda/src/lib.rs:869-879](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/src/lib.rs#L869-L879) 看 `build_host_anchor_object_for_target` 如何让后端只写锚占位文件而不写 `.oxart`，这条线在 U4 的「后端入口与 host/device 分流」会系统讲解。
- 下一讲 [u3-l3 异步执行模型](u3-l3-async-execution-model.md) 将离开「加载」话题，进入 cuda-async 的惰性 `DeviceOperation` 与 `DeviceFuture` 三态机。
