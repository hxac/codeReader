# 模块加载与内嵌制品

## 1. 本讲目标

本讲要回答一个贯穿 cuda-oxide 运行时的核心问题：

> 编译期生成的 PTX（或 cubin/NVVM IR/LTOIR）是**怎样**被装进最终的可执行文件里，运行时又是**怎样**被宿主程序找回来并交给 CUDA 驱动的？

读完本讲，你应当能够：

- 画出 `.oxart` 制品段的二进制 wire 格式，并说出每一段字节的含义；
- 解释「锚符号握手（anchor handshake）」为什么是 lib crate 场景下避免 bundle 被链接器裁剪的关键，并追踪它跨 `#[cuda_module]` 宏与 codegen 后端两侧的实现；
- 跟踪运行时从「当前可执行文件」中发现、解析、加载内嵌 bundle 的完整调用链；
- 区分 `cuda-core` 与 `cuda-host` 两层在模块加载上的职责分工。

本讲承接 [u2-l1](u2-l1-kernel-and-cuda-module-macros.md)（宏生成的 `load()` 三步走）与 [u3-l1](u3-l1-cuda-core-safe-wrappers.md)（`CudaContext`/`CudaModule` 的 RAII 封装），把视角从「类型与句柄」下沉到「字节与链接器契约」。

## 2. 前置知识

阅读本讲前，建议你已经了解：

- **ELF 目标文件与「段（section）」**：Linux 上的可执行文件由若干段组成。cuda-oxide 把设备代码塞进一个名为 `.oxart` 的自定义数据段，运行时再用解析器把这个段读回来。你只要知道「ELF 文件 = 一堆带名字的段」即可。
- **归档成员（archive member）的惰性提取**：Rust 的 `.rlib` 本质是一个静态归档（`.a`）。链接器在处理归档时**不会**把每个成员都链接进来，只有当某个成员定义了一个能被「未解析引用」命中的符号时，它才会被提取（pull in）。本讲的「锚符号」机制正是建立在这条规则上。
- **`--gc-sections` / dead-strip**：链接器会删除最终二进制中「没人引用」的段以缩减体积。`.oxart` 段如果被当作普通数据，就可能在发布构建中被这样裁掉。
- **CUDA 驱动的模块加载**：`cuModuleLoadData` 接收一段 PTX/cubin 字节，返回一个 `CUmodule` 句柄（在 cuda-oxide 里被安全封装成 `CudaModule`，见 u3-l1）。
- **u2-l1 引入的 `load()` 三步走**：anchor 握手 → `load_embedded_module` 解析 PTX → 逐个 kernel 调 `cuModuleGetFunction`。本讲深入第 1、2 步的内部实现。

## 3. 本讲源码地图

| 文件 | 层 | 作用 |
|------|----|------|
| `crates/oxide-artifacts/src/lib.rs` | 制品格式层 | 定义 `.oxart` 段的 wire 格式、序列化/反序列化、把 blob 包成宿主目标文件、定义锚符号 |
| `crates/rustc-codegen-cuda/src/lib.rs` | 编译器后端 | 编译期收集设备代码，调用 `oxide-artifacts` 打包成 `.oxart` 段并产出宿主目标文件，交给 rustc 链接 |
| `crates/cuda-macros/src/lib.rs` | 宏层 | `#[cuda_module]` 在生成的 `load()` 里发出对锚符号的**引用**，完成握手 |
| `crates/reserved-oxide-symbols/src/lib.rs` | 命名契约 | 定义锚符号的前缀与构造规则，保证后端与宏两侧拼出同一个名字 |
| `crates/cuda-core/src/embedded.rs` | 运行时（底层） | 从「当前可执行文件的字节」中发现 `.oxart` 段、解析成 `OwnedArtifactBundle`、挑出可加载的 payload |
| `crates/cuda-host/src/embedded.rs` | 运行时（上层） | 在 `cuda-core` 之上挑选 payload、处理 NVVM/LTOIR 的二次编译、交给驱动加载成 `CudaModule` |

一句话总结分工：`oxide-artifacts` 管「字节怎么排」，后端 + 宏管「字节怎么进/出二进制」，`cuda-core` 管「从二进制里把字节找出来」，`cuda-host` 管「把字节变成可执行的 GPU 模块」。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块，对应设备代码「打包 → 装入二进制 → 运行时找回 → 加载执行」的全生命周期。

---

### 4.1 `.oxart` bundle 格式

#### 4.1.1 概念说明

设备代码（PTX 文本、cubin 二进制、NVVM IR、LTOIR）归根到底是一串字节。要把它们随宿主程序一起发布，最简单的办法就是把这串字节嵌进可执行文件的某个数据段。但光塞进去还不够——运行时需要知道：

