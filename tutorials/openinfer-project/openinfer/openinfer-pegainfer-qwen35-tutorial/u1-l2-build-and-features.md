# 构建与运行：feature 门控与 Triton AOT 工具链

## 1. 本讲目标

上一讲（u1-l1）我们建立了 pegainfer-qwen35 的定位地图，知道整个 crate 被 `qwen35` feature 门控、构建期需要 Python + Triton。本讲把这两句话拆开验证：

1. 能沿着源码说出 `--features qwen35` 这一个开关如何同时影响**两层 crate**（模型 crate 与 `pegainfer-kernels`）的编译内容。
2. 能追踪 Triton AOT 构建链的完整流程：Python 探测 → SM 目标选择 → 生成 cubin 与 C 包装 → 链接进 Rust 静态库 → 最终被 `src/recurrent.rs` 调用。
3. 能配置 `PEGAINFER_TRITON_PYTHON`、`PEGAINFER_CUDA_SM`、`PEGAINFER_TEST_MODEL_PATH` 等关键环境变量。
4. 能解释 `[[test]]` / `[[bench]]` 的 `required-features` 声明，并知道单元测试与集成测试各自的运行命令和跳过机制。

## 2. 前置知识

- **cargo feature**：Cargo 的可选编译开关。`[features]` 表里可以声明「本 feature 依赖其他 crate 的某个 feature」（feature 转发），也可以配合 `#[cfg(feature = "...")]` 在源码级裁剪内容。一个 feature 关闭时，被它门控的代码**根本不参与编译**，而不是编译成空实现。
- **build.rs（构建脚本）**：Cargo 在编译 crate 本体之前先编译并运行的程序，通过 `println!("cargo:...")` 向 Cargo 回传指令（链接库、重编译条件等）。`pegainfer-kernels` 的 build.rs 是整个工程的 CUDA 编译入口。
- **AOT 与 JIT**：JIT（即时编译）在程序运行时才编译内核；AOT（ahead-of-time，预先编译）在构建期就产出最终产物。Qwen3.5 的 Gated DeltaRule 分块预填充内核用 Triton DSL（Python）编写，但只在**构建期**用 Python，运行时加载的是编译好的 cubin，服务器进程本身零 Python。
- **SM 架构代号**：NVIDIA GPU 的 compute capability 简写，如 `sm_89`（RTX 40 系）、`sm_120`（RTX 50 系）。cubin 与 SM 强绑定，为错误的 SM 编译的 cubin 在目标 GPU 上加载会失败。
- **required-features**：`[[test]]` / `[[bench]]` 目标的属性——feature 未开启时 Cargo 直接**跳过**该目标，不编译也不运行。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `pegainfer-qwen35/Cargo.toml` | 声明 `qwen35` feature 并转发到 kernels；六个集成测试与一个基准的 `required-features` |
| `pegainfer-qwen35/src/lib.rs` | crate 级 `#![cfg(feature = "qwen35")]`，feature 关闭时整个 crate 编译为空 |
| `pegainfer-kernels/Cargo.toml` | kernels 侧的 `qwen35` feature（build.rs 读取的唯一开关） |
| `pegainfer-kernels/build.rs` | CUDA/Triton 编译总指挥：SM 探测、nvcc 编译、Triton AOT 生成与链接 |
| `pegainfer-kernels/tools/triton/gen_triton_aot.py` | Triton AOT 生成器（build.rs 以子进程调用） |
| `pegainfer-kernels/tools/triton/gated_delta_rule_chunkwise_kernels.py` | 用 Triton DSL 写的 GDR 分块预填充内核源（7 个内核） |
| `pegainfer-kernels/tools/triton/README.md` | Triton 工具链的官方说明：环境准备、常见失败 |
| `pegainfer-kernels/src/ffi.rs` + `src/ffi/qwen35.rs` | 被 feature 门控的 `extern "C"` 符号声明（Rust 侧内核入口） |
| `pegainfer-core/src/ffi.rs` → `pegainfer-qwen35/src/ffi.rs` | FFI 声明的两级重导出链 |
| `pegainfer-qwen35/src/recurrent.rs` | 真正调用 Triton 生成的 CUDA 符号的地方 |
| `pegainfer-qwen35/tests/common/model_fixture.rs` | 集成测试的模型路径夹具：读 `PEGAINFER_TEST_MODEL_PATH`，无效则 SKIP |
| `pegainfer-qwen35/benches/qwen35_ops.rs` | criterion 基准入口（norm 与 state 两组） |
| `docs/models/qwen35/model-crate.md` | crate 官方文档的「Build and validation」一节 |

