# compile_fail 与设备/启动安全契约

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 cuda-oxide 用**两条负向验证流水线**（`trybuild` 的 `compile_fail` 与 `smoketest.sh` 的 `error` 分类）固化安全契约的机制，以及它们各自能拦住什么、拦不住什么。
- 区分**设备端 codegen 契约**（在 `cargo oxide` 编译期由后端拒绝，如 `error_missing_device_attr`、`error_wgmma_mma_unimplemented`）与**宿主/宏端类型契约**（由 `rustc` 普通类型检查在更早阶段拒绝，如启动契约的品牌化、秩、私有字段）。
- 用真实源码读懂 `cuda_module_contract` 这个「正面样本」如何同时把启动契约、动态共享内存对齐合并、`#[launch_bounds]` 传播、泛型单态化等约束一次性验证通过，并理解它对照的负向测试为什么必须存在。
- 看懂 `#318`/`#324` 新增的一批负向测试（`fail_wrong_brand`、`fail_wrong_rank`、`fail_private_construction`、`launch_contract_fake_disjoint_slice`、`cuda_module_duplicate_nested_kernel` 等）各自由**哪条契约**拦截。
- 为自己新增的安全约束补一个 `compile_fail` 用例，把「现在能编译、但其实不安全」的模式提前钉死在编译期。

本讲承接 [u2-l1](u2-l1-kernel-and-cuda-module-macros.md)（宏与 `#[launch_contract]`）和 [u2-l2](u2-l2-thread-indexing-and-safety.md)（线程索引与品牌化 sealed trait），把它们落到「契约如何被测试」这一工程层面。

## 2. 前置知识

- **正定测试（positive test）与负定测试（negative test）**：前者断言「正确的代码能编译/能跑出正确结果」，后者断言「错误的代码**必须**编译失败」。负定测试是安全契约的「保险丝」——一旦哪天有人改坏了诊断逻辑、让不安全模式静默通过，负定测试会立刻变红。
- **`trybuild`**：Rust 生态里专门做「编译期单测」的 crate。你给它一个 `foo.rs` 和一份预期的 `foo.stderr`，它会真的去编译 `foo.rs`，把真实编译器输出与 `.stderr` 逐字节比对，完全一致才通过。这样就把「某段代码必须以某个错误码/某段提示失败」变成了可回归的断言。
- **「能编译」≠「能跑」**：cuda-oxide 里很多安全契约发生在 `cargo oxide` 驱动的设备 codegen 阶段（比标准 `rustc` 类型检查更晚），所以负向测试分两层：能在普通 `rustc` 阶段拦下的走 `trybuild`，必须等到设备后端才能拦下的走 `error_*` 示例 + `smoketest.sh`。
- **品牌化（branding）**：用一个类型参数（「品牌」）把「这条证明属于哪个 kernel」编进类型，使两份证明即使在结构上相同也不能互换。这是本讲负向测试最频繁出现的概念，不熟可回看 [u2-l2](u2-l2-thread-indexing-and-safety.md)。
- **sealed trait**：把 trait 的实现权锁死在 crate 内部（用一个私有 `Sealed` 子 trait 作超类型约束），外部无法为自己的类型实现该 trait。cuda-oxide 用它保证只有「真正的 `DisjointSlice`」能通过启动契约的品牌校验。

## 3. 本讲源码地图

| 文件 | 作用 |
|:-----|:-----|
| `crates/cuda-core/tests/launch_contract_types.rs` | cuda-core 侧 `trybuild` 入口，注册 4 个 `fail_*` 启动契约负向测试 |
| `crates/cuda-core/tests/launch_contract/fail_wrong_brand.rs`（及同目录其余 `fail_*.rs`） | 启动契约类型层负向测试源码 |
| `crates/cuda-macros/tests/launch_contract_semantics.rs`、`macro_guard.rs` | cuda-macros 侧 `trybuild` 入口，注册品牌化、命名、嵌套模块、`unsafe` 等负向测试 |
| `crates/cuda-macros/tests/compile_fail/*.rs` | 宏层负向测试源码（含 `launch_contract_fake_disjoint_slice`、`cuda_module_duplicate_nested_kernel` 等） |
| `crates/rustc-codegen-cuda/examples/cuda_module_contract/src/main.rs` | 正面样本：一个 `#[cuda_module]` 同时承载多条启动契约并验证 PTX 落地 |
| `crates/rustc-codegen-cuda/examples/error_missing_device_attr/src/main.rs` | 设备 codegen 负向测试：helper 缺 `#[device]` |
| `crates/rustc-codegen-cuda/examples/error_wgmma_mma_unimplemented/src/main.rs` | 设备 codegen 负向测试：未实现的 `wgmma.mma_async` lowering |
| `crates/cuda-device/src/disjoint.rs` | 品牌化 sealed trait `__LaunchContractDisjointSlice` 的定义与实现集 |
| `scripts/smoketest.sh` | 端到端冒烟脚本，含 `error` 分类与 `verdict_error` 判定逻辑 |
| `cuda-oxide-book/gpu-safety/the-safety-model.md` | 安全模型总纲（三层 tier） |

---

## 4. 核心概念与源码讲解

### 4.1 compile_fail 测试机制：两条负向验证流水线

#### 4.1.1 概念说明

cuda-oxide 的安全契约分布在编译流水线的**不同阶段**，因此负向测试不能用单一机制覆盖。它用两条互补的流水线：

1. **`trybuild` 流水线**：捕获一切「标准 `rustc` 类型检查 / 借用检查 / 隐私检查」阶段就能拒绝的错误。典型是启动契约的类型层证明（品牌不匹配、秩不符、字段私有、sealed trait 不满足）和宏层结构错误（保留符号冲突、嵌套模块重名）。这些错误**不需要设备后端**，普通 `cargo test` 即可运行，速度快、可在无 GPU 的 CI 上跑。

2. **`error_*` 示例 + `smoketest.sh` 流水线**：捕获只有「设备 codegen 后端」才能诊断的错误——比如 helper 函数缺 `#[device]` 导致 `thread::index_1d()` 落到 panic 桩、或某个 PTX intrinsic 的 lowering 尚未实现。这些错误发生在 `cargo oxide` 驱动的 codegen 阶段，必须用 `cargo oxide build/run` 才能触发，因此以「示例」形式存在，由 `smoketest.sh` 统一调度并按 `error` 分类判定。

一句话区分：**能用 `rustc` 普通类型系统拦住的 → `trybuild`；必须等设备后端拦住的 → `error_*` 示例。**