- 这串字节是**哪种** payload（PTX 还是 cubin？）；
- 它是为**哪个设备架构**编译的（`sm_80`？`sm_100a`？）；
- 它里面**导出了哪些入口符号**（哪些是 kernel，哪些是普通 `#[device]` 函数？）；
- 它属于**哪个 bundle**（一个二进制里可能并存多个 crate 各自的 bundle）。

`oxide-artifacts` crate 定义了一个与具体加速器后端无关的「制品 bundle」wire 格式来回答这些问题。这个格式被存进名为 `.oxart` 的 ELF 段里（`oxart` = **ox**ide **art**ifact）。设计目标有两点：自描述（带 magic 与版本号）、可串联（一个段里可以背靠背放多个 bundle）。

#### 4.1.2 核心流程

一个 bundle 的内存布局如下（小端字节序）：

```
┌──────────────── HEADER（固定 32 字节）────────────────┐
│ [ 0: 8]  magic        "OXIDEART"                      │
│ [ 8:10]  version      u16 = 1                         │
│ [10:12]  header_len   u16 = 32                        │
│ [12:16]  total_len    u32（整个 blob 的字节数）        │
│ [16:18]  name_len     u16                             │
│ [18:20]  target_len   u16                             │
│ [20:22]  payload_cnt  u16                             │
│ [22:24]  entry_cnt    u16                             │
│ [24:32]  保留（全 0，对齐到 32）                       │
├──────────────── 变长区 ────────────────────────────────┤
│ bundle 名字（name_len 字节）                            │
│ target 字符串（如 "sm_90"，target_len 字节）            │
│ payload_cnt × 24 字节的「payload 记录表」               │
│ entry_cnt   × 24 字节的「entry 记录表」                 │
│ 每个 payload 的名字（8 字节对齐）+ 数据（8 字节对齐）    │
│ 每个 entry 的符号字符串（8 字节对齐）                    │
└────────────────────────────────────────────────────────┘
```

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

序列化时先写一份占位 header，再追加名字/target/两张定长记录表，再追加所有变长字符串与数据（顺便回填记录表里的偏移），最后回填 header 里的 `total_len`、计数等字段。解析则是严格的逆过程，并对每一步做长度校验，任何截断或非法 magic 都会返回 `ArtifactError` 而非 panic。

注意长度约束：计数与长度字段多为 `u16`，偏移与 payload 长度为 `u32`。任何超出都会被 `checked_u16`/`checked_u32` 拒绝，因此格式本身限制了单个 bundle 的规模上限（约 \(2^{16}\) 个 payload、单 payload 约 \(2^{32}\) 字节）。

#### 4.1.3 源码精读

格式常量集中在文件顶部：