## 4. 核心概念与源码讲解

### 4.1 cargo feature：一个开关，两层 crate

#### 4.1.1 概念说明

「开启 Qwen3.5 支持」看似一个 flag，实际是**两层联动**：

- **第一层（模型 crate）**：`pegainfer-qwen35` 的 `qwen35` feature 做两件事——把 feature 转发给 `pegainfer-kernels`，同时让本 crate 的全部源码参与编译。
- **第二层（kernels crate）**：`pegainfer-kernels` 的 `qwen35` feature 是一个空 feature（不拉依赖），它的唯一作用是让 build.rs 通过 `cfg!(feature = "qwen35")` 感知到开关，从而执行 Triton AOT 生成；同时让 `src/ffi/qwen35.rs` 里的符号声明参与编译。

这种设计让「模型代码」与「内核产物」永远同进同退：不可能出现模型代码在、内核符号缺的情况。

#### 4.1.2 核心流程

```text
cargo ... --features qwen35
        │
        ├─▶ pegainfer-qwen35: feature "qwen35" 开启
        │       ├─▶ 转发: pegainfer-kernels/qwen35 = ["pegainfer-kernels/qwen35"]
        │       └─▶ src/lib.rs 的 #![cfg(feature = "qwen35")] 为真 → 编译完整 crate
        │
        └─▶ pegainfer-kernels: feature "qwen35" 开启
                ├─▶ build.rs: cfg!(feature = "qwen35") == true
                │       ├─▶ nvcc 编译 csrc/qwen35/*.cu（native CUDA）
                │       └─▶ compile_triton_aot_kernels()（需要 Python + Triton）
                └─▶ src/ffi.rs: #[cfg(feature = "qwen35")] mod qwen35 → extern 符号可用
```

feature **关闭**时：模型 crate 编译为空（lib.rs 的 crate 级 cfg 把所有 item 裁掉），kernels 跳过 `csrc/qwen35/` 与全部 Triton 步骤，只打印一条 warning。

#### 4.1.3 源码精读