#### 4.1.2 核心流程

**`trybuild` 流水线**（以 cuda-core 为例）：

```
cargo test -p cuda-core
  └─ launch_contract_types::launch_contract_types_fail_closed
       └─ trybuild::TestCases::compile_fail("tests/launch_contract/fail_wrong_brand.rs")
            └─ 真编译 fail_wrong_brand.rs
            └─ 取真实 stderr
            └─ 与 fail_wrong_brand.stderr 逐字节比对 → 一致才 PASS
```

每个 `fail_*.rs` 都配一份同名的 `fail_*.stderr`，里面写死期望的编译器输出（含错误码 `E0308`/`E0277`/`E0451`/`E0616`/`E0133` 和指向具体行号的注解）。`trybuild` 提供 `TRYBUILD=overwrite` 来重写期望文件，但日常 CI 是严格比对模式。

**`error_*` 流水线**：

```
smoketest.sh
  └─ 对每个 example 调 classify() → error_* 归入 "error" 分类
  └─ cargo oxide build <example>，捕获日志与退出码
  └─ verdict_error(log, ec):
        日志含 'Device codegen failed|Translation failed|Compilation error|Unsupported construct'  → PASS
        否则 ec≠0 且日志含 '^error(\[|:)|could not compile|aborting due to'                          → PASS
        ec==0（竟然编译成功）                                                                          → FAIL（回归！）
```

关键设计：`verdict_error` 对「假阳性」很严格——单纯 `exit=42` 而没有任何编译错误行**不算**通过（见脚本注释 L306-309），必须有真实的 `error:` 诊断行。这避免「随便崩一下就当通过」。

#### 4.1.3 源码精读

cuda-core 的 `trybuild` 入口极其简洁，4 行注册 4 个负向测试：