[crates/oxide-artifacts/src/lib.rs:14-22](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/oxide-artifacts/src/lib.rs#L14-L22) — 定义了 `.oxart` 段名、`.oxlink`（仅锚符号占位段）段名、8 字节 magic `"OXIDEART"`、版本号 `1`，以及三组定长尺寸（header 32、payload 记录 24、entry 记录 24）。

payload 与 entry 的「种类」枚举各自带 `to_u16`/`from_u16`：

[crates/oxide-artifacts/src/lib.rs:24-51](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/oxide-artifacts/src/lib.rs#L24-L51) — `ArtifactPayloadKind` 把 PTX/NvvmIr/Ltoir/Cubin 映射成稳定 wire 值 `0x100/0x110/0x120/0x200`。未知值解析时返回 `UnsupportedPayloadKind` 错误，保证未来新增种类不会被静默误读。

序列化的核心是 `build_artifact_blob`：

[crates/oxide-artifacts/src/lib.rs:287-322](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/oxide-artifacts/src/lib.rs#L287-L322) — 先 `validate_spec`（拒绝空名/空 target/空 payload），预留 32 字节 header，追加 bundle 名字与 target，再预留两张记录表的空间；随后遍历每个 payload，先写它的名字串（8 字节对齐）再写数据（8 字节对齐），用 `checked_u32` 把真实偏移回填进记录表。

回填 header 的字段：

[crates/oxide-artifacts/src/lib.rs:341-362](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/oxide-artifacts/src/lib.rs#L341-L362) — 最后写入 magic、version、header_len、`total_len`、name/target 长度与 payload/entry 计数，至此 blob 自洽。

反序列化由 `parse_artifact_section` 驱动，它处理「一个段里串联多个 blob + 末尾零填充」的真实情况：

[crates/oxide-artifacts/src/lib.rs:366-378](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/oxide-artifacts/src/lib.rs#L366-L378) — 循环里先读 `artifact_blob_total_len` 决定切多长；遇到「整段都是 0」的尾部填充则提前 `break`，避免把 ELF 段对齐的零字节误判成坏 blob。`tests::artifact_section_parses_concatenated_blobs` 与 `artifact_section_ignores_trailing_zero_padding` 锁定了这两条行为。

单个 blob 的严格解析：

[crates/oxide-artifacts/src/lib.rs:380-440](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/oxide-artifacts/src/lib.rs#L380-L440) — 校验 magic、version、header_len；用 `total_len` 截断 blob 后，按 header 给出的计数读取 payload 记录表与 entry 记录表，再按记录里的偏移取出名字串与数据。每一步都过 `require_len`/`read_slice`，截断会得到 `Truncated` 而非越界 panic。

#### 4.1.4 代码实践

**实践目标**：用 `oxide-artifacts` 的公开 API 亲手构造一个 bundle，再把它读回来，验证 wire 格式确实自描述。

**操作步骤**（这是一个「最小调用示例」，可作为 `oxide-artifacts` 的 doctest 风格练习）：

```rust
// 示例代码：演示 build/parse 往返，非项目原有代码
use oxide_artifacts::{
    build_artifact_blob, parse_artifact_section,
    ArtifactBundleSpec, ArtifactEntrySpec, ArtifactPayloadSpec,
    ArtifactEntryKind, ArtifactPayloadKind,
};

let spec = ArtifactBundleSpec::new("demo", "sm_90")
    .with_payload(ArtifactPayloadSpec::new(
        ArtifactPayloadKind::Ptx, "demo.ptx", b".version 8.0\n... 真实 PTX ...",
    ))
    .with_entry(ArtifactEntrySpec::new("vecadd", ArtifactEntryKind::Kernel));

let blob = build_artifact_blob(&spec).unwrap();
let bundles = parse_artifact_section(&blob).unwrap();
```

**需要观察的现象**：
1. `blob[0..8]` 应等于 `b"OXIDEART"`，`blob[12..16]`（小端 u32）应等于 `blob.len()`。
2. 读回后 `bundles[0].name == "demo"`、`bundles[0].target == "sm_90"`，`payload(Ptx)` 返回的正是你写入的 PTX 字节，`entry("vecadd").kind == Kernel`。
3. 把 `b"..."` 改成空字节 `b""`，`build_artifact_blob` 应返回 `ArtifactError::EmptyPayload`——这印证了 4.1.2 提到的「空 payload 被拒」。

**预期结果**：往返完全相等（这正是单元测试 `artifact_blob_round_trips_ptx_payload` 断言的内容）。若你想直接读项目里的现成测试，打开 [crates/oxide-artifacts/src/lib.rs:815-831](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/oxide-artifacts/src/lib.rs#L815-L831) 即可看到等价断言。运行该测试：`cargo test -p oxide-artifacts artifact_blob_round_trips`（具体测试是否能在本机通过待本地验证，取决于是否装齐 nightly 工具链）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 magic `"OXIDEART"` 要占满 8 字节，而不是更短的 `"OX"`？
**答案**：8 字节 magic 既能在 `read_artifact_bundles_from_object_bytes` 扫描段时可靠定位 blob 起始，又恰好让 header 前 8 字节自然对齐；过短的 magic 在真实二进制里更容易被随机数据撞中，误判率更高。

**练习 2**：`parse_artifact_blob` 在校验 `total_len > bytes.len()` 时返回什么错误？为什么要这样设计而不是直接 panic？
**答案**：返回 `ArtifactError::Truncated("blob")`。因为运行时面对的是用户编出的二进制文件，数据可能损坏或不完整，用 `Result` 把错误向上传给宿主程序是安全且可恢复的；panic 会让整个 GPU 程序崩溃。`tests::artifact_section_rejects_truncated_blob_without_panicking` 专门守护这一行为。

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
        │    extern "C" {                            │
        │        #[link_name = "<同一个锚名>"]        │
        │        static ANCHOR: u8;                  │
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

[crates/reserved-oxide-symbols/src/lib.rs:117-134](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/reserved-oxide-symbols/src/lib.rs#L117-L134) — 顶部文档完整说明了 issue #72 的来龙去脉：纯数据目标文件不含符号会被归档忽略，解法是后端定义一个带此前缀的全局符号、宏在 `load()` 里读它的地址。注意 `246e25db` 与 kernel 前缀用的是同一个固定哈希后缀（见 u2-l1），表明这些保留符号同属一个内部命名空间。

[crates/reserved-oxide-symbols/src/lib.rs:214-220](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/reserved-oxide-symbols/src/lib.rs#L214-L220) — `artifact_anchor_symbol` 把包名、版本拼进符号，`push_symbol_sanitized` 把所有非 `[A-Za-z0-9_]` 字符替换为下划线，确保结果永远是合法链接器符号（例如 `julia-lib 0.1.0` → `..._julia_lib_0_1_0`）。v2 构造器 [crates/reserved-oxide-symbols/src/lib.rs:228-249](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/reserved-oxide-symbols/src/lib.rs#L228-L249) 追加 crate 名与（可选）二进制名。

后端一侧，写设备制品时同时定义锚符号：

[crates/rustc-codegen-cuda/src/lib.rs:823-855](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/rustc-codegen-cuda/src/lib.rs#L823-L855) — 先 `build_artifact_blob` 得到 `.oxart` 字节，再用 `artifact_anchor_symbol` 算出 legacy 锚名，调用 `oxide_artifacts::build_host_object_for_target(&blob, host_target, Some(&legacy_anchor))` 把字节连同锚符号一起包进宿主目标文件；owner 过滤场景则改用 `build_host_object_for_target_with_legacy_anchor` 同时给出 v2 强符号 + legacy 弱别名。注释明确指出「没有锚，库 crate 的 bundle 会被 dead-strip，`load()` 在运行时报 ModuleNotFound」。

`.oxart` 段与锚符号如何写进目标文件，是 `oxide-artifacts` 的职责：

[crates/oxide-artifacts/src/lib.rs:516-548](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/oxide-artifacts/src/lib.rs#L516-L548) — `build_host_object_for_target` 的文档完整解释了锚对**库** crate 的意义（归档成员惰性提取），并指出 host 侧 `#[cuda_module]` 宏会发出匹配的引用来强制提取。

[crates/oxide-artifacts/src/lib.rs:617-638](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/oxide-artifacts/src/lib.rs#L617-L638) — `build_host_object_with_section` 给 `.oxart` 段打上 `SHF_ALLOC | SHF_GNU_RETAIN` 标志（`SHF_GNU_RETAIN` 就是抵御 `--gc-sections` 的关键），锚符号用 `SymbolScope::Linkage`（全局绑定可触发归档提取，但又对动态符号表隐藏，不泄漏到最终二进制的导出表）。

宏一侧的引用：

[crates/cuda-macros/src/lib.rs:629-642](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-macros/src/lib.rs#L629-L642) — 生成一段 `unsafe extern "C"` 块，用 `#[link_name = #anchor_name]` 指向那个锚，再 `black_box(addr_of!(...))` 把锚地址「读」出来。`black_box` 防止优化器把这次读取连同对外部符号的引用一起删掉，从而保证未解析引用真实存在。这段代码会插进 `load_named()`，所以只要用户程序调了 `load()`，就一定会带上这条引用。完整逻辑与各种边界（泛型 kernel、cfg-gated kernel、owner 过滤）的判定见 [crates/cuda-macros/src/lib.rs:564-643](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-macros/src/lib.rs#L564-L643)。

#### 4.2.4 代码实践

**实践目标**：用一个静态库 + C 链接器，亲眼看到「有锚引用 → 归档成员被提取、`.oxart` 被保留」与「无引用 → 被丢弃」的差别。

**操作步骤**：

1. 直接阅读项目里现成的端到端验证测试 [crates/oxide-artifacts/src/lib.rs:1092-1150](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/oxide-artifacts/src/lib.rs#L1092-L1150)（`weak_legacy_alias_extracts_artifact_from_static_archive`）。它用 `ar crs` 把含 `.oxart`+弱锚的目标文件打成 `libartifact.a`，再写一行 C：`extern const unsigned char legacy_anchor; int main(void){ return legacy_anchor; }`，用 `cc` 链接成 `app`，最后 `read_artifact_bundles_from_object_bytes(&app)` 断言 bundle 被正确提取。
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
**答案**：不会。泛型 kernel 是在**消费方** crate 单态化后才生成 PTX 的，定义方 crate 本身不产出 `.oxart`。若此时仍发锚引用，会变成一条永远无法解析的未定义符号导致链接失败。宏在 [crates/cuda-macros/src/lib.rs:588-592](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-macros/src/lib.rs#L588-L592) 用 `non_generic.is_empty()` 判断，只有存在非泛型 kernel 时才发引用。

---

### 4.3 运行时 bundle 发现

#### 4.3.1 概念说明

字节进了二进制、锚也保住了它，剩下的问题就是：**运行时怎么把它找回来？**

cuda-oxide 的设计是「自省当前可执行文件」——程序启动后，宿主运行时打开**自己**这个二进制文件（`/proc/self/exe` 或等价路径），像解析普通 ELF 一样扫描所有段，挑出名为 `.oxart` 的段，把里面的 blob 反序列化成 `OwnedArtifactBundle` 列表。这样发布时只需要拷贝一个二进制文件，PTX 已经「焊」在里面了，无需额外的 `.ptx`/`.cubin` 旁车文件。

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
   4. 收集成 Vec<OwnedArtifactBundle>
```

关键点：发现逻辑对「段名」敏感，对「文件路径」不敏感。`artifact_bundles_from_binary_path` 接受任意路径，因此单元测试可以直接拿一个手工构造的目标文件做往返，而不必真的链接一个可执行文件。

#### 4.3.3 源码精读

入口「从当前可执行文件发现」：

[crates/cuda-core/src/embedded.rs:51-56](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/embedded.rs#L51-L56) — `artifact_bundles_from_current_exe` 用 `std::env::current_exe()` 拿到自身路径，把读取失败包成 `EmbeddedModuleError::CurrentExe`。

读取并交给 `oxide-artifacts`：

[crates/cuda-core/src/embedded.rs:58-68](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/embedded.rs#L58-L68) — `artifact_bundles_from_binary_path` 读出整个文件字节，调 `read_artifact_bundles_from_object_bytes`，IO 错误映射成 `EmbeddedModuleError::Io { path, source }`，格式错误映射成 `Artifacts`。

实际扫描 ELF 段的代码：

[crates/oxide-artifacts/src/lib.rs:493-514](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/oxide-artifacts/src/lib.rs#L493-L514) — 用 `object` crate 解析字节流，遍历 `file.sections()`，**只**处理名字等于 `.oxart` 的段，对每个段的数据调 `parse_artifact_section`（即 4.1 里的串联解析器），把所有 bundle 汇总返回。这一函数是「运行时发现」与「wire 格式」之间的唯一粘合点。

端到端的集成验证：

[crates/cuda-core/src/embedded.rs:210-263](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/embedded.rs#L210-L263) — `artifact_bundles_from_binary_path_reads_linked_executable` 构造一个带锚的 `.oxart` 目标文件，用 `rustc` 的 `link-arg` 把它链进一个空 `main`，生成可执行文件 `host`，再断言从 `host` 里能读回那个 bundle。注释里特意点明「镜像生产环境：后端总是在制品目标文件里定义一个链接锚符号」。

#### 4.3.4 代码实践

**实践目标**：理解 `cargo oxide pipeline vecadd` 产出的中间产物里，`.ll`（LLVM IR）与 `.ptx` 是文件形态的「可见制品」，而真正进二进制的是 `.oxart` 段。

**操作步骤**：

1. 进入 vecadd 示例目录，运行 `cargo oxide pipeline vecadd`（参考 u1-l3）。该命令会打开全部诊断开关（`CUDA_OXIDE_VERBOSE`/`DUMP_MIR`/`DUMP_LLVM`）做一次 release 构建，见 [crates/cargo-oxide/src/commands.rs:1053-1122](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cargo-oxide/src/commands.rs#L1053-L1122)。
2. 构建结束后，`show_generated_artifacts` 会把 `vecadd.ll` 与 `vecadd.ptx` 打印到 stdout，见 [crates/cargo-oxide/src/commands.rs:2426-2450](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cargo-oxide/src/commands.rs#L2426-L2450)。这两份文件是设备代码的**文本形态**。
3. 对构建出的可执行文件（通常在该示例的 `target/release/` 下，名为 `vecadd`）执行 `readelf -S ./vecadd | grep oxart` 或 `objdump -h ./vecadd | grep oxart`，应能看到一个名为 `.oxart` 的段。

**需要观察的现象**：
- `.ptx` 文件里能看到 `.version`、`.target`、kernel 入口符号。
- `readelf` 输出里存在 `.oxart` 段，其大小与 PTX 字节数（加上 bundle header/记录表）量级相当。

**预期结果 / 待本地验证**：能定位到 `.oxart` 段即说明 4.2 的锚握手成功保住了它。若 `readelf` 看不到该段，多半是发布构建被 `--gc-sections` 裁掉（对应 SHF_GNU_RETAIN 未生效），或本机工具链版本不符（u1-l3 提到需 llc-21 以上）。具体段大小、偏移以本机实际输出为准。

> 提示：你看到的 `.ptx`/`.ll` 是文件，而 `.oxart` 是二进制内的段——本讲关心的「内嵌」指的是后者。两者内容同源（都是后端编出的设备代码），但承载形态不同。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `artifact_bundles_from_current_exe` 要读「整个文件」而不是用 `dlsym` 之类按符号查找？
**答案**：因为我们要的是整个 `.oxart` **段**（里面可能有多个 bundle、多段 payload），而不是某个单一符号的地址。按段扫描能一次性拿到全部设备代码，且不依赖动态符号表（锚符号是 `Linkage` 隐藏的，本来就不在动态表里）。

**练习 2**：`read_artifact_bundles_from_object_bytes` 遍历段时用 `name != ARTIFACT_SECTION_NAME` 跳过非 `.oxart` 段。如果二进制里有多个 `.oxart` 段会怎样？
**答案**：每个都会被独立解析，结果 `extend` 进同一个 `Vec`。这正是「跨 crate 的 bundle 各自独立内嵌」的运行时体现——一个二进制可以同时携带多个 crate 的设备代码。

---

### 4.4 `load_embedded_module`

#### 4.4.1 概念说明

发现到 `OwnedArtifactBundle` 列表后，最后一公里是：**挑出正确的 bundle、挑出可加载的 payload、交给 CUDA 驱动变成 `CudaModule`**。

这一步有两层实现：

- `cuda-core` 提供了一个**只认 cubin/PTX** 的极简 `load_embedded_module`——因为 `cuda-core` 是「不依赖 libNVVM/LTOIR 二次编译」的薄运行时。
- `cuda-host` 重新导出同名函数，但它的 `load_bundle` 额外支持 NVVM IR 与 LTOIR：遇到这两种 payload 时，会按当前 GPU 的执行架构把 IR 二次编译成 cubin 或 PTX，再交给驱动。它还提供 `load_all_ptx_bundles_merged`，把跨 crate 的多份 PTX 合并成单个模块——这对**泛型 kernel** 至关重要。

回顾 u2-l1：`#[cuda_module]` 生成的 `load()` 三步走里，第 2 步「解析 PTX」实际调用的就是 `cuda-host` 的 `load_embedded_module`；第 3 步「逐 kernel 调 `cuModuleGetFunction`」用的就是本步返回的 `CudaModule`。

#### 4.4.2 核心流程

`cuda-host` 的加载优先级（按 payload 种类短路返回）：

```
load_bundle(ctx, bundle)
  ├─ 有 Cubin   → cuModuleLoadData(cubin)        直接加载
  ├─ 有 PTX     → cuModuleLoadData(ptx)          直接加载
  ├─ 有 NvvmIr  → 按 execution_route 编译成 cubin 或 ptx → 加载
  ├─ 有 Ltoir   → 链接成 cubin 或 ptx            → 加载
  └─ 全无        → UnsupportedPayload 错误
```

「执行路由（execution_route）」决定 NVVM IR / LTOIR 最终走 cubin 还是 PTX 桥接：它比较「bundle 编译时的目标架构」与「当前 GPU 实际执行架构」，得出 `Cubin` 或 `PtxBridge`。一个为 `sm_86` 编译的标准 payload 在 Blackwell GPU 上可能被转成 PTX 由驱动 JIT，这正是源码注释强调的兼容性路径。

#### 4.4.3 源码精读

`cuda-host` 的命名加载入口：

[crates/cuda-host/src/embedded.rs:51-62](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-host/src/embedded.rs#L51-L62) — `load_embedded_module(ctx, name)` 从当前可执行文件发现全部 bundle，按 `bundle.name == name` 找到目标 bundle，找不到则 `ModuleNotFound`，再交给 `load_bundle`。注意这里 `pub use cuda_core::embedded::{...}`（[第 9-12 行](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-host/src/embedded.rs#L9-L12)）复用了 `cuda-core` 的发现能力，避免重复实现。

payload 优先级与二次编译：

[crates/cuda-host/src/embedded.rs:135-178](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-host/src/embedded.rs#L135-L178) — `load_bundle` 依次尝试 Cubin → PTX → NvvmIr → Ltoir，命中即 `ctx.load_module_from_image(image)`（u3-l1 里讲过的 `CudaContext` 安全封装，底层是 `cuModuleLoadData`）。NVVM/LTOIR 分支会先用 `target_arch_for_bundle` 与 `ltoir::execution_arch_for_context` 算出编译/执行架构，再用 `execution_route` 决定产出 cubin 还是 PTX。这是 `cuda-host` 区别于 `cuda-core` 的核心增值能力。

跨 crate PTX 合并（泛型 kernel 的救星）：

[crates/cuda-host/src/embedded.rs:76-119](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-host/src/embedded.rs#L76-L119) — `load_all_ptx_bundles_merged` 把当前二进制里**所有** PTX bundle 拼成单个 CUDA 模块：第一份保留 `.version`/`.target`/`.address_size` 头，其余去掉重复头再拼接。文档解释了为什么需要它——泛型 kernel 在**消费方** crate 单态化，其 PTX 落在消费方的 bundle 而非定义方 bundle，只有把所有 PTX bundle 合并，才能保证任意 kernel 符号都能被 `cuModuleGetFunction` 找到。

`cuda-core` 的极简版本（只认 cubin/PTX）：

[crates/cuda-core/src/embedded.rs:77-104](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/embedded.rs#L77-L104) — `cuda-core` 版的 `load_embedded_module` 用 `loadable_payload` 只挑 cubin 或 PTX（[第 100-104 行](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-core/src/embedded.rs#L100-L104)），完全不带 NVVM/LTOIR 二次编译。这种「分层」让不需要 libNVVM 的场景可以只用 `cuda-core`。

错误模型：

[crates/cuda-host/src/embedded.rs:18-43](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/cuda-host/src/embedded.rs#L18-L43) — `EmbeddedModuleError` 用 `thiserror` 把「发现/格式错误（Core）」「找不到（ModuleNotFound）」「无可加载 payload（UnsupportedPayload）」「NVVM/LTOIR 编译失败（Ltoir）」「驱动拒绝（Driver）」分门别类，调用方可以精确区分「bundle 没找到」与「bundle 在但驱动加载失败」。

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
   - `[12:16]`（小端 u32）是否等于文件总长？
   - `[20:22]` 的 payload 计数、`[22:24]` 的 entry 计数是否符合预期（vecadd 通常 1 个 PTX payload + 1 个 kernel entry）？
4. 比对 `vecadd.ptx` 的字节数与 bundle 里某条 payload 记录的 `data_len` 字段：两者应当相等——这证明 `.ptx` 文件里的内容就是被嵌进 `.oxart` 的那段 payload。

**需要观察的现象**：
- `hexdump` 开头能看到 `OXIDEART` magic。
- payload 记录表里有一条 `kind = 0x100`（PTX）的记录，其 `data_len` 与 `vecadd.ptx` 大小一致。
- entry 记录表里有一条 `kind = 1`（Kernel）的记录，符号串为 `vecadd`（即 u2-l1 讲过的剥前缀 PTX 入口名）。

**预期结果 / 待本地验证**：能逐一指认 magic、total_len、payload 记录、entry 记录四类字段，即说明你已彻底掌握 `.oxart` 的 wire 格式与运行时加载链。若 `objcopy` 不可用，可改为阅读 [crates/oxide-artifacts/src/lib.rs:380-474](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/oxide-artifacts/src/lib.rs#L380-L474) 的解析顺序，按它读字段的次序手工对照 hexdump。

#### 4.4.5 小练习与答案

**练习 1**：`cuda-host` 的 `load_bundle` 为什么把 Cubin 排在 PTX 前面？
**答案**：cubin 是已针对具体架构汇编好的二进制，驱动加载它无需任何 JIT，启动最快、兼容性最确定；PTX 还需驱动即时编译。同一 bundle 若同时带了 cubin 与 PTX，优先用 cubin 是性能与确定性兼顾的选择。

**练习 2**：为什么泛型 kernel 场景必须用 `load_all_ptx_bundles_merged` 而不是 `load_embedded_module`？
**答案**：泛型 kernel 在消费方 crate 单态化，PTX 落在消费方的 bundle；调用方通常只知道定义方 crate 的名字，`load_embedded_module(ctx, "定义方")` 找不到消费方 bundle 里的那份 PTX。`load_all_ptx_bundles_merged` 不按 crate 名筛选，而是把全部 PTX bundle 合并成一个模块，于是任何 kernel 符号都在同一个 `CudaModule` 里可解析。

**练习 3**：`EmbeddedModuleError::UnsupportedPayload` 与 `ModuleNotFound` 分别在什么场景下触发？
**答案**：`ModuleNotFound` 是「bundle 没找到」（名字对不上，或锚握手失败导致整段被裁）；`UnsupportedPayload` 是「bundle 找到了，但里面没有任何驱动能加载的 payload」。区分两者能帮你快速定位是「链接/嵌入」问题还是「payload 种类」问题。

---

## 5. 综合实践

把本讲四个模块串成一个端到端排查任务：

**场景**：你新写了一个 lib crate `my_kernels`，里面有一个 `#[cuda_module]` 和一个非泛型 `#[kernel]`。另一个二进制 crate 依赖它并调用 `kernels::load(&ctx)`。发布构建（`cargo oxide build --release`）后运行，却收到 `ModuleNotFound`。

**任务**：用本讲学到的知识定位根因，并给出验证步骤。

**参考排查路径**：

1. **先确认 bundle 是否真的进了二进制**（对应 4.3）：对最终可执行文件跑 `readelf -S | grep oxart`。
   - 若**没有** `.oxart` 段 → bundle 没被嵌入，问题在链接阶段，跳到第 2 步。
   - 若**有** → bundle 在，问题在加载/命名，跳到第 3 步。

2. **排查锚握手**（对应 4.2）：bundle 没进二进制，最可能是 lib crate 的归档成员没被提取。
   - 检查宏是否真的发出了锚引用：grep `my_kernels` 的展开（`cargo expand`）里有没有 `cuda_oxide_artifact_anchor_246e25db_my_kernels_...` 与 `black_box`。
   - 检查后端是否定义了同名锚：构建时打开 `CUDA_OXIDE_VERBOSE=1`，看日志里的 `Embedded artifact object complete` 路径，对该目标文件跑 `readelf -s` 应能看到该锚符号且为已定义全局。
   - 两侧名字对不上（例如版本号、包名替换规则不一致）会导致引用永远无法命中，归档成员被丢弃——这正是 issue #72。

3. **排查加载链**（对应 4.4）：bundle 在但 `load()` 报错。
   - 若报 `ModuleNotFound`：检查传给 `load_embedded_module` 的 `name` 是否等于 bundle 的 `name`（默认是 `CARGO_PKG_NAME`）。
   - 若报 `UnsupportedPayload`：bundle 里没有 cubin/PTX（可能是只编了 NVVM IR 但用了 `cuda-core` 而非 `cuda-host`，或 execution_route 选不出合法目标）。

4. **格式核验**（对应 4.1）：若怀疑 bundle 损坏，把 `.oxart` 段 dump 出来，确认 magic/version/计数合理，payload 记录的 `data_len` 与 PTX 大小一致。

**预期产出**：一张「症状 → 可能根因 → 验证命令 → 修复方向」的排查表。完成这张表，你就把本讲的「格式 → 锚 → 发现 → 加载」四环完全打通了。

## 6. 本讲小结

- `.oxart` 是一个自描述的 wire 格式：固定 32 字节 header（含 `OXIDEART` magic、版本、total_len、计数）+ 变长的名字/target + 两张各 24 字节的定长记录表（payload 与 entry）+ 对齐的数据区；`oxide-artifacts` 同时负责序列化、反序列化与「包成宿主目标文件」。
- **锚符号握手**是 lib crate bundle 不被链接器裁剪的关键：后端在 `.oxart` 段首定义全局锚 `cuda_oxide_artifact_anchor_246e25db_<pkg>_<ver>`（段加 `SHF_GNU_RETAIN`），`#[cuda_module]` 宏在 `load()` 里用 `black_box(addr_of!(#[link_name=...]))` 发出匹配引用，强制归档提取并抵御 `--gc-sections`。
- 运行时**自省当前可执行文件**：`cuda-core` 用 `object` crate 解析自身 ELF，扫描所有名为 `.oxart` 的段，调 `parse_artifact_section` 还原出 `OwnedArtifactBundle` 列表。
- **两层加载分工**：`cuda-core` 的 `load_embedded_module` 只认 cubin/PTX（薄运行时）；`cuda-host` 的 `load_bundle` 额外支持 NVVM IR/LTOIR 的二次编译，并提供 `load_all_ptx_bundles_merged` 解决泛型 kernel 跨 crate 的 PTX 归并问题。
- 错误模型清晰区分「没找到 bundle（`ModuleNotFound`）」「bundle 在但无可用 payload（`UnsupportedPayload`）」「编译/驱动失败（`Ltoir`/`Driver`）」，便于运行时定位。

## 7. 下一步学习建议

- 想看「加载完之后内核如何被启动」，继续 [u2-l4 从宿主启动内核](u2-l4-launching-kernels.md)，它讲解拿到 `CudaModule` 后的参数 marshalling 与 `cuLaunchKernel`。
- 想深入 NVVM IR/LTOIR 这条二次编译支线，阅读 `crates/cuda-host/src/ltoir.rs`（本讲仅作为黑盒引用了它的 `execution_route`、`build_cubin_from_nvvm_ir` 等），它属于编译流水线的尾部，建议在学完 U4「编译流水线总览」后再回看。
- 想理解「为什么有些 crate 的设备代码会被 owner 过滤跳过」，回到 [crates/rustc-codegen-cuda/src/lib.rs:525-554](https://github.com/NVlabs/cuda-oxide/blob/52e7078d255e1b085566095c39f5c8a697b5125f/crates/rustc-codegen-cuda/src/lib.rs#L525-L554) 看 `CUDA_OXIDE_DEVICE_CODEGEN_CRATE` 如何让后端只写锚占位文件而不写 `.oxart`，这条线在 U4 的「后端入口与 host/device 分流」会系统讲解。
- 下一讲 [u3-l3 异步执行模型](u3-l3-async-execution-model.md) 将离开「加载」话题，进入 cuda-async 的惰性 `DeviceOperation` 与 `DeviceFuture` 三态机。