**① feature 的声明与转发** —— [pegainfer-qwen35/Cargo.toml:L35-L37](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/Cargo.toml#L35-L37)：`qwen35 = ["pegainfer-kernels/qwen35"]` 就是转发语法的全部——开启本 crate 的 `qwen35`，等于同时开启依赖 crate 的同名 feature。注意 `default = []`：默认一个 feature 都不开，所以 `cargo build --workspace` 不会拉起 Qwen3.5。

**② kernels 侧的空 feature** —— [pegainfer-kernels/Cargo.toml:L31-L44](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/Cargo.toml#L31-L44)：第 40 行 `qwen35 = []`，注释（34-35 行）写明「这是唯一需要构建期 Python + Triton 的 feature」。空 feature 的意义在于：它不是给 Rust 代码用的依赖开关，而是给 build.rs 和 `#[cfg]` 用的编译期信号。

**③ crate 级裁剪** —— [pegainfer-qwen35/src/lib.rs:L1-L7](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/lib.rs#L1-L7)：文件头部的注释直接说明了动机——「Without the feature this compiles to an empty crate so plain workspace builds stay Python-free」，第 7 行 `#![cfg(feature = "qwen35")]` 是 crate 级 cfg，为假时整个文件的所有模块声明都被移除。

**④ kernels 符号声明的门控** —— [pegainfer-kernels/src/ffi.rs:L24-L25](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/ffi.rs#L24-L25) 声明 `#[cfg(feature = "qwen35")] mod qwen35;`，[第 48-49 行](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/ffi.rs#L48-L49) 再 `pub use qwen35::*;`。feature 关闭时这些 `extern "C"` 声明不存在，链接器也不会去找对应的 C 符号——这就是「模型代码与内核产物同进同退」在 Rust 侧的落点。

一个容易误解的点：**feature 关闭 ≠ 不需要 CUDA**。kernels 的 build.rs 在任何 feature 组合下都要用 nvcc 编译 `csrc/*.cu`（通用算子），`qwen35` 增加的要求只是**额外的 Python + Triton**。

#### 4.1.4 代码实践

1. **实践目标**：亲眼确认「feature 关闭时 crate 为空」这一行为。
2. **操作步骤**：
   - 执行 `cargo check -p pegainfer-qwen35`（不带 feature）。注意：即使 feature 关闭，依赖 `pegainfer-kernels` 的 build.rs 仍会运行并需要 CUDA toolkit（nvcc），这是正常现象。
   - 观察编译输出中来自 `pegainfer-kernels` 的 warning：`Qwen3.5 Triton AOT kernels disabled; enable the pegainfer-kernels 'qwen35' feature to build them (needs Python + Triton at build time)`。
   - 再执行 `cargo test -p pegainfer-qwen35`（不带 feature），观察 Cargo 提示所有 test target 因 `required-features` 不满足而被过滤。
3. **需要观察的现象**：两次命令都应**成功**；不带 feature 时没有任何 Qwen3.5 相关的编译产物被生成，也没有任何 Python 被调用。
4. **预期结果**：`cargo test` 输出形如 `Running 0 tests` / 目标被跳过；不存在 `hf_golden_gate` 等测试被编译的迹象。
5. 本环境（本讲义写作环境）未执行上述命令，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：如果只给 `pegainfer-kernels` 开 feature（`cargo build -p pegainfer-kernels --features qwen35`），`pegainfer-qwen35` 的代码会被编译吗？
**答案**：不会。feature 转发是单向的：`pegainfer-qwen35/qwen35` → `pegainfer-kernels/qwen35`。反向不成立——kernels 会生成并链接 Triton 内核，但模型 crate 仍是空 crate（其 `#![cfg]` 依赖自己的 feature）。

**练习 2**：为什么把 feature 转发写在模型 crate、而不是让 kernels 默认开启 `qwen35`？
**答案**：kernels 是全仓库共享的编译单元，默认开启会让**所有**模型线的构建都背上 Python + Triton 依赖，违背「默认构建 Python-free」的约束；由模型 crate 按需转发，污染范围被限制在真正构建 Qwen3.5 的那次命令里。

**练习 3**：`#![cfg(feature = "qwen35")]` 放在 lib.rs 顶部和给每个 `mod` 单独加 `#[cfg]` 相比，有什么好处？
**答案**：crate 级 cfg 一条语句裁掉全部内容（包括嵌套模块、`use`、常量），不可能漏掉某个文件；逐模块标注则容易在新增文件时忘记，造成「feature 关闭但某个模块仍引用 kernels 符号」的链接错误。

### 4.2 Triton AOT 构建链：从 Python 脚本到静态库

#### 4.2.1 概念说明

Qwen3.5 的线性注意力层在**预填充**阶段使用 Gated DeltaRule（GDR）的 chunkwise 算法，这组内核用 Triton DSL（Python）编写。Triton 的常规用法是运行时 JIT，但那会让服务器进程依赖 Python。PegaInfer 选择 AOT：构建期由 build.rs 调用 Python 把每个内核编译成cubin + 一段 C 加载代码，再由宿主编译器（cc）编成静态库 `triton_kernels_aot` 链进 Rust。这样运行时只需要 `libcuda`，与 Python 彻底解耦。

注意分工：**Triton AOT 只覆盖 GDR chunkwise prefill 的 7 个内核**；线性注意力的 conv1d、递推 decode 以及 head_dim=256 的全注意力内核是手写 CUDA（`csrc/qwen35/*.cu`），走常规 nvcc 路径。两者都被同一个 feature 门控。

#### 4.2.2 核心流程

build.rs 的 `main()` 每次构建都会执行，`qwen35` 开启时的增量流程：

1. **门控判断**：`qwen35_enabled = cfg!(feature = "qwen35")`；为真才调用 `compile_triton_aot_kernels`，为假打印禁用 warning。
2. **Python 探测**（`find_triton_python`）：依次尝试 `PEGAINFER_TRITON_PYTHON` 环境变量 → 仓库根 `.venv/bin/python` → `python3` → `python`，每个候选都跑一次 `python -c "import triton"` 探测，全部失败则 panic 并给出诊断。
3. **SM 目标选择**（`triton_target`）：从探测到的 SM 列表取最大值，拼成 Triton 目标串 `cuda:{sm}:32`；多个 SM 时打印 warning 并只用最高目标（`PEGAINFER_CUDA_SM` 可手动钉死）。
4. **逐内核生成**：对 7 个 `TritonKernelSpec` 各跑一次 `gen_triton_aot.py`，参数包括内核路径、内核名、类型签名（如 `*bf16,*fp32,...`）、grid 表达式、`--num-warps` / `--num-stages`；生成器回传 `FUNC_NAME` 与 `C_PATH`。
5. **修补生成的 C 源**（`patch_triton_aot_per_device_handles`）：Triton 生成的加载器只支持单设备，build.rs 把 `CUmodule` / `CUfunction` 改造成按设备索引的表（上限 16），使 TP 多卡各自加载。
6. **写 C 包装**（`write_wrapper`）：把生成的裸启动函数包一层稳定的 `*_cuda` 符号（如 `gated_delta_rule_prefill_chunk_prepare_cuda`），参数类型换成 `const uint16_t*` 等 C 类型并接收 `CUstream`。
7. **编译链接**：`cc::Build` 把全部生成的 `.c` 编为静态库 `triton_kernels_aot`，并 `cargo:rustc-link-lib=cuda`。
8. **重编译追踪**：`cargo:rerun-if-changed` 盯住两个 Python 文件；`rerun-if-env-changed=PEGAINFER_TRITON_PYTHON` 保证换解释器时重建。

7 个 Triton 内核规格（均在 [build.rs:L1339-L1500](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/build.rs#L1339-L1500) 一带定义）：

| Triton 内核（Python 函数名） | 产物符号后缀 | 作用 |
|---|---|---|
| `gdr_prepare_qkv_gbeta_qwen35_kernel` | `..._chunk_prepare_cuda` | 投影出 q/k/v/g/beta |
| `gdr_chunk_local_cumsum_qwen35_kernel` | `..._chunk_cumsum_cuda` | 门控 g 的块内前缀和 |
| `gdr_chunk_scaled_dot_kkt_qwen35_kernel` | `..._chunk_a_cuda` | 块内 K·Kᵀ |
| `gdr_solve_tril_64_qwen35_kernel` | `..._chunk_solve_cuda` | 解下三角系统 |
| `gdr_recompute_w_u_qwen35_kernel` | `..._chunk_recompute_cuda` | 重算 w/u |
| `gdr_chunk_state_qwen35_kernel` | `..._chunk_state_cuda` | 递推状态跨 chunk 推进 |
| `gdr_chunk_o_qwen35_kernel` | `..._chunk_o_cuda` | 块输出累积 |

#### 4.2.3 源码精读

**① main() 的总开关** —— [pegainfer-kernels/build.rs:L2874-L2880](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/build.rs#L2874-L2880)：`if qwen35_enabled { compile_triton_aot_kernels(...) } else { 打印禁用 warning }`。变量在 [第 2225 行](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/build.rs#L2225) 用 `cfg!(feature = "qwen35")` 取值——build.rs 里读 feature 只能用 `cfg!` 宏（编译期常量），不能用 `#[cfg]` 属性。

**② nvcc 侧的门控** —— [pegainfer-kernels/build.rs:L2293-L2295](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/build.rs#L2293-L2295)：遍历 `csrc/*.cu` 时，feature 关闭则丢弃路径含 `qwen35` 目录的文件。判定函数 [is_qwen35_source（L760-L769）](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/build.rs#L760-L769) 的注释点明了这一目录的语言：`csrc/qwen35/` 下是「线性注意力递推 + head_dim 256 全注意力」的手写 CUDA（conv1d.cu、gated_delta_rule.cu、三个 attention 内核）。

**③ Python 探测链** —— [pegainfer-kernels/build.rs:L1017-L1051](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/build.rs#L1017-L1051)：`find_triton_python` 先看 `PEGAINFER_TRITON_PYTHON`（设了但为空是硬错误），否则探测 `.venv/bin/python` → `python3` → `python`；单个候选的判定是 [probe_triton_python（L1001-L1015）](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/build.rs#L1001-L1015) 里的 `import triton`。失败信息会拼接每个候选的探测结果，这是排障时最先看的输出。

**④ SM 目标** —— [pegainfer-kernels/build.rs:L1149-L1163](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/build.rs#L1149-L1163)：取最高 SM 拼 `cuda:{max_sm}:32`。SM 列表来自 [detect_sm_targets（L435-L447）](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/build.rs#L435-L447)，它优先读 `PEGAINFER_CUDA_SM`（或 `CUDA_SM`）——这就是构建机上看不到 `nvidia-smi` 时的逃生通道。

**⑤ 一次生成调用** —— [pegainfer-kernels/build.rs:L1339-L1351](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/build.rs#L1339-L1351)：`gdr_prepare_spec` 是 7 个规格的代表——`signature: "*bf16,*bf16,...,128,128"` 用 Triton 的类型记号描述参数（指针后跟元素类型，尾部常量是 constexpr），`grid: "seq_len,num_value_heads,1"` 是启动维度表达式。具体执行在 [generate_triton_artifacts（L1187-L1215）](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/build.rs#L1187-L1215)：以子进程运行 `gen_triton_aot.py`，透传全部规格参数；失败时 panic 并打印生成器的 stdout/stderr。

**⑥ 编译与链接** —— [pegainfer-kernels/build.rs:L1503-L1514](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/build.rs#L1503-L1514)：`cc::Build`（`-std=c11`，带上 `cuda.h` 的 include 路径）把生成的 C 源编成静态库 `triton_kernels_aot`；[L1516-L1529](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/build.rs#L1516-L1529) 随后 `cargo:rustc-link-lib=cuda` 并登记 rerun 条件。

**⑦ Rust 侧的符号声明与重导出链** —— [pegainfer-kernels/src/ffi/qwen35.rs:L196-L214](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/ffi/qwen35.rs#L196-L214) 声明了与 C 包装一一对应的 `unsafe extern "C" fn gated_delta_rule_prefill_chunk_prepare_cuda(...)`，注释写明「Triton AOT-generated at build time」；[L255-L274](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/ffi/qwen35.rs#L255-L274) 的 `chunk_state` 声明还附带期望的张量形状注释（如 state 为 `[H, 128, 128]` fp32 的 `[H,K,V]` 布局）。声明经两级重导出到达模型 crate：[pegainfer-core/src/ffi.rs:L1](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-core/src/ffi.rs#L1) 的 `pub use pegainfer_kernels::ffi::*;`，再到 [pegainfer-qwen35/src/ffi.rs:L1](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/ffi.rs#L1) 的 `pub(crate) use pegainfer_core::ffi::*;`。

**⑧ 调用点** —— [pegainfer-qwen35/src/recurrent.rs:L105-L134](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent.rs#L105-L134)：`gated_delta_rule_prefill_chunk_prepare_into` 先用 `device_ptr` / `device_ptr_mut` 取出每个张量的设备指针（返回的 guard 绑定成 `_gqkv` 等变量，存活到 `unsafe` 块结束——这保证指针在调用期间不被释放），然后在 `unsafe` 块里把裸指针和 `ctx.stream` 传给第 ⑦ 步声明的 C 符号，最后 `result.result()?` 把 `CUresult` 转成 Rust 错误。构建期生成的 cubin 就在这里被启动。

**⑨ 官方文档佐证** —— [docs/models/qwen35/model-crate.md:L54-L59](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/docs/models/qwen35/model-crate.md#L54-L59)：明确「`qwen35` feature 同时启用 crate 与其 Triton AOT GDR prefill 内核；`PEGAINFER_TRITON_PYTHON` 选择构建期解释器；默认 Qwen3 构建不启用此内核路径」。工具链操作细节（uv venv 引导、Windows 的 triton-windows、常见失败）在 [pegainfer-kernels/tools/triton/README.md:L29-L50](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/tools/triton/README.md#L29-L50) 与 [L97-L106](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/tools/triton/README.md#L97-L106)。

#### 4.2.4 代码实践

1. **实践目标**：观察一次真实构建里 Triton AOT 各阶段的发生顺序与耗时。
2. **操作步骤**：
   - 准备解释器：`uv venv .venv && uv pip install -p .venv/bin/python triton`，然后 `export PEGAINFER_TRITON_PYTHON=$PWD/.venv/bin/python`（在仓库根执行）。
   - 若构建机没有 `nvidia-smi`，再 `export PEGAINFER_CUDA_SM=120`（换成目标卡的实际值）。
   - `export PEGAINFER_BUILD_TIMING=1`（build.rs 在 [L261-L264](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/build.rs#L261-L264) 识别它，`time_phase` 会给每个阶段计时）。
   - `cargo build --release --features qwen35`。
   - 构建后查看 `target/release/build/pegainfer-kernels-*/out/triton_aot/` 目录。
3. **需要观察的现象**：构建日志按序出现 `triton-gen gdr_prepare_qkv_gbeta_qwen35_kernel` 等 7 条计时行，之后是 `cc triton_kernels_aot`；`out/triton_aot/` 下出现 7 个子目录，各含生成的 `.c` 源与 cubin 产物。
4. **预期结果**：链接产物包含静态库 `triton_kernels_aot`；再次构建（不改动 Python 文件）时 Triton 阶段全部跳过——这是 `rerun-if-changed` 的效果。
5. 本讲义写作环境无 GPU 与 Triton，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `find_triton_python` 用 `python -c "import triton"` 探测，而不是检查 `which python3`？
**答案**：解释器存在不代表 Triton 已安装。探测的本质是「能否完成 AOT 编译」，`import triton` 直接验证了这一点，还顺带在失败时收集每个候选的具体报错，拼进 panic 信息方便排障。

**练习 2**：构建成功但运行时报 `CUDA_ERROR_NO_BINARY_FOR_GPU`，最可能的原因是什么？
**答案**：cubin 与 SM 强绑定。构建期的 SM 探测（或 `PEGAINFER_CUDA_SM`）与实际运行环境的 GPU 架构不一致——比如为 `sm_120` 生成却在 `sm_89` 的卡上运行。README 的「Common failures」把这条列为典型故障，解法是在目标 GPU 环境重建。

**练习 3**：`patch_triton_aot_per_device_handles` 解决什么问题？为什么对 Qwen3.5 尤其重要？
**答案**：Triton 生成的 C 加载器用单个全局 `CUmodule` / `CUfunction`，隐含单设备假设；build.rs 把它们改成按 device ordinal 索引的表（上限 16）。Qwen3.5 支持张量并行（多卡多进程），每个 rank 在自己的 GPU 上都要能独立加载同一份 cubin，所以这个修补是 TP 能跑起来的前提。

### 4.3 测试入口：required-features 与模型路径跳过

#### 4.3.1 概念说明

这个 crate 的测试分三类，入口行为各不相同：

- **单元测试**（`src` 内 `#[cfg(test)]` 模块）：随 lib 编译，feature 关闭时 crate 为空，所以**必须**带 `--features qwen35`。
- **集成测试**（`tests/*.rs` 六个文件）：每个都在 Cargo.toml 里声明了 `required-features = ["qwen35"]`——feature 不满足时 Cargo 连编译都跳过，不会报错也不会运行。
- **基准**（`benches/qwen35_ops.rs`）：同样带 `required-features`，且 `harness = false`（用 criterion 自己的 main）。

集成测试全部依赖真实模型权重与 GPU。为了在无权重环境里不假失败，公共夹具 `model_fixture.rs` 实现了「校验 + 跳过」：读 `PEGAINFER_TEST_MODEL_PATH`，逐项检查路径下 `config.json` 是否存在、是否合法 JSON、`model_type` 是否为 `qwen3_5`（或嵌套 `text_config.model_type == "qwen3_5_text"`），任何一步不满足就打印 `SKIP <test>: <原因>` 并让测试直接通过返回。

#### 4.3.2 核心流程

```text
cargo test --release -p pegainfer-qwen35 --features qwen35
    │
    ├─ Cargo 读取 [[test]] 目标 → required-features 满足 → 逐个编译运行
    │
    └─ 每个集成测试开头：
        model_path_or_skip("<test>")
            ├─ 读 PEGAINFER_TEST_MODEL_PATH
            ├─ 未设置           → SKIP（"not set; point it at a public Qwen3.5 model fixture"）
            ├─ 路径为空          → SKIP
            ├─ config.json 读不出 → SKIP
            ├─ JSON 解析失败      → SKIP
            ├─ model_type ≠ qwen3_5 / qwen3_5_text → SKIP（"not a Qwen3.5 config"）
            └─ 通过 → Some(path)，测试继续（随后才真正需要 GPU）
```

#### 4.3.3 源码精读

**① 六个集成测试 + 一个基准的声明** —— [pegainfer-qwen35/Cargo.toml:L42-L69](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/Cargo.toml#L42-L69)：`hf_golden_gate`（精度金标门）、`e2e_scheduler`（调度活性）、`sampling_behavior`、`chunked_prefill`、`prefix_cache`、`serving_tp2`（TP2 服务，需两卡且串行）全部 `required-features = ["qwen35"]`；[第 66-69 行](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/Cargo.toml#L66-L69) 的 `qwen35_ops` bench 加了 `harness = false`。另注意 [第 2 行](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/Cargo.toml#L2) `autobenches = false`：关闭 Cargo 的 bench 自动发现，bench 只剩显式声明这一个——`benches/ops/` 下的子模块由 [benches/qwen35_ops.rs:L1-L11](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/benches/qwen35_ops.rs#L1-L11) 的 criterion 入口 `mod ops;` 手工挂载（norm 与 state 两组）。

**② 夹具的环境变量入口** —— [pegainfer-qwen35/tests/common/model_fixture.rs:L3-L9](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/tests/common/model_fixture.rs#L3-L9)：`MODEL_PATH_ENV` 就是 `PEGAINFER_TEST_MODEL_PATH`；`model_path_or_skip` 返回 `Option<String>`，`None` 即跳过。

**③ 校验链** —— [pegainfer-qwen35/tests/common/model_fixture.rs:L41-L83](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/tests/common/model_fixture.rs#L41-L83)：先查空串，再读 `<path>/config.json`，再解析 JSON，最后同时接受根级 `model_type == "qwen3_5"` 与嵌套 `text_config/model_type == "qwen3_5_text"` 两种 checkpoint 布局（第 68-72 行）。[L85-L88](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/tests/common/model_fixture.rs#L85-L88) 的 `skip` 用 `eprintln!("SKIP ...")` 把原因打到 stderr 后返回 `None`——测试以通过收场，但日志里留下了为什么。

**④ 常用命令**（CLAUDE.md「Tests」一节收录，配合 `--release` 是硬要求，debug 构建对 GPU 代码慢到会超时）：

```bash
# 单元测试（全仓库）
cargo test --release --workspace --lib

# 本 crate 单元测试
cargo test --release -p pegainfer-qwen35 --features qwen35 --lib

# 集成测试：调度活性（需要 GPU + 权重）
PEGAINFER_TEST_MODEL_PATH=models/Qwen3.5-4B cargo test --release \
  -p pegainfer-qwen35 --features qwen35 --test e2e_scheduler

# 精度金标门（需要 GPU + 权重）
PEGAINFER_TEST_MODEL_PATH=models/Qwen3.5-4B cargo test --release \
  -p pegainfer-qwen35 --features qwen35 --test hf_golden_gate

# 基准
cargo bench --release -p pegainfer-qwen35 --features qwen35
```

[docs/models/qwen35/model-crate.md:L60-L67](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/docs/models/qwen35/model-crate.md#L60-L67) 补充了两条纪律：`PEGAINFER_TEST_MODEL_PATH` 建议**绝对路径**（避免 package 工作目录歧义）；TP2 门需要两块卡且必须串行运行。

#### 4.3.4 代码实践

1. **实践目标**：亲眼看到「跳过」机制如何工作，区分「SKIP 通过」与「真实运行」。
2. **操作步骤**：
   - 不设任何环境变量，执行 `cargo test --release -p pegainfer-qwen35 --features qwen35 --test e2e_scheduler -- --nocapture`。
   - 盯住 stderr 里的 `SKIP` 行，抄下它给出的原因字符串。
   - 再故意把 `PEGAINFER_TEST_MODEL_PATH` 指向一个存在但非 Qwen3.5 的模型目录（例如 Qwen3-4B），重跑一次，观察 SKIP 原因的变化（应命中「is not a Qwen3.5 config」分支）。
3. **需要观察的现象**：两次测试都显示 passed（0 个断言执行），但 SKIP 原因不同；`--nocapture` 让 stderr 可见。
4. **预期结果**：第一次原因是 `PEGAINFER_TEST_MODEL_PATH is not set; ...`；第二次是 `... is not a Qwen3.5 config`——分别对应 [model_fixture.rs:L31-L34](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/tests/common/model_fixture.rs#L31-L34) 与 [L72-L80](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/tests/common/model_fixture.rs#L72-L80)。
5. 命令需要 CUDA toolkit 才能编译，本讲义写作环境未执行，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么集成测试选择「SKIP 后 pass」而不是 `#[ignore]` 标注？
**答案**：`#[ignore]` 是静态的，永远不跑；SKIP 是动态的——同一份代码在无权重机器上安静通过、在有权重机器上自动全量运行，CI 与本地都不需要维护两套命令或过滤列表。

**练习 2**：`autobenches = false`（Cargo.toml 第 2 行）删掉会发生什么？
**答案**：Cargo 会自动发现 `benches/` 下的 `.rs` 文件并生成额外 bench 目标，但这些目标没有 `required-features` 声明，会在不带 feature 的构建里因引用被裁剪的符号而编译失败（或产生空目标），破坏「默认构建可用」的保证。

**练习 3**：夹具为什么同时接受 `model_type == "qwen3_5"` 和 `text_config.model_type == "qwen3_5_text"` 两种布局？
**答案**：HF checkpoint 的 config.json 有两种形态：单模型把 `model_type` 放在根级；多模态/嵌套布局把文本配置放在 `text_config` 子对象里。两种都可能是合法的 Qwen3.5 权重目录，夹具按实际磁盘格式逐一探测。

## 5. 综合实践

把本讲三个模块串成一次对照实验：**同一个 crate，feature 开与关，构建行为差在哪**。

1. **实践目标**：用真实构建证据回答「为什么默认 workspace 构建必须保持 Python-free」，并把 feature → build.rs → 链接产物的链路走一遍。
2. **操作步骤**：
   - **基线（无 feature）**：`cargo check -p pegainfer-qwen35 2>&1 | tee /tmp/qwen35-off.log`。在日志里找到「Qwen3.5 Triton AOT kernels disabled」warning（出自 build.rs 的 else 分支）；确认没有任何 Python 进程被调用。
   - **对照（有 feature）**：先按 4.2.4 准备 Triton 解释器与 `PEGAINFER_CUDA_SM`，然后 `cargo check -p pegainfer-qwen35 --features qwen35 2>&1 | tee /tmp/qwen35-on.log`。
   - **对比产物**：在 `target/` 下各找 `pegainfer-kernels-*/out/`：基线不应存在 `triton_aot/` 目录，对照应出现含 7 个子目录的 `triton_aot/`；再确认对照多出静态库 `triton_kernels_aot`。两份日志按出现顺序 diff，标出只存在于对照的行（triton-gen 计时、cc triton_kernels_aot、cuda 链接指令）。
   - **写出结论**：用 5-8 句话说明默认构建为何必须 Python-free。论证要点（供核对）：qwen3 是默认 feature，日常/CI 的 workspace 构建不涉及 Qwen3.5；Triton 只在构建期被调用，AOT 把 Python 依赖隔离在 `--features qwen35` 的那次构建里；`#![cfg]` + `is_qwen35_source` + `#[cfg] mod qwen35` 三处门控保证关掉时不留任何残缺符号。
3. **需要观察的现象**：两次 check 都成功；差异集中在 kernels 的 build 脚本输出与 OUT_DIR 内容；模型 crate 本体在基线下近乎空编译。
4. **预期结果**：一张两列对照表（无 feature / 有 feature × 构建输入、build.rs 分支、OUT_DIR 产物、链接库），加一段 Python-free 论证。
5. 本环境无 CUDA toolkit 与 Triton，以上命令均**待本地验证**；若 `cargo check` 因缺 nvcc 失败，属环境问题而非 feature 行为差异，请先解决 CUDA toolkit 再对照。

## 6. 本讲小结

- `qwen35` 是一个跨 crate 的开关：模型 crate 的 feature 转发到 `pegainfer-kernels/qwen35`，后者同时驱动 build.rs 的 Triton AOT 与 nvcc 的 `csrc/qwen35/` 编译，并让 `extern "C"` 符号声明参与编译——模型代码与内核产物永远同进同退。
- feature 关闭时 `#![cfg(feature = "qwen35")]` 让整个模型 crate 编译为空，这是「默认 workspace 构建 Python-free」的源码级保证；但 CUDA toolkit 在任何 feature 组合下都是必需的。
- Triton AOT 链：`find_triton_python`（env → `.venv` → python3 → python，以 `import triton` 探测）→ 按 7 个 `TritonKernelSpec` 运行 `gen_triton_aot.py` 生成 cubin + C → 修补为多设备安全 → cc 编成 `triton_kernels_aot` 静态库 → 经 `kernels::ffi → core::ffi → qwen35::ffi` 重导出，最终在 `src/recurrent.rs` 的 unsafe 块里被启动。
- `PEGAINFER_TRITON_PYTHON`、`PEGAINFER_CUDA_SM`、`PEGAINFER_BUILD_TIMING` 控制构建期行为；`PEGAINFER_TEST_MODEL_PATH` 控制测试期权重发现。
- 六个集成测试与一个基准都声明 `required-features = ["qwen35"]`；无权重时 `model_fixture` 的校验链以 `SKIP <test>: <reason>` 安静通过，日志保留原因。
- 所有 GPU 相关命令必须 `--release`；cubin 与 SM 强绑定，跨架构运行会出现 `CUDA_ERROR_NO_BINARY_FOR_GPU`。

## 7. 下一步学习建议

- 下一讲 **u1-l3（目录结构与模块地图）**：本讲只碰了 `lib.rs` 的门控行，下一讲会把 `src/` 下全部模块按 config / weights / 执行 / 调度 / 缓存 / TP 归类，建立后续所有讲义的导航图。
- 想深挖 Triton 内核本体的读者，可以提前读 `pegainfer-kernels/tools/triton/gated_delta_rule_chunkwise_kernels.py`（7 个内核的 DSL 实现，本讲的 7 个规格就是它们的清单）与 `pegainfer-kernels/csrc/qwen35/` 下的手写 CUDA，体会「Triton 生成 vs 手写」的分工边界；这部分将在 u3（混合注意力核心）正式展开。
- 构建行为还有疑问的，对照 `pegainfer-kernels/tools/triton/README.md` 的「Common failures」逐条演练一遍故障注入（错误 SM、缺 Triton），比读代码更快建立直觉。