[cuda-core/tests/launch_contract_types.rs:L7-L12](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/tests/launch_contract_types.rs#L7-L12) —— 新建 `TestCases`，逐个声明「这些文件必须编译失败」。

cuda-macros 的入口按主题拆成了两个 runner，`launch_contract_semantics.rs` 专管启动契约语义（品牌化、伪造 slice、不可信 loader），`macro_guard.rs` 专管宏结构守卫（保留符号、嵌套模块、`unsafe` 要求）：

[cuda-macros/tests/launch_contract_semantics.rs:L12-L16](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/tests/launch_contract_semantics.rs#L12-L16) —— 注册 5 个启动契约语义负向测试。

[cuda-macros/tests/macro_guard.rs:L23](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/tests/macro_guard.rs#L23) · [L29-L30](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/tests/macro_guard.rs#L29-L30) —— 嵌套模块重名（L23）与文件级/`include!` 模块边界（L29-30）的负向测试注册。

`smoketest.sh` 用一个数组枚举所有 `error_*` 示例，判定函数把「编译如期失败」翻译成 PASS：

[scripts/smoketest.sh:L48](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/scripts/smoketest.sh#L48) —— `ERROR_EXAMPLES` 数组，列出所有预期编译失败的示例。

[scripts/smoketest.sh:L299-L319](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/scripts/smoketest.sh#L299-L319) —— `verdict_error`：先看是否命中设备后端的失败标志词（L302），再看是否有标准 rustc 错误行（L310），`ec==0`（编译成功）直接判 FAIL（L314-315），即「负定测试变绿才是回归」。

#### 4.1.4 代码实践

**实践目标**：亲手跑通两条负向流水线，观察「契约被破坏时测试如何变红」。

操作步骤：

1. 进入 `crates/cuda-macros/tests/compile_fail/launch_contract_fake_disjoint_slice.rs`，把第 9 行本地伪造的 `DisjointSlice` 结构体的字段名改一下（例如把 `ptr` 改成 `p`），人为让 `.stderr` 失配。
2. 运行 `cargo test -p cuda-macros --test launch_contract_semantics`，观察 `trybuild` 报告「actual stderr 与 expected 不一致」。
3. 还原改动，确认测试再次通过。
4. 运行 `scripts/smoketest.sh error_missing_device_attr`（无 GPU 也可，因为只看编译失败），观察输出 `PASS (expected compile failure)`。
5. 临时把 `error_missing_device_attr/src/main.rs` 第 39 行的 `fn helper` 加上 `#[device]`（注：需 `use cuda_device::device;`），让它「不再错误」，再跑 smoketest，预期看到 `FAIL (compilation succeeded, expected failure)`——这就是负向测试拦住「诊断回归」的样子。**（实践后请还原，勿提交。）**

需要观察的现象：步骤 2 出现 `trybuild` 的 diff；步骤 5 smoketest 明确写出 `compilation succeeded, expected failure`。

预期结果：人为破坏契约 → 对应负向测试立即 FAIL；还原 → PASS。

> 待本地验证：步骤 4/5 需要 `cargo oxide` 工具链可用；若本机无 CUDA toolkit，至少步骤 1-3 的 `trybuild` 部分一定能跑。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `fail_wrong_brand` 用 `trybuild` 而不是 `error_*` 示例来测？
**答案**：跨品牌传递 `PreparedLaunch<KernelA>` 给期望 `PreparedLaunch<KernelB>` 的函数是普通的**类型不匹配**（`E0308`），标准 `rustc` 在类型检查阶段就能拒绝，根本走不到设备 codegen 后端，所以最适合用 `trybuild` 快速、无 GPU 地固化。

**练习 2**：`verdict_error` 为什么拒绝「`exit=42` 但日志无 `error:` 行」？
**答案**：因为没有诊断行的非零退出可能是脚本/环境问题（比如 `cargo oxide` 自身崩溃），并不能证明「设备后端如约拒绝了不安全代码」。要求真实 `error:` 行能避免「随便崩一下就当通过」的假阳性。

---

### 4.2 设备端 codegen 契约：error_* 负向测试

#### 4.2.1 概念说明

有些错误只有在「设备 codegen 后端」把 MIR 翻译成 dialect IR 时才能发现——它们不是 Rust 类型错误，而是「这段设备代码语义上不合法 / 还不支持」。两个典型代表：

- **`error_missing_device_attr`**：一个 helper 函数没有 `#[device]` 标注，却在体内调用了 `thread::index_1d()`。问题在于 `thread::index_1d()` 的公开桩函数体是 `unreachable!()`（设备 intrinsic 的真实逻辑要靠 `#[kernel]`/`#[device]` 宏改写注入）。在普通 helper 里它不会被改写，于是 MIR 内联后 helper 的函数体坍缩成一堆 panic 格式化代码，最终用户只会看到晦涩的 `Symbol ... not found`。cuda-oxide 为此专门加了诊断，要求后端**在 codegen 期就给出清晰错误**，而不是静默跳过坍缩的 helper。
- **`error_wgmma_mma_unimplemented`**：`wgmma_mma_m64n64k16_f32_bf16` 等 intrinsic 的完整 lowering 还没实现（需要 16+ 输出寄存器的寄存器分配）。后端必须**显式拒绝**这类调用并给出诊断，而不是静默 emit 一条注释、生成「乘累加结果恒为 0」的 PTX（那是比崩溃更糟的静默 miscompile）。

两者的共同点：**「编译期缺口即 bug」**——只要不安全或不支持的形状能溜过去，就是后端的失职，必须有诊断 + 有负向测试盯住它。

#### 4.2.2 核心流程

```
用户写错误代码
  └─ cargo oxide build  →  rustc 正常类型检查通过（代码本身类型合法）
  └─ 设备 codegen 后端扫描设备函数集
       ├─ 发现 helper 缺 #[device] 但调用了 index 桩  →  emit 专用 diagnostic，编译失败
       └─ 发现 wgmma_mma_* 调用走占位 lowering         →  emit "not yet implemented"，编译失败
  └─ smoketest verdict_error 命中 'Device codegen failed|...|Unsupported construct' → PASS
```

#### 4.2.3 源码精读

`error_missing_device_attr/src/main.rs` 的「罪魁代码」与期望诊断都写在文件头注释里，这是 cuda-oxide 负向测试的惯例——把**期望错误文本**钉死在 doc comment 中：

[error_missing_device_attr/src/main.rs:L36-L44](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/error_missing_device_attr/src/main.rs#L36-L44) —— `helper` 缺 `#[device]`，`thread::index_1d()` 落到 panic 桩。

[error_missing_device_attr/src/main.rs:L24-L29](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/error_missing_device_attr/src/main.rs#L24-L29) —— 文件头钉死的期望诊断文本：`` `thread::index_1d` only works inside `#[kernel]` / `#[device]` functions ``。注意第 58-61 行还故意打印「如果你看到这条消息，说明负向测试失效了」——这是 `verdict_error` 判 FAIL 的「正面证据」。

`error_wgmma_mma_unimplemented` 同理，内核故意调用占位 lowering：

[error_wgmma_mma_unimplemented/src/main.rs:L29-L39](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/error_wgmma_mma_unimplemented/src/main.rs#L29-L39) —— `unsafe` 内核调用 `wgmma_mma_m64n64k16_f32_bf16`。

[error_wgmma_mma_unimplemented/src/main.rs:L18-L19](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/error_wgmma_mma_unimplemented/src/main.rs#L18-L19) —— 期望诊断 `wgmma.mma_async lowering is not yet implemented; ...`。

这两个示例都属于 `ERROR_EXAMPLES` 数组，由 `smoketest.sh` 统一盯住（见 [4.1.3](#413-源码精读) 引用的 L48）。

#### 4.2.4 代码实践

**实践目标**：追踪「设备桩为什么必须在 codegen 期被识别」。

操作步骤：

1. 阅读 `crates/cuda-device/src/thread.rs`（参考 [u2-l2](u2-l2-thread-indexing-and-safety.md)），确认 `index_1d` 的公开桩是 `unreachable!()`，真实逻辑靠 `#[kernel]`/`#[device]` 宏改写。
2. 阅读 `error_missing_device_attr/src/main.rs` 注释，理解「helper 缺 `#[device]` → 桩被内联 → helper 坍缩成 panic 机器」的退化路径。
3. 在 `crates/rustc-codegen-cuda/src/device_codegen.rs` 中检索诊断字符串 `` `only works inside `#[kernel]` / `#[device]` ``，找到 emit 该错误的代码点，理解它是如何区分「真内核/真设备函数」与「普通 helper」的。
4. 运行 `cargo oxide build error_missing_device_attr`（待本地验证），确认错误出现在 codegen 阶段而非 `rustc` 类型检查阶段。

需要观察的现象：错误消息指向 `helper` 的定义，并带一条 note 指向 kernel 内的调用点、一条 help 建议加 `#[device]`。

预期结果：编译失败，诊断精准定位到 helper，而非旧的 `Symbol not found`。

> 待本地验证：步骤 3/4 需要 `cargo oxide` 工具链。

#### 4.2.5 小练习与答案

**练习 1**：假如 `wgmma_mma_*` 的占位 lowering 不是「显式报错」而是「emit 一条 PTX 注释 + 返回零」，`error_wgmma_mma_unimplemented` 这个负向测试还能拦住回归吗？
**答案**：不能。那样代码会**编译成功**且「能跑」，只是结果恒为 0，`verdict_error` 会判 `FAIL (compilation succeeded, expected failure)`——但这恰恰说明负向测试**成功**抓到了「静默 miscompile 回归」。换句话说，负向测试的设计意图就是：一旦有人把「显式报错」改成「静默放行」，测试立刻变红。

**练习 2**：为什么 `error_*` 用「示例目录」而不是 `trybuild`？
**答案**：这些错误依赖设备 codegen 后端，而 `trybuild` 用的是普通 `rustc`（无 cuda-oxide 后端），触发不到诊断。用 `cargo oxide` 驱动的示例 + `smoketest.sh` 才能把设备后端拉进测试链路。

---

### 4.3 启动契约的类型层固化：正面样本与品牌化负向测试

#### 4.3.1 概念说明

`#318` 引入的「类型化启动契约」把内核启动从「调用方每次自证」升级为「类型层引导 + 运行期一次性校验」。它的安全证明分散在多个类型机制上，每个机制都必须有负向测试盯住，否则任何一处松动都会让「免 `unsafe` 启动」变得不安全。本模块用 `cuda_module_contract` 作为**正面样本**（展示契约如何正确通过），再用一组 `fail_*` / `launch_contract_*` 作为**对照负向样本**（展示各条契约如何拦截违规）。

涉及的类型机制有四条：

1. **秩保持配置类型**：`LaunchConfig1D/2D/3D` 实现 sealed trait `KernelLaunchConfig`，把「尾随维度恒为 1」锁进类型；每个签约 kernel 的 `Config` 关联类型钉死一个秩。
2. **品牌化见证 `PreparedLaunch<Kernel>`**：`prepare_*` 的校验结果缓存进带 kernel 品牌的见证类型，此后同名启动方法才安全。品牌不可互换。
3. **私有字段**：`LaunchConfig1D` 的字段全部私有，外部既不能结构体字面量伪造、也不能改写，唯一入口是 `::new()`。
4. **品牌化 sealed trait `__LaunchContractDisjointSlice<Element, DOMAIN>`**：`#[cuda_module]` 给签约 kernel 的 `prepare_*` 加这个 bound，强制 `rustc` 先解析类型别名再校验 `DisjointSlice` 的元素类型与 domain，堵死「名字长得像但不是真 `DisjointSlice`」的伪造。

#### 4.3.2 核心流程

**正面路径**（`cuda_module_contract`）：

```
#[launch_contract(domain=1, block=(256,1,1), dynamic_shared=..., dynamic_shared_alignment=...)]
  └─ 宏生成 module.prepare_mixed_abi(LaunchConfig1D) -> PreparedLaunch<__mixed_abi_CudaKernel>
       └─ PreparedLaunch::__prepare 在活设备校验 block/shared/compute-cap/cluster/cooperative
  └─ module.mixed_abi(&stream, &launch, ...)  ← 安全（见证已带品牌）
  └─ verify_launch_contract_ptx() 逆向前端：检查 .ptx 是否含对齐后的 .extern .shared 声明
```

**负向拦截**（按违规类型分派）：

```
跨品牌：launch_b(&PreparedLaunch<KernelA>)              → E0308（fail_wrong_brand）
秩不符：prepare(LaunchConfig2D) where Config=LaunchConfig1D → E0308（fail_wrong_rank）
伪结构体：LaunchConfig1D { grid_x:1, block_x:32, ... }   → E0451（fail_private_construction）
改字段：  valid.block_x = 64                              → E0616（fail_private_mutation）
伪造 slice：本地 struct DisjointSlice 通过 prepare_*      → E0277（launch_contract_fake_disjoint_slice）
误导别名：use Index1D as Index2D 配 domain=2              → E0277（launch_contract_misleading_index_alias）
```

#### 4.3.3 源码精读

**正面样本**——`cuda_module_contract` 同时验证多条契约：

[cuda_module_contract/src/main.rs:L36-L58](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/cuda_module_contract/src/main.rs#L36-L58) —— 两个 entry 共享同一 helper `ordinary_shared_forward`，但声明不同的 `dynamic_shared_alignment`（32 与 256）。注释 L34-35 说明这是在测「helper 的单条 PTX 声明必须采用两个 caller 中更强的契约」（对齐合并，承接 [u2-l3](u2-l3-shared-memory-and-sync.md)）。

[cuda_module_contract/src/main.rs:L60-L77](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/cuda_module_contract/src/main.rs#L60-L77) —— `mixed_abi` 内核：混合 scalar / `&[f32]` / 裸指针 / `DisjointSlice` 四种宿主 ABI 形状，且有 `#[launch_bounds(256)]` + `#[launch_contract(domain=1, block=(256,1,1), dynamic_shared=0)]`。这是对「类型化模块宏必须正确 lowering 常见参数形状」的正面覆盖。

[cuda_module_contract/src/main.rs:L176-L187](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/cuda_module_contract/src/main.rs#L176-L187) —— 正面启动链路：先 `module.prepare_mixed_abi(LaunchConfig1D::new(...))?`（活设备校验），再安全的 `module.mixed_abi(&stream, &launch, ...)`。注意 `kernels::load(&ctx)` 在 L163 是 `unsafe`（签约模块 loader 的一次性绑定证明，见 [u3-l2](u3-l2-module-loading-and-embedded-artifacts.md)），而 `mixed_abi` 本身因持有 `PreparedLaunch` 见证而**不再**需要 `unsafe`。

[cuda_module_contract/src/main.rs:L219-L253](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/cuda_module_contract/src/main.rs#L219-L253) —— `verify_launch_contract_ptx()` 是「逆向前端」：直接读编译出的 `.ptx` 文件，断言动态共享内存声明被合并到 `align 128`（L223-230），且泛型/显式实例化的对齐与 launch bounds 都传播到位。它把「契约应影响最终 PTX」也变成了可回归的字符串断言。

**品牌化负向样本**——`fail_wrong_brand`，证「品牌不可互换」：

[fail_wrong_brand.rs:L29-L33](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/tests/launch_contract/fail_wrong_brand.rs#L29-L33) —— `launch_b` 收 `&PreparedLaunch<KernelB>`，却传入 `&PreparedLaunch<KernelA>`。两类型结构相同但品牌不同，`rustc` 给 `E0308`。期望 stderr 见 [fail_wrong_brand.stderr:L1-L3](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/tests/launch_contract/fail_wrong_brand.stderr#L1-L3)。

**秩负向样本**——`fail_wrong_rank`，证「尾随维度锁死」：

[fail_wrong_rank.rs:L8-L24](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/tests/launch_contract/fail_wrong_rank.rs#L8-L24) —— `OneDimensionalKernel` 的 `type Config = LaunchConfig1D`（L9），但 `prepare` 调用传入 `LaunchConfig2D`（L24）。关联类型把秩焊死，传错秩即 `E0308`。

**私有字段负向样本**——一对 `fail_private_*`，证「配置只能经 `::new` 构造」：

[fail_private_construction.rs:L4-L8](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/tests/launch_contract/fail_private_construction.rs#L4-L8) —— 结构体字面量伪造 `LaunchConfig1D`，三个字段全私有 → `E0451`。

[fail_private_mutation.rs:L4-L5](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/tests/launch_contract/fail_private_mutation.rs#L4-L5) —— 拿到合法实例后改 `block_x`，字段私有 → `E0616`。两者合起来封死了「绕过 `::new` 单点构造」的所有出口，使「尾随维度恒为 1」这个不变量无法被外部破坏。

**宏侧品牌化 sealed trait** 的定义与实现集，是上面 `E0277` 的依据：

[cuda-device/src/disjoint.rs:L101-L120](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/disjoint.rs#L101-L120) —— 私有 `launch_contract_sealed::Sealed` 守门（L101-103），只有真 `DisjointSlice` 实现 `Sealed`（L105）；品牌 trait `__LaunchContractDisjointSlice<Element, const DOMAIN: u8>` 以 `Sealed` 为超类型（L117-120），文档（L110-115）直言「sealed：只有真 `DisjointSlice` 能实现；`#[cuda_module]` 加这个 bound 让 Rust 先解析类型别名再校验 domain」。

[cuda-device/src/disjoint.rs:L122-L142](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/disjoint.rs#L122-L142) —— 实现集编码了「2D 索引空间也能支持 1D launch（Y 维全 1），但 1D 索引空间不能支持 2D launch」这条不对称规则（见 L114-115 注释）：`Index1D` 只 impl `DOMAIN=1`（L122），`Index2D<ROW_STRIDE>` 与 `Runtime2DIndex` 同时 impl `1` 和 `2`（L124-142）。

**宏侧品牌化负向样本**——两个最精巧的伪造：

[launch_contract_fake_disjoint_slice.rs:L8-L24](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/tests/compile_fail/launch_contract_fake_disjoint_slice.rs#L8-L24) —— 用户**在本地定义一个同名 `DisjointSlice`**（L9-13，字段布局甚至一模一样），并喂给签约 kernel（L21）。因为本地类型没有 impl sealed trait，`prepare_lookalike` 的 bound 失败 → `E0277`。期望 stderr 明确列出「只有以下真 `DisjointSlice` 变体 impl 该 trait」（[.stderr:L12-L17](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/tests/compile_fail/launch_contract_fake_disjoint_slice.stderr#L12-L17)）。

[launch_contract_misleading_index_alias.rs:L4-L15](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/tests/compile_fail/launch_contract_misleading_index_alias.rs#L4-L15) —— 更隐蔽：`use cuda_device::thread::Index1D as Index2D;`（L4）把 `Index1D` **改个别名**叫 `Index2D`，slice 写成 `DisjointSlice<u32, Index2D>`（L13），配 `domain = 2`。光看代码像 2D，实际是 1D。品牌 trait 的 impl 集不认别名，仍判 `E0277`——这正是 sealed trait 设计目标「让 Rust 先解析别名再校验」的体现（见 disjoint.rs L111-112 注释）。期望 stderr 见 [launch_contract_misleading_index_alias.stderr:L1-L7](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/tests/compile_fail/launch_contract_misleading_index_alias.stderr#L1-L7)。

第三个变体 `launch_contract_reordered_disjoint_alias.rs` 把元素类型放到第二个 type 参数位置（`type DisjointSlice<'a, IS, T> = cuda_device::DisjointSlice<'a, T, IS>;`），测宿主 marshalling 不会把 `IS` 误当元素类型——同样是 `E0277`。

#### 4.3.4 代码实践

**实践目标**：亲手写一段违反品牌化或维度契约的启动代码，确认它在编译期被拒绝，并定位是哪条契约拦截的。

操作步骤（任选其一，建议都做）：

1. **违反品牌**：仿照 `fail_wrong_brand.rs`，定义两个 kernel 结构体 `KernelX`/`KernelY` 各自 `impl KernelLaunchContract`（`type Config = LaunchConfig1D`），写一个 `fn launch_y(_: &PreparedLaunch<KernelY>)`，再写 `launch_y(&prepared_x)`。运行 `cargo test -p cuda-core --test launch_contract_types`，确认 `E0308`。
2. **违反秩**：仿照 `fail_wrong_rank.rs`，把一个 `domain=1` kernel 的 `prepare_*` 喂入 `LaunchConfig2D::new(...)`，确认 `E0308`。
3. **伪造 slice**：仿照 `launch_contract_fake_disjoint_slice.rs`，在 crate 内本地定义一个 `struct DisjointSlice<'a,T>{...}`，写一个 `#[launch_contract(domain=1, block=(64,1,1))]` 的 kernel 接收它，调用 `module.prepare_*`，确认 `E0277`，并阅读 stderr 中列出的「合法 impl 集」。

需要观察的现象：每次违规都对应一个明确的错误码（`E0308`/`E0277`），且 stderr 的「以下类型实现该 trait」帮助文本精准列出真 `DisjointSlice` 的所有变体。

预期结果：三段代码全部编译失败，错误分别由「品牌（关联类型/泛型不可换）」「秩（关联类型钉死）」「sealed trait（无 impl）」三条独立机制拦截。

> 待本地验证：步骤 1-2 走 `cargo test -p cuda-core`，无需 GPU；步骤 3 因用到 `#[cuda_module]` 宏，建议放进 `crates/cuda-macros/tests/compile_fail/` 并在 `launch_contract_semantics.rs` 注册后用 `cargo test -p cuda-macros` 跑。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `LaunchConfig1D` 的字段必须私有？公开字段会破坏哪条不变量？
**答案**：会破坏「尾随维度恒为 1」。若字段公开，调用方能 `LaunchConfig1D{ grid_x, block_y: 2, .. }` 之类伪造出 Y/Z 非零的「假 1D」配置，使 `index_1d()` 的唯一性前提（Y/Z 不活跃）失效，从而让「免 `unsafe` 启动」变得不安全。私有字段把唯一构造出口收拢到 `::new()`，再由 `fail_private_construction`/`fail_private_mutation` 两个负向测试盯死。

**练习 2**：`launch_contract_misleading_index_alias` 里把 `Index1D` 别名为 `Index2D`，为什么 `rustc` 没被骗过去？
**答案**：因为 `#[cuda_module]` 给 `prepare_*` 加的 bound 是 `__LaunchContractDisjointSlice<u32, 2>`，而该 sealed trait 的 impl 集只列「真 `DisjointSlice<_, Index1D>` impl `DOMAIN=1`」「真 `DisjointSlice<_, Index2D<_>>` impl `DOMAIN=1` 和 `2`」等。别名 `Index2D` 经 `rustc` 解析后仍是 `Index1D`，匹配的是 `DOMAIN=1` 那条 impl，与声明的 `DOMAIN=2` 不符 → `E0277`。sealed trait 强制 `rustc` 先解析别名再比对 impl 集，别名误导因此失效。

**练习 3**：`cuda_module_contract` 为什么除了跑内核，还要 `verify_launch_contract_ptx()` 去读 `.ptx` 文件？
**答案**：因为「启动契约正确」不只是「类型检查通过 + 运行结果对」，还包括「契约确实影响了最终生成的 PTX」（如动态共享内存被合并到 `align 128`、launch bounds 写成 `.maxntid`）。这些是 codegen 后段的产物，类型系统看不到；用字符串断言读 `.ptx` 才能把「契约→PTX」这条链路也纳入回归。

---

### 4.4 嵌套模块边界负向测试

#### 4.4.1 概念说明

`#324` 让 `#[cuda_module]` 能收集**嵌套 inline 模块**里的 kernel（见 [u1-l5](u1-l5-examples-tour.md) 的 `cuda_module_nested` 示例），每个命名空间层级有自己的 `LoadedModule` 视图，子视图经 `from_parent` 共享同一已加载模块。这带来一条硬约束：**整棵 inline 模块树的 kernel 名必须唯一**——因为 PTX `.entry` 符号目前是 kernel 的裸函数名，没有命名空间前缀，两个同名 kernel 会冲突。

「收集」本身也有边界：`#[cuda_module]` 只收集**当前 inline 模块树内**的 kernel，不会（也不应）跨越文件边界去捡 `mod file_kernel;`（外部文件模块）或 `include!` 进来的代码里的 kernel。这些是「可见性/作用域」的契约，一旦越界就会让用户误以为某个 kernel 被加载了、实际却没有（或反之），所以同样需要负向测试盯死。

#### 4.4.2 核心流程

```
#[cuda_module] mod kernels { mod first { #[kernel] fn step }  mod second { #[kernel] fn step } }
  └─ 宏扫描 inline 树，发现两个同名 `step`
  └─ 命中唯一性约束 → emit error: "...kernel names to be unique across its inline module tree..."

#[cuda_module] mod kernels { pub mod file_kernel; }   ← 外部文件模块
  └─ 宏不跨越文件边界收集
  └─ 用户尝试 kernels::file_kernel::LoadedModule → 该子视图不应被生成 → 编译失败
```

#### 4.4.3 源码精读

**唯一性约束**负向测试：

[cuda_module_duplicate_nested_kernel.rs:L7-L25](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/tests/compile_fail/cuda_module_duplicate_nested_kernel.rs#L7-L25) —— `first` 和 `second` 两个 inline 子模块各有一个 `#[kernel] pub fn step`。期望诊断见 [cuda_module_duplicate_nested_kernel.stderr:L1](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/tests/compile_fail/cuda_module_duplicate_nested_kernel.stderr#L1)：`` cuda-oxide PTX entry names are currently bare function names, so #[cuda_module] requires kernel names to be unique across its inline module tree: `step` in `second` conflicts with `step` in `first` ``。诊断**点出两个冲突位置**，便于用户改名。该测试在 [macro_guard.rs:L23](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/tests/macro_guard.rs#L23) 注册。

**文件边界**负向测试：

[cuda_module_file_kernel_boundary.rs:L8-L20](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/tests/compile_fail/cuda_module_file_kernel_boundary.rs#L8-L20) —— `kernels` 内声明 `pub mod file_kernel;`（外部文件模块，L15），用户尝试取 `kernels::file_kernel::LoadedModule`（L18）。因为宏不跨文件收集，该视图不会被生成，编译失败。同族还有 `cuda_module_include_kernel_boundary.rs`（`include!` 边界）与 `cuda_module_include_kernel_boundary_items.rs`，均在 [macro_guard.rs:L29-L30](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/tests/macro_guard.rs#L29-L30) 注册。

对照正面样本 `cuda_module_nested`，可见「合法的嵌套」长什么样：

[cuda_module_nested/src/main.rs:L23-L85](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/cuda_module_nested/src/main.rs#L23-L85) —— 三层 inline 嵌套（`init`/`scale`/`offset` 一层，`post::double` 两层），每个 kernel 名互不重复（`fill_index`/`scale_by`/`offset_by`/`double_all`），根模块甚至没有自己的 kernel，仅靠后代拥有 artifact——这是 `duplicate_nested_kernel` 负向测试的「合法对照」。

[cuda_module_nested/src/main.rs:L100-L109](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/cuda_module_nested/src/main.rs#L100-L109) —— `from_parent` 链：根 `module` 加载一次，各子命名空间经 `LoadedModule::from_parent(&module)` 共享，两层子模块用 `from_parent(&post)` 再下一级。

#### 4.4.4 代码实践

**实践目标**：体会「inline 树内 kernel 名必须唯一」与「不跨文件收集」两条边界。

操作步骤：

1. 复制 `cuda_module_nested` 示例，把 `scale::scale_by` 改名为 `offset_by`，使其与 `offset::offset_by` 重名，运行 `cargo oxide build cuda_module_nested`（待本地验证），预期看到与 `cuda_module_duplicate_nested_kernel.stderr` 同款的冲突诊断。
2. 阅读 `cuda_module_file_kernel_boundary.rs` 与其 `.stderr`，理解 `pub mod file_kernel;`（分号结尾=外部文件）与 `pub mod file_kernel { ... }`（花括号=inline）的区别——前者不被收集。
3. 还原步骤 1 的改名。

需要观察的现象：步骤 1 报冲突并点出两个 `offset_by` 的位置；步骤 2 看到诊断明确拒绝外部文件模块的 `LoadedModule` 视图。

预期结果：重名 → 编译失败；外部文件模块 → 不生成视图。

> 待本地验证：步骤 1 需 `cargo oxide` 工具链。

#### 4.4.5 小练习与答案

**练习 1**：为什么 PTX `.entry` 用裸函数名会导致「全树唯一」要求？未来若改成带命名空间前缀的符号，这条约束会怎样？
**答案**：因为当前 `.entry` 符号就是 kernel 的裸名（如 `step`），两个同名 kernel 会生成两条同名 `.entry`，链接/加载时冲突。若未来改成 `first_step`/`second_step` 这样带命名空间前缀的符号，全树唯一约束就可放松为「同层唯一」，`duplicate_nested_kernel` 这个负向测试也会相应改写或删除——这正是它作为「契约钉子」的价值：符号策略一变，测试会逼着文档与诊断同步更新。

**练习 2**：`pub mod file_kernel;`（分号）和 `pub mod file_kernel { #[kernel] fn k() {} }`（花括号）对 `#[cuda_module]` 有何不同？
**答案**：分号结尾是**外部文件模块**，kernel 定义在另一个文件里，`#[cuda_module]` 宏只看 inline 树的 token，不跨文件收集，故不生成 `file_kernel::LoadedModule` 视图（`cuda_module_file_kernel_boundary` 测的就是用户误用该视图）。花括号是 **inline 模块**，kernel 在树内，会被收集并生成视图。

---

### 4.5 安全模型三层总览与契约地图

#### 4.5.1 概念说明

前面四个模块讲了「契约如何被测」，本模块回到全局视角：cuda-oxide 的整套安全模型把内核按「编译器能验证多少」分成三层（tier），负向测试与 `unsafe` 边界都围绕这三层布置。理解这张地图，你才能判断「我新写的约束属于哪一层、该用哪条测试流水线」。

#### 4.5.2 核心流程

三层按「编译器验证能力」递减、用户责任递增排列：

| Tier | 描述 | 是否需 `unsafe` | 典型负向测试 |
|:-----|:-----|:----------------|:-------------|
| **Tier 1** | 安全 kernel 体 + 匹配的 `PreparedLaunch` | 否 | `fail_wrong_brand`/`fail_wrong_rank`/`launch_contract_fake_disjoint_slice`（保证「免 unsafe」真的安全） |
| **Tier 2** | 共享内存、warp shuffle、原子等，`unsafe` 作用域内、契约可审计 | 是，局部 | `launch_requires_unsafe`/`uncontracted_launch_requires_unsafe`（保证 raw 启动必须显式 unsafe） |
| **Tier 3** | TMA、tcgen05、WGMMA、cluster 等裸硬件 intrinsic | 是，遍布 | `error_wgmma_mma_unimplemented`（未实现的 Tier 3 必须显式拒绝） |

#### 4.5.3 源码精读

三层定义见安全模型文档：

[the-safety-model.md:L24-L36](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/cuda-oxide-book/gpu-safety/the-safety-model.md#L24-L36) —— 三层 tier 表。多数应用内核在 Tier 1 或 1-2 之间，Tier 3 留给「写下一个 CUTLASS」的性能工程师。

Tier 1 的核心抽象是 `ThreadIndex` + `DisjointSlice` 这对类型，安全由五条事实保证：

[the-safety-model.md:L42-L60](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/cuda-oxide-book/gpu-safety/the-safety-model.md#L42-L60) —— `ThreadIndex` 构造器私有、`!Send/!Sync/!Copy/!Clone`、`'kernel` 生命周期绑定；`DisjointSlice::get_mut` 只收匹配 `IndexSpace` 的见证、越界返回 `None`。

Tier 2 的「raw 启动必须 unsafe」由宏层负向测试盯死。`launch_requires_unsafe.rs` 验证裸 `cuda_launch!`（无 `unsafe` 块）不编译：

[launch_requires_unsafe.rs:L28-L35](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/tests/compile_fail/launch_requires_unsafe.rs#L28-L35) —— 宏展开调用 unsafe 的 `launch_kernel_on_stream`，不包 `unsafe` → `E0133`（期望 stderr [launch_requires_unsafe.stderr:L1-L3](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/tests/compile_fail/launch_requires_unsafe.stderr#L1-L3)）。

`uncontracted_launch_requires_unsafe.rs` 验证**未签约** kernel 的同名启动方法也是 unsafe：

[uncontracted_launch_requires_unsafe.rs:L20-L31](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/tests/compile_fail/uncontracted_launch_requires_unsafe.rs#L20-L31) —— `module.uncontracted(stream, raw LaunchConfig{...}, 7)` 不包 `unsafe` → `E0133`，期望 stderr 明确点名 `LoadedModule::uncontracted` 是 unsafe 函数（[.stderr:L1](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/tests/compile_fail/uncontracted_launch_requires_unsafe.stderr#L1)）。这把「未签约 kernel 吃 raw config 故 unsafe」这条 [u2-l4](u2-l4-launching-kernels.md) 结论钉成了编译期事实。

还有一条容易被忽略的契约——**签约模块的所有 loader 都是 unsafe**（一次性绑定证明），由 `launch_contract_untrusted_loaders.rs` 盯死：

[launch_contract_untrusted_loaders.rs:L20-L24](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/tests/compile_fail/launch_contract_untrusted_loaders.rs#L20-L24) —— 不包 `unsafe` 调 `kernels::load`/`from_module`/`load_named`，三个全部 `E0133`（期望 stderr 见 [launch_contract_untrusted_loaders.stderr:L1-L23](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/tests/compile_fail/launch_contract_untrusted_loaders.stderr#L1-L23)）。它守住的是「证明从每次启动前移到一次性绑定」这条边界——绑定错了，后续所有 `PreparedLaunch` 受检启动都跟着错。

最后，安全模型文档末尾的「状态总表」是判断「这条约束到底有没有被强制」的权威清单：

[the-safety-model.md:L555-L567](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/cuda-oxide-book/gpu-safety/the-safety-model.md#L555-L567) —— 比如「`&mut [T]` 作 kernel 参数」标注为 **NOT enforced**（已知缺口，需当作全程 unsafe），「warp 收敛」「内存空间感知」也是 NOT enforced（运行期义务/未来工作）。负向测试只覆盖 *Enforced* 的行；NOT enforced 的行是「诚实承认的边界」，不能用测试假装已经守住。

#### 4.5.4 代码实践

**实践目标**：把本讲的所有负向测试映射到三层模型，建立一张「契约→测试→tier」对照表。

操作步骤：

1. 列出本讲涉及的所有负向测试：`fail_wrong_brand`、`fail_wrong_rank`、`fail_private_construction`、`fail_private_mutation`、`launch_contract_fake_disjoint_slice`、`launch_contract_misleading_index_alias`、`launch_contract_reordered_disjoint_alias`、`launch_contract_untrusted_loaders`、`launch_contract_wrong_const_brand`、`cuda_module_duplicate_nested_kernel`、`cuda_module_file_kernel_boundary`、`launch_requires_unsafe`、`uncontracted_launch_requires_unsafe`、`error_missing_device_attr`、`error_wgmma_mma_unimplemented`。
2. 为每个测试判断：它守的是 Tier 1（免 unsafe 的安全证明）、Tier 2（raw/未签约必须 unsafe）、还是 Tier 3/设备 codegen（不支持的模式必须显式拒绝）。
3. 对照 [the-safety-model.md:L555-L567](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/cuda-oxide-book/gpu-safety/the-safety-model.md#L555-L567) 的状态总表，标注哪些「Enforced」行有对应负向测试、哪些没有。

需要观察的现象：大部分 Tier 1 安全证明都有对应负向测试；`&mut [T]`、warp 收敛等 NOT enforced 的缺口**没有**负向测试（因为它们本就没被强制）。

预期结果：得到一张清晰的「契约覆盖度」表，能看出哪些约束测试充分、哪些是已知未覆盖。

#### 4.5.5 小练习与答案

**练习 1**：`launch_contract_untrusted_loaders` 守的是 Tier 1 还是 Tier 2？
**答案**：它守的是 Tier 1 的**前提**。Tier 1 的「免 unsafe 受检启动」依赖 `PreparedLaunch` 见证，而见证又依赖「模块正确绑定」这一次性 unsafe 证明。若 loader 不强制 unsafe，用户可能绑错模块、拿到错误的 `PreparedLaunch`，Tier 1 的安全性就崩了。所以它守的是「Tier 1 安全证明的入口闸门」。

**练习 2**：为什么 `&mut [T]` 作 kernel 参数没有负向测试？
**答案**：因为安全模型总表（L562-574）明确把它列为 **NOT enforced** 的已知缺口——宏当前接受该类型，但运行期每个线程看到同一 backing 指针，写 `data[i]` 会数据竞争。既然没强制，就没有「必须编译失败」的契约可测；文档只能建议「把任何含 `&mut [T]` 参数的 kernel 当作全程 unsafe」。这是「诚实承认边界」而非「假装已解决」。

---

## 5. 综合实践

**任务：为「cluster launch 形状契约」补一个 compile_fail 用例。**

背景：[u5-l4](u5-l4-cluster-and-distributed-smem.md) 讲过 `#[cluster_launch(x,y,z)]` 是 launch contract 的一等公民，`prepare_*` 会在活设备上校验 cluster 形状并把 min CC 抬到 (9,0)，失败映射为带 kernel 名的 `Cluster*` 错误。但「类型层/编译期」对 cluster 的覆盖相对弱。

请完成：

1. **定位现有覆盖**：在 `crates/cuda-macros/tests/compile_fail/` 与 `crates/cuda-core/tests/launch_contract/` 中检索 `cluster`，确认 cluster 相关形状目前有没有 compile_fail 测试、由哪条契约（类型层还是运行期）兜底。
2. **设计一个负向用例**：参考本讲的写法，构造一段「看似合理但应被拒绝」的 cluster 启动代码。候选角度：
   - 在不支持 cluster 的 kernel（未加 `#[cluster_launch]`）上尝试传 cluster 配置；
   - 或让一个签约 cluster kernel 的 `prepare_*` 接收与 `Config` 关联类型秩不符的配置（仿 `fail_wrong_rank`）。
3. **判断归属**：你的用例该放 `cuda-macros/tests/compile_fail/`（宏/类型层，`trybuild`）还是做成 `error_*` 示例（设备 codegen，`smoketest.sh`）？给出理由。
4. **写期望 stderr**：如果走 `trybuild`，手写期望的 `E0xxx` 错误码与帮助文本；指出哪些部分你需要「待本地验证」才能钉死。
5. **在 runner 注册**：在 `launch_contract_semantics.rs` 或 `macro_guard.rs` 加一行 `t.compile_fail(...)`。

交付物：一个 `fail_*.rs` + `fail_*.stderr` + runner 注册行 + 一段说明「它守的是哪条 tier / 哪条契约、为什么不能省」。

> 提示：先确认你要拦的错误**确实发生在编译期**——若它只能由 `prepare_*` 在活设备运行期发现，那它就不是 compile_fail 而是运行期 `LaunchContractError`，需要的是单元测试而非 trybuild。这个判断本身就是本综合实践的核心训练。

## 6. 本讲小结

- cuda-oxide 用**两条负向流水线**固化安全契约：`trybuild compile_fail` 拦「标准 `rustc` 阶段」的错误（类型/隐私/sealed trait/宏结构），`error_*` 示例 + `smoketest.sh` 拦「设备 codegen 后端」才能诊断的错误；两者用 `verdict_error` 与 `.stderr` 比对分别判 PASS/FAIL。
- **设备 codegen 契约**（`error_missing_device_attr`、`error_wgmma_mma_unimplemented`）贯彻「编译期缺口即 bug」：缺 `#[device]` 的桩调用、未实现的 intrinsic lowering 都必须在 codegen 期显式拒绝，绝不静默 miscompile。
- `cuda_module_contract` 是启动契约的**正面样本**，一次性覆盖参数 ABI lowering、动态共享内存对齐合并、`#[launch_bounds]` 传播、泛型单态化，并用 `verify_launch_contract_ptx()` 把「契约→PTX」也纳入字符串断言。
- **启动契约类型层**由四条机制 + 一组负向测试共同焊死：秩保持（`fail_wrong_rank`）、品牌不可换（`fail_wrong_brand`）、私有字段（`fail_private_construction`/`fail_private_mutation`）、品牌化 sealed trait（`launch_contract_fake_disjoint_slice`/`misleading_index_alias`/`reordered_disjoint_alias`），以及签约模块 loader 必须 unsafe（`launch_contract_untrusted_loaders`）。
- **嵌套模块边界**（`#324`）有两条契约：inline 树内 kernel 名唯一（`cuda_module_duplicate_nested_kernel`，因为 PTX `.entry` 是裸函数名）、不跨文件/`include!` 边界收集（`cuda_module_file_kernel_boundary` 等）。
- 安全模型三层（Tier 1/2/3）是布置所有 `unsafe` 边界与负向测试的总地图；状态总表里 **NOT enforced** 的行（`&mut [T]`、warp 收敛、内存空间感知）是诚实承认的缺口，**没有**负向测试，不能用测试假装已经守住。

## 7. 下一步学习建议

- 想动手调试被这些契约拦下的内核，进入 [u7-l2 cuda-gdb 调试与设备端 debug intrinsics](u7-l2-cuda-gdb-and-debug-intrinsics.md)：用 `cargo oxide debug` 在 cuda-gdb 里看 Tier 2/3 内核的实际执行。
- 想从「正确性」走向「内存安全/数据竞争」的运行期兜底，进入 [u7-l3 NVIDIA Compute Sanitizer 正确性检查](u7-l3-compute-sanitizer.md)：`cargo oxide sanitize` 的 `racecheck`/`initcheck` 正好覆盖安全模型里 NOT enforced 的缺口。
- 想理解「契约→符号→制品」的工程闭环，进入 [u7-l4 符号命名契约、制品嵌入与差分模糊测试](u7-l4-symbol-contract-artifacts-fuzzing.md)：`reserved-oxide-symbols` 的命名契约是本讲多处「保留符号/品牌」的统一真相源，fuzzer 则是 codegen 正确性的差分回归网。
- 若要新增 intrinsic 或新安全约束，回到 [u6-l4 端到端新增一个 intrinsic](u6-l4-adding-new-intrinsic-template.md) 与本讲 [4.5](#45-安全模型三层总览与契约地图) 的对照表，按「设备层→dialect→importer→lowering→compile_fail」五工位落地，并为每条新约束补一个负向测试。
