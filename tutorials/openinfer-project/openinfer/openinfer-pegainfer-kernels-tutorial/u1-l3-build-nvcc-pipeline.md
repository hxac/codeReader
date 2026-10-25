# 构建管线 I：nvcc 编译、SM 检测与 FlashInfer 头

## 1. 本讲目标

本讲是构建管线两讲中的第一讲，聚焦 `build.rs` 里 **nvcc 阶段**的机械原理。学完本讲，你应该能够：

1. 沿着 `main()` 讲出 nvcc 阶段的完整流程：SM 目标探测 → 归一化 → `-gencode` 参数拼装 → `.cu` 文件收集与 feature 过滤 → 并行编译 → `ar` 归档 → 链接指令输出。
2. 说清 `PEGAINFER_CUDA_SM` 何时生效、`nvidia-smi` 探测失败时会发生什么、以及为什么构建脚本选择直接 panic。
3. 解释 `NvccTask` 队列的优先级排序和多线程工作池的工作方式，理解「每次重跑 build script 都全量重编所有 `.cu`」这一事实与 `cargo:rerun-if-changed` 声明之间的关系。
4. 解释 `FlashInferIncludes` 为什么必须把 vendored CCCL 以 `-I` 传入、而 CUDA 工具链头以 `-isystem` 传入。
5. 会用 `PEGAINFER_BUILD_TIMING` 和 `PEGAINFER_NVCC_JOBS` 观察和控制构建。

第二讲（u1-l4）再讲 feature 门控的 AOT 代码生成（Triton、TileLang）与 cubin 嵌入，本讲不展开。

## 2. 前置知识

### 2.1 build script 是什么

Cargo 允许每个包在编译前先跑一个 `build.rs`（构建脚本），典型用途是编译 C/C++ 依赖、生成代码。脚本通过 `println!("cargo:xxx")` 向 Cargo「回话」：

- `cargo:rerun-if-changed=<路径>`：**一旦脚本打印了任何 rerun-if 指令，Cargo 就不再使用默认规则**（包内任意文件变化即重跑），只监控被声明的路径。
- `cargo:rerun-if-env-changed=<变量名>`：声明的环境变量取值变化时重跑脚本。
- `cargo:rustc-link-search` / `cargo:rustc-link-lib`：告诉 rustc 去哪找库、链接哪些库。
- `cargo:warning=...`：在构建输出里打印一条 warning，这是 build script 打日志的标准通道——本讲的很多「观察点」都靠它。

build script 的产物目录由环境变量 `OUT_DIR` 给出，本讲所有 `.o` 和归档文件都写在那里。

### 2.2 SM、SASS、PTX 与 `-gencode`

- **SM（Streaming Multiprocessor）架构版本**是 NVIDIA GPU 的代际编号，如 `sm_80`（A100）、`sm_89`（RTX 4090）、`sm_120`（RTX 50 系列）。编号常写成「主版本.次版本」形式，如 `8.9`、`12.0`。
- nvcc 一次可以为多个目标编译：`-gencode arch=compute_XX,code=sm_XX` 生成针对该架构的 **SASS**（真机码）；`-gencode arch=compute_XX,code=compute_XX` 则把 **PTX**（类汇编的中间表示）嵌进 fatbin，驱动可以在未来更新的 GPU 上 JIT 执行它。
- 新架构还带字母后缀的「家族目标」，如 `sm_90a`、`sm_100f`、`sm_120f`，它们解锁该代特有的指令（cluster、tcgen05 等），但**不能跨代 JIT**，且要求较新的 nvcc 才认识。

本讲的构建脚本要回答的问题就是：这张机器上有哪些 GPU？nvcc 认不认识对应目标？编译参数怎么拼？

### 2.3 `-I` 与 `-isystem` 的搜索顺序

nvcc（沿用 host 编译器语义）的头文件搜索顺序里，**`-I` 目录排在 `-isystem` 目录之前**。所以「vendored（随仓库 vendor 进来的）副本用 `-I`、系统/工具链头用 `-isystem`」是一种标准的覆盖手法：同名头文件时 vendored 版本获胜，同时系统头仍享受「不告警」待遇。4.3 节会看到本 crate 正是用这一招让新版 CCCL 压过 CUDA 工具链自带的旧版。

### 2.4 与前两讲的衔接

u1-l1 讲过：本 crate 用 cargo feature 门控各模型线的 kernel 面，默认 feature 为空；u1-l2 讲过：`csrc/<模型线>/` 目录与 feature 多对多对应。本讲要看这些规则在 `build.rs` 里**如何被机械执行**——feature 决定哪些 `.cu` 进入编译队列，SM 探测决定它们编译成什么目标。

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| [build.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs) | 本讲主角：约 2900 行的构建管线。SM 探测（L319-L528）、FlashInfer 头定位（L1061-L1147）、`main()` 的 nvcc 阶段（L2206-L2931）都在这里 |
| [Cargo.toml](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/Cargo.toml) | `[build-dependencies]` 声明 `cc` 与 workspace 内的 `pegainfer-build`（CUDA 工具链发现逻辑所在）；`[features]` 表是 csrc 目录过滤的开关 |
| [csrc/common.cuh](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/common.cuh) | 所有 CUDA 源共享的设备端公共头（warp 归约、越界检查）。本讲把它当作 rerun 语义的实物样本 |

另外两个背景事实：CUDA 工具链的发现（`CudaToolkit::discover()`、`header_dir("cuda.h")`、`link_search()`）由 workspace 兄弟 crate `pegainfer-build/src/lib.rs` 提供，作为 `[build-dependencies]` 被本 crate 引用（[Cargo.toml:27-30](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/Cargo.toml#L27-L30)）；`third_party/flashinfer` 等上游依赖是 git 子模块，`build.rs` 开头会自动 `git submodule update --init`。

## 4. 核心概念与源码讲解

### 4.1 SM 探测与 PEGAINFER_CUDA_SM

#### 4.1.1 概念说明

一台机器可能装着多代 GPU（例如同时有 A100 和 H100）。把所有 `.cu` 编译成只支持 `sm_80` 的代码，在 H100 上会退化或直接无法运行；反过来全量编译历代目标又浪费时间。所以构建的第一步是回答「**为谁编译**」，答案是一个 SM 目标列表，来源优先级为：

1. 环境变量 `PEGAINFER_CUDA_SM`（或别名 `CUDA_SM`），逗号分隔多个目标——为无 GPU 的构建机（CI、交叉编译）准备的逃生门；
2. `nvidia-smi --query-gpu=compute_cap` 自动探测，多卡去重；
3. 两者都失败 → 打 warning 后 **panic**，构建终止。

这是典型的 fail-fast 设计：与其静默编出错误目标的 fatbin、等到运行期才报 `no kernel image is available for execution on the device`，不如在构建期就拒绝。

#### 4.1.2 核心流程

```
detect_sm_targets()
  ├─ 读 PEGAINFER_CUDA_SM / CUDA_SM → 逐 token parse_sm_token → 有效则直接返回
  ├─ 否则 sm_targets_from_nvidia_smi()
  │    nvidia-smi --query-gpu=compute_cap --format=csv,noheader
  │    每行取第一列 → parse_sm_token → BTreeSet 去重排序
  └─ 都失败 → cargo:warning + panic!("GPU detection failed")

随后（main 中）：
  normalize_nvcc_sms(sms, nvcc)
    └─ nvcc_supported_arches(): nvcc --list-gpu-arch → 收集 compute_* 集合
       normalize_nvcc_sm(): "120"/"120f"→"120f"、"121"/"121a"→"121a"
                            （仅当 nvcc 列出对应 compute_ 目标，否则回退裸数字并告警）
  nvcc_arch_args(normalized)
    ├─ 每个 sm: -gencode arch=compute_{sm},code=sm_{sm}   （SASS）
    └─ 最大 sm 额外: -gencode arch=compute_{max},code=compute_{max} （PTX，向前兼容）
```

`parse_sm_token` 的归一化规则值得记住：剥掉 `sm_`/`compute_` 前缀；`主.次` 形式合并为两位数字（`8.0`→`80`、`12.0`→`120`），允许字母后缀（`9.0a`→`90a`）；单个数字会补零（`8`→`80`）；其余一律判非法并跳过。

#### 4.1.3 源码精读

探测入口，环境变量优先、nvidia-smi 兜底、失败即 panic——[build.rs:435-465](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L435-L465)：

```rust
fn detect_sm_targets() -> Vec<String> {
    if let Ok(env) = std::env::var("PEGAINFER_CUDA_SM").or_else(|_| std::env::var("CUDA_SM")) {
        let mut sms = Vec::new();
        for token in env.split(',') {
            if let Some(sm) = parse_sm_token(token) { sms.push(sm); } else { /* warning 并跳过 */ }
        }
        if !sms.is_empty() { return sms; }
    }
    if let Some(sms) = sm_targets_from_nvidia_smi() { return sms; }
    print!("cargo:warning=Failed to detect GPU SMs via nvidia-smi. Set PEGAINFER_CUDA_SM/CUDA_SM ...");
    panic!("GPU detection failed");
}
```

注意它**先设 env 就完全不查 nvidia-smi**——所以设置错误的目标值不会被自动纠正，运行期才会暴露（见 4.1.5 练习 1）。

nvidia-smi 探测与 token 归一化——[build.rs:410-433](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L410-L433) 用 `BTreeSet` 同时完成去重与排序：

```rust
fn sm_targets_from_nvidia_smi() -> Option<Vec<String>> {
    let output = Command::new("nvidia-smi")
        .args(["--query-gpu=compute_cap", "--format=csv,noheader"]).output().ok()?;
    // 每行取逗号前第一列，parse_sm_token 归一化，塞进 BTreeSet 去重
}
```

nvcc 能力探测：`--list-gpu-arch` 列出它认识的所有 `compute_*`——[build.rs:367-381](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L367-L381)。随后 [build.rs:383-400](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L383-L400) 的 `normalize_nvcc_sm` 把 `120` 升级成家族目标 `120f`、`121` 升级成 `121a`——但仅当 nvcc 列表里有 `compute_120f`/`compute_121a`；否则退回裸数字并打 warning。这解释了为什么同一张 GPU 在不同 CUDA 版本下会编出不同目标：**探测结果是「显卡能力」与「nvcc 能力」的交集**。

`-gencode` 拼装——[build.rs:467-483](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L467-L483)：

```rust
fn nvcc_arch_args(normalized_sms: &[String]) -> Vec<String> {
    // 每个 sm 一条 -gencode arch=compute_{sm},code=sm_{sm}
    // 再为数值最大的 sm 追加一条 arch=compute_{max},code=compute_{max}（嵌入 PTX）
}
```

最后一条 PTX gencode 是向前兼容的经典手法：未来新卡上没有匹配 SASS 时，驱动可以 JIT 执行 PTX。此外还有一个更激进的探测函数 `nvcc_accepts_gencode`（[build.rs:485-528](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L485-L528)）：它在临时目录写一个空的 `__global__` 函数、真的调 nvcc 编一次，用「编译能否成功」来验证 nvcc 是否接受某个 gencode 组合——供 glm52/k3 的 `sm_100f` 特化路径使用（u8 展开）。

#### 4.1.4 代码实践

**实践目标**：亲眼看到探测链的每一环。

1. 在有 GPU 的机器上直接运行：
   ```bash
   nvidia-smi --query-gpu=compute_cap --format=csv,noheader
   ```
2. 对照 [build.rs:319-352](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L319-L352) 的 `parse_sm_token`，手工推导你的输出（如 `8.9`）会被归一化成什么。
3. 运行 `nvcc --list-gpu-arch | tail -5`，看你的 nvcc 是否列出 `compute_120f`。
4. 执行一次构建（默认 feature 即可）并 grep 构建输出：
   ```bash
   cargo build --release -p pegainfer-kernels 2>&1 | grep -E "SM targets|nvcc targets"
   ```
5. **需要观察的现象**：两条 `cargo:warning`——`Detected CUDA SM targets: sm_XX`（探测原始结果）与 `Compiling CUDA kernels for nvcc targets: sm_XX[f|a]`（归一化后实际传给 nvcc 的目标）。若你的 nvcc 较老，还可能出现 `nvcc does not list compute_120f; compiling CUDA kernels for raw sm_120` 这类回退告警。
6. **预期结果**：两条 warning 中的目标一致或仅差家族后缀。具体数值随机器而定，**待本地验证**。（注意：构建需要本机安装 CUDA 工具链与已初始化的 git 子模块。）

#### 4.1.5 小练习与答案

**练习 1**：在 sm_89 的机器上设置 `PEGAINFER_CUDA_SM=120`，构建期和运行期分别会发生什么？

答案：构建期完全正常——`detect_sm_targets` 读到有效 token 就直接返回，不查 nvidia-smi；nvcc 顺利编出 `sm_120` SASS 加 `compute_120` PTX。运行期才失败：fatbin 里既没有 `sm_89` SASS，`compute_120` PTX 也无法 JIT 到更老的架构，加载 kernel 时报 no kernel image 之类的驱动错误。教训：这个变量是为「无 GPU 的构建机」准备的，不要在有卡机器上乱设。

**练习 2**：`parse_sm_token` 对 `"9.0a"`、`"8"`、`"compute_100"` 分别返回什么？

答案：`"90a"`（主 9 次 0 加后缀 a）、`"80"`（单数字补零）、`"100"`（剥 `compute_` 前缀）。

**练习 3**：为什么探测失败时选择 panic，而不是回退到一个保守的默认 SM（比如 `80`）？

答案：错误目标的问题会在运行期才暴露，且症状（kernel 加载失败）远离病因（构建期配置），排查成本高。fail-fast 加一条指明出路的 warning（「Set PEGAINFER_CUDA_SM/CUDA_SM to override」）把失败留在构建期，是更便宜的失败。

### 4.2 NvccTask 并行编译

#### 4.2.1 概念说明

本 crate 约有 70+ 个 `.cu` 翻译单元，单个 FlashInfer 模板实例化的 TU 可能要编几分钟，串行编译不可接受。`build.rs` 的做法是经典的生产者-消费者模型：

- 先按 feature 过滤出要编译的 `.cu` 列表，为每个文件生成一份完整的 nvcc 参数，打包成 `NvccTask`；
- 全部任务按**优先级排序**后塞进一个 `Mutex<VecDeque>` 队列；
- 起 N 个操作系统线程，各自循环 `pop_front` 取任务、同步调 nvcc、记录退出码；全部 join 后统一断言成功；
- 最后 `ar rcs` 把所有 `.o` 打成 `libkernels_cuda.a`，输出 `cargo:rustc-link-lib=static=kernels_cuda` 等链接指令。

一个重要事实：**工作循环里没有任何增量检查**——build script 每跑一次，每个任务都无条件 nvcc。也就是说，这个 crate 真正的「增量编译」策略不是跳过 `.o`，而是**靠 `cargo:rerun-if-changed` 声明尽量让 build script 整个不重跑**（见 4.2.3 与综合实践）。

#### 4.2.2 核心流程

```
main() nvcc 阶段：
  collect_files_recursively(csrc) ──→ csrc_files（所有文件）
      │  feature 过滤（is_gemma4_source / is_kimi_k2_source / ...）
      ▼
  cu_files（.cu 且不在退役名单 {activation,embedding,fused_attention}.cu 中）
      │  逐个组装 nvcc_args：基础参数 + arch 参数（个别文件有专属特化）
      │  + FlashInfer/CCCL 头（命中名单的文件）+ DeepEP/kimi/glm52/k3 专属参数
      ▼
  nvcc_tasks: Vec<NvccTask>
      │  sort_by_key(nvcc_task_priority)   ← 稳定排序，热点 kernel 排前
      ▼
  Mutex<VecDeque<NvccTask>>  ──pop_front──→  N 个 worker 线程并发 nvcc
      ▼
  ar rcs libkernels_cuda.a  ──→  rustc-link-lib=static=kernels_cuda (+cudart/cublas/...)
```

N 个 worker 处理 M 个任务时，理想情况下墙钟时间约为

\[ T_{\text{wall}} \approx \max_i t_i \;+\; \frac{\sum_i t_i - \max_i t_i}{N} \]

即受「最慢单个 TU」下限约束；优先级排序的意义在于让最重的热点 TU（paged_attention 家族）**最先**占住 worker，而不是最后才开编。

#### 4.2.3 源码精读

任务与队列的载体只是三个字段——[build.rs:23-27](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L23-L27)：

```rust
struct NvccTask {
    cu_file: PathBuf,   // 源文件
    obj_file: PathBuf,  // OUT_DIR 下的目标 .o
    args: Vec<String>,  // 完整 nvcc 参数
}
```

feature 过滤决定谁进队列——[build.rs:2263-2313](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L2263-L2313) 递归收集 `csrc/`，然后逐文件问「这个 feature 开了吗？」：例如 `!gemma4_enabled && is_gemma4_source(...)` 就跳过；`activation.cu`、`embedding.cu`、`fused_attention.cu` 三个退役 TU 无论如何都被排除。默认 feature 集为空时，进入队列的就是 `csrc/shared/` 下的常驻共享 TU（`csrc/shared/norm.cu`、`csrc/shared/paged_attention.cu` 等）——`csrc/marlin/` 也被门控排除（需要 `gemma4` 或 `kimi-k2` 之一）。

每个任务的基础参数——[build.rs:2342-2357](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L2342-L2357)：`-c` 单文件编译、`-O3`、CUDA 头以 `-isystem` 传入（工具链头降为系统头）、`-I csrc`（自家头），随后按文件名挂上 4.1 的 arch 参数或专属特化（如 `glm52_fp8_gemm` 走 sm_100 探测分支，其余默认 `arch_args`），最后统一加 `--compiler-options -fPIC`。产物名由 `cuda_object_name` 生成（[build.rs:983-999](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L983-L999)）：`csrc/shared/norm.cu` → `shared_norm_cuda.o`，路径成分用下划线拼接，天然无碰撞。

优先级排序——[build.rs:304-317](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L304-L317)：`paged_attention` 为 0（最优先），hd256/hd512/滑窗变体与 `flashinfer_sampling` 为 1，`flashinfer_top1` 为 2，`flashinfer_norm` 为 3，其余为 10。配合 `sort_by_key` 的稳定排序，同优先级内保持 `cu_files.sort()` 得到的字母序。

工作池——[build.rs:2794-2840](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L2794-L2840)：

```rust
nvcc_tasks.sort_by_key(|task| nvcc_task_priority(&task.cu_file));
let task_queue = Mutex::new(VecDeque::from(nvcc_tasks));
let nvcc_workers = nvcc_jobs.min(task_queue.lock().expect("task queue poisoned").len());
thread::scope(|scope| {
    // N 个线程各自 loop { pop_front → nvcc → 收集 (cu_file, obj_file, status) }
    // join 后统一 assert!(status.success(), "nvcc compilation failed for {}")
});
```

两个细节：worker 数会被 `min` 钳到任务数（任务比线程少时不开空线程）；退出码在 join 之后统一断言，任何一个 TU 失败都会带着文件名 panic，不会静默产出残缺归档。

归档与链接——[build.rs:2847-2863](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L2847-L2863) 先删旧 `libkernels_cuda.a` 再 `ar rcs` 重建；[build.rs:2882-2905](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L2882-L2905) 输出 `rustc-link-lib=static=kernels_cuda`，并链接 `cudart`、`cublas`、`cublasLt`、`cuda`（驱动 API，DeepGEMM 的 `cuLibraryLoadData` 需要）、`nvrtc` 与 `stdc++`。

rerun 声明——[build.rs:2907-2930](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L2907-L2930)：声明了 `csrc/` 目录、`build.rs` 本身、六个环境变量（`PEGAINFER_CUDA_SM`、`CUDA_SM`、`PEGAINFER_FLASHINFER_INCLUDE`、`PEGAINFER_BUILD_TIMING`、`PEGAINFER_NVCC_JOBS`、`PEGAINFER_NCCL_ROOT`）以及 FlashInfer/CUTLASS/CCCL/DeepEP/DeepGEMM/FlashMLA 等第三方目录。另外 [build.rs:2314-2321](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L2314-L2321) 还对 `csrc/` 下每个 `.cu`/`.cuh` 逐个声明了 rerun。而 k3 的 vendored FlashKDA 源码位于 `third_party/`、靠 `#include` 混进 `csrc/k3/k3_flash_kda.cu`，Cargo 的 csrc 目录监控看不到它们，所以 [build.rs:2778-2786](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L2778-L2786) 逐文件显式声明——这是「改了上游头但构建不刷新」这类问题的标准解法。

#### 4.2.4 代码实践

**实践目标**：从源码到产物，完整追踪一个 TU 的旅程（纯源码阅读 + 只读命令，不改任何文件）。

1. 选定 `csrc/shared/norm.cu`，按 [build.rs:2342-2357](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L2342-L2357) 写出它理论上收到的完整 nvcc 命令行（基础参数 + 你机器的 `-gencode` 序列）。
2. 推导它的产物名（用 [build.rs:983-999](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L983-L999) 的规则），然后在构建后验证：
   ```bash
   ls target/release/build/pegainfer-kernels-*/out/ | head -20
   ar t target/release/build/pegainfer-kernels-*/out/libkernels_cuda.a | grep norm
   ```
3. **需要观察的现象**：`out/` 下有形如 `shared_norm_cuda.o` 的对象文件；归档成员表里能找到它。
4. **预期结果**：对象文件名与你按规则推导的一致。构建产物目录具体路径带 hash，请用通配符定位。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`cuda_object_name` 对 `csrc/gemma4/paged_attention_hd512.cu` 输出什么？

答案：`gemma4_paged_attention_hd512_cuda.o`——相对路径各成分（目录 + 去 extension 的文件名）用 `_` 连接，加 `_cuda.o` 后缀。

**练习 2**：既然 build script 每次重跑都全量 nvcc，那么「改一个 `.cu` 只重编一个文件」的效果从哪来？

答案：从 Cargo 的 rerun 机制来。只要 `csrc/` 没有变化、声明的环境变量没变，build script 整个不重跑，旧归档原样复用；一旦重跑，则是全量重编。所以 rerun 声明（4.2.3 末段）就是这个构建系统实际的增量粒度。

**练习 3**：为什么退出码断言放在所有线程 join 之后，而不是每个任务 nvcc 一返回就 panic？

答案：panic 在工作线程里只结束该线程（且 `thread::scope` 的 join 处会 propagate）；统一收集后断言能保证即使某个 TU 失败，也把每条失败信息以「文件名 + 失败」的形式带全，而不是第一个失败就中断其余编译、丢失别的错误线索。（代码事实是逐线程收集 `completed` 向量，join 后循环断言。）

### 4.3 FlashInferIncludes 与 vendored CCCL

#### 4.3.1 概念说明

FlashInfer 是以头文件（header-only）形式被消费的：本 crate 的多个 CUDA TU `#include` FlashInfer 头，所以构建期必须找到一整套 include 目录——FlashInfer 本体、它的 `csrc`、CUTLASS、CUTLASS util、spdlog，以及 CCCL（cub/libcudacxx/thrust）。这些目录的定位逻辑收在 `flashinfer_includes()` 里，优先级为：环境变量 `PEGAINFER_FLASHINFER_INCLUDE` → crate 内子模块 `third_party/flashinfer/include` → workspace 根的子模块 → `.venv` 里 pip 装的 flashinfer 自带头。

最精妙的一点在 `FlashInferIncludes` 的 `cccl` 字段上：**vendored CCCL 必须以 `-I` 传入，而 CUDA 工具链头以 `-isystem` 传入**。CUDA 工具链自带一份（可能偏旧的）CCCL，而 FlashInfer v0.6+ 用到了老版没有的 API（如 `cuda::fast_mod_div`）。由于 `-I` 的搜索优先级高于 `-isystem`，把 vendored CCCL 放前面就能精准覆盖那几个重名头，其余工具链头不受影响。注释里写明这是镜像上游 flashinfer `jit/cpp_ext.py` 的顺序。

#### 4.3.2 核心流程

```
flashinfer_includes()
  ├─ PEGAINFER_FLASHINFER_INCLUDE 且包含 flashinfer/sampling.cuh → 采用
  ├─ 候选链：crate/third_party/flashinfer/include → workspace 同路径
  │          → .venv/lib/python3.{10..13}/site-packages/flashinfer/data/include
  │   （每个候选以 flashinfer/sampling.cuh 是否存在做指纹校验）
  └─ 全部落空 → 回退 crate 内子模块路径（后续 require_* 阶段兜底）

flashinfer_includes_from_include(include)
  └─ 由 flashinfer 根目录派生：csrc、cutlass（3rdparty/ 或顶层）、
     cutlass/util、spdlog、cccl = {cccl/cub, cccl/libcudacxx/include, cccl/thrust}
```

进入 nvcc 参数时的形态（针对命中 FlashInfer 名单的 TU）：

```
-I <cccl>/cub -I <cccl>/libcudacxx/include -I <cccl>/thrust   ← -I，优先级最高
--std=c++17 --expt-relaxed-constexpr
-I <flashinfer>/include -I <flashinfer>/csrc -I <cutlass> ... -I <spdlog>
（而 CUDA 工具链头早在基础参数里就是 -isystem）
```

#### 4.3.3 源码精读

结构体与覆盖动机，注释本身就是文档——[build.rs:29-40](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L29-L40)：

```rust
struct FlashInferIncludes {
    include: PathBuf,      // flashinfer 头本体
    csrc: PathBuf,
    cutlass: PathBuf,
    cutlass_util: PathBuf,
    spdlog: PathBuf,
    /// Vendored CCCL (cub, libcudacxx, thrust). Must be passed as `-I` while the
    /// CTK include dir is `-isystem`, so these override the toolkit's older CCCL
    /// copy — FlashInfer v0.6+ uses APIs (e.g. `cuda::fast_mod_div`) that older
    /// CTK CCCL lacks. Mirrors upstream flashinfer/jit/cpp_ext.py ordering.
    cccl: Vec<PathBuf>,
}
```

候选链与指纹校验——[build.rs:1061-1096](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L1061-L1096)：判断一个目录是不是「有效的 FlashInfer include」的方式是查 `flashinfer/sampling.cuh` 这个哨兵文件是否存在；环境变量指向的目录若不含哨兵文件则告警回退。派生兄弟目录的逻辑在 [build.rs:1098-1147](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L1098-L1147)（`3rdparty/cutlass` 与顶层 `cutlass` 两种布局都兼容，`cccl` 拆成 cub、libcudacxx/include、thrust 三个入口）。

谁享受这套头——[build.rs:2493-2528](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L2493-L2528)：以文件名 stem 判定，命中 `paged_attention`、`paged_attention_hd512`、`paged_attention_hd256`、`paged_attention_window_hd256`、`flashinfer_norm`、`flashinfer_sampling`、`dflash2_selector`、`flashinfer_top1`、`glm52_topk`、`local_kv_fp8`、`qwen4_grouped_rmsnorm` 之一的 TU 才追加 CCCL `-I` 序列与 FlashInfer/CUTLASS/spdlog include；`kimi_*` 与 `marlin_nvfp4*` 前缀的文件在 [build.rs:2558-2583](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L2558-L2583) 走另一组等价参数（多带 marlin 的两个 include）。**默认构建下**这些 FlashInfer 委托 TU（如 `csrc/shared/flashinfer_sampling.cu`）就在编译名单里，所以 vendored 头对默认构建同样必不可少。

顺带一提，被几乎所有 kernel `#include` 的公共设备头是 [csrc/common.cuh:1-25](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/common.cuh#L1-L25)：定义 `WARP_SIZE`、warp 级 sum/max 归约等基础设施。改它一个字符，经 4.2 的 rerun 语义会触发所有 TU 全量重编——它就是「公共头 = 最大重编半径」的活例子。

#### 4.3.4 代码实践

**实践目标**：验证你机器上 FlashInfer 头的实际来源。

1. 确认子模块已初始化（未初始化则 `git submodule update --init --recursive`），然后：
   ```bash
   ls third_party/flashinfer/include/flashinfer/sampling.cuh
   ls third_party/flashinfer/3rdparty/cccl/
   ```
2. 执行任意一次构建，在输出中找 `cargo:warning=Using FlashInfer include dir: ...`（打印点在 [build.rs:2334-2338](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L2334-L2338)）。
3. **需要观察的现象**：warning 里的路径与第 1 步验证存在的路径一致；`cccl/` 下能看到 `cub`、`libcudacxx`、`thrust` 三个子目录。
4. **预期结果**：默认走 crate 内子模块路径。如果你设置了 `PEGAINFER_FLASHINFER_INCLUDE` 指向 pip 安装的 flashinfer，warning 会改指到 site-packages 下的 `data/include`。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：如果把 vendored CCCL 也改成 `-isystem` 传入，会发生什么？

答案：两组目录进入同一优先级层，按出现顺序解析——工具链 include 在基础参数里先出现，工具链自带的旧 CCCL 将获胜，FlashInfer v0.6+ 依赖的新 API（`cuda::fast_mod_div` 等）会编译失败或解析到旧定义。这正是该字段注释强调「must be passed as `-I`」的原因。

**练习 2**：`flashinfer_includes()` 用什么作为「这是一个有效 FlashInfer include 目录」的判据？为什么需要这种校验？

答案：哨兵文件 `flashinfer/sampling.cuh` 的存在性。因为环境变量和候选路径都可能指错（路径拼错、版本不含该头），无校验地采用会让错误推迟到 nvcc 阶段以难懂的「头文件找不到」告终；哨兵校验把失败提前到定位阶段并给出可读的 warning 与回退。

**练习 3**：默认 feature 下（`gemma4`、`glm52` 都关闭），FlashInfer 头还会被用到吗？

答案：会。命中名单是按文件 stem 判定而非按 feature——`csrc/shared/flashinfer_sampling.cu`、`csrc/shared/flashinfer_norm.cu`、`csrc/shared/paged_attention.cu` 等属于常驻共享面，默认构建即编译，因此 vendored FlashInfer 子模块在默认构建中也是必需依赖。

### 4.4 构建计时与并行度环境变量

#### 4.4.1 概念说明

约 70 个 TU、多个分钟级的模板实例化，构建耗时需要被观测、并行度需要被控制。本 crate 提供两个观测/控制旋钮：

- `PEGAINFER_BUILD_TIMING`：打开后，`time_phase(label, f)` 会把每个被包裹的阶段（每个 nvcc TU、`ar`、feature 门控的 Triton/TileLang 生成）的耗时以 `cargo:warning=build-timing <label> <秒>s` 打出来。空串、`0`、`false`、`off` 均视为关闭。
- `PEGAINFER_NVCC_JOBS`：nvcc 工作池线程数。未设置时回落到 Cargo 传给 build script 的 `NUM_JOBS`（即 `cargo build -j` 的并行度），再回落到 CPU 核数，最后保底 1。非法值（非正整数）告警并忽略。

另有一个顺路的小机关：`nvcc_command()`（[build.rs:354-365](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L354-L365)）支持 `PEGAINFER_NVCC_LAUNCHER` 前缀——设置后所有 nvcc 调用（包括 `--list-gpu-arch` 探测和 4.1 的编译探针）都变成 `<launcher> nvcc ...`，可以用来接缓存包装器等工具；它同样被声明为 rerun-if-env-changed。

#### 4.4.2 核心流程

```
time_phase(label, f):
  未开计时 → 直接 f()
  开计时   → 记 Instant → f() → cargo:warning=build-timing {label} {elapsed:.3}s

nvcc_job_count():
  PEGAINFER_NVCC_JOBS（正整数）
    ↳ 否则 NUM_JOBS（cargo 提供）
        ↳ 否则 available_parallelism()
            ↳ 保底 1
  worker 数再 min(任务总数)（4.2 已见）
```

计时粒度 \(\text{耗时} = t_{\text{end}} - t_{\text{start}}\) 是每阶段独立的墙钟差值；由于多 worker 并发，所有 `nvcc <file>` 条目的耗时之和会显著大于整段构建的墙钟时间——这是并行度生效的直接证据。

#### 4.4.3 源码精读

开关与计时器——[build.rs:260-280](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L260-L280)：

```rust
fn build_timing_enabled() -> bool {
    std::env::var("PEGAINFER_BUILD_TIMING").is_ok_and(|value| {
        let value = value.trim().to_ascii_lowercase();
        !(value.is_empty() || value == "0" || value == "false" || value == "off")
    })
}

fn time_phase<T>(label: impl AsRef<str>, f: impl FnOnce() -> T) -> T {
    if !build_timing_enabled() { return f(); }
    let started = Instant::now();
    let result = f();
    println!("cargo:warning=build-timing {} {:.3}s", label.as_ref(), started.elapsed().as_secs_f64());
    result
}
```

注意 `time_phase` 是**零成本默认关闭**的：不开计时时不多打一行输出。

并行度解析——[build.rs:282-302](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L282-L302)：`parse_job_count_env` 拒绝非正整数并打 `Ignoring invalid ...` 告警；`nvcc_job_count` 的回落链让默认体验「跟 cargo 自己的 `-j` 对齐」。worker 数在 [build.rs:2797](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L2797) 被钳到任务数，并在 [build.rs:2332-2333](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L2332-L2333) 无条件打印 `Compiling CUDA translation units with N nvcc job(s)`（这条 warning 不需要开计时就能看到）。

计时调用点：每个 TU 的 nvcc 调用在 worker 循环里被 `time_phase(format!("nvcc {}", cu_file), ...)` 包裹（[build.rs:2812-2822](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L2812-L2822)），归档阶段是 `time_phase("ar libkernels_cuda.a", ...)`（[build.rs:2856-2861](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L2856-L2861)）；feature 门控的 Triton/TileLang 阶段同样包裹（u1-l4 展开）。两个环境变量都在 rerun 声明清单里（[build.rs:2912-2913](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L2912-L2913)），所以切换它们的取值会触发 build script 重跑——对 `BUILD_TIMING` 来说这是刻意的便利：改了开关就能立刻看到带计时的输出，不用先碰一个源文件。

#### 4.4.4 代码实践

**实践目标**：量化并行度对构建的影响。

1. 先做一次基线构建让依赖就绪，然后分别计时：
   ```bash
   time cargo build --release -p pegainfer-kernels                       # 默认并行度
   time env PEGAINFER_NVCC_JOBS=1 cargo build --release -p pegainfer-kernels
   ```
   （若想让第二次必然重跑 build script，可先 `touch build.rs`。）
2. **需要观察的现象**：单作业版本显著更慢；输出始终包含 `Compiling CUDA translation units with 1 nvcc job(s)`。
3. 再叠加计时观察热点：
   ```bash
   env PEGAINFER_BUILD_TIMING=1 cargo build --release -p pegainfer-kernels 2>&1 \
     | grep "build-timing" | sort -t' ' -k3 -rn | head
   ```
4. **预期结果**：耗时榜前列通常是 FlashInfer 模板重量级 TU（paged_attention 家族、flashinfer_sampling 等），与 4.2 的优先级名单相互印证；`nvcc ...` 条目耗时之和远大于总墙钟。具体数值随机器而定，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`PEGAINFER_NVCC_JOBS=0` 或 `=abc` 会怎样？

答案：`parse_job_count_env` 要求解析为正整数，否则打 `cargo:warning=Ignoring invalid PEGAINFER_NVCC_JOBS=...` 并返回 `None`，并行度回落到 `NUM_JOBS`/CPU 核数——不会 panic，构建继续。

**练习 2**：为什么 `PEGAINFER_BUILD_TIMING` 也被声明为 `rerun-if-env-changed`？它并不影响编译产物。

答案：因为计时输出是 build script 的副作用，脚本不重跑就看不到新输出。声明它之后，用户拨动开关即触发重跑，立刻拿到带计时的构建日志；代价只是多一次全量 nvcc（无增量检查，见 4.2）。

**练习 3**：同时设 `PEGAINFER_BUILD_TIMING=off` 和 `=1` 是不可能的，但设成 `OFF`（大写）呢？

答案：仍然关闭——`build_timing_enabled` 先 `trim` 再 `to_ascii_lowercase` 后比对，大小写与空白都不敏感。

## 5. 综合实践

把本讲四个模块串成一个实验序列（全程只读，不修改任何源码）。**前置**：机器装有 CUDA 工具链（nvcc）且 git 子模块已初始化；有无 GPU 均可，第 3 步专为无 GPU 场景设计。

1. **定位**。在 `build.rs` 中找到并记下行号：SM 探测入口 `detect_sm_targets`、token 归一化 `parse_sm_token`、nvcc 能力探测 `nvcc_supported_arches`、`-gencode` 拼装 `nvcc_arch_args`、优先级函数 `nvcc_task_priority`、队列与工作池（`Mutex<VecDeque>` + `thread::scope`）。（本讲引用的行号可直接对照验证。）
2. **计时构建**。执行：
   ```bash
   env PEGAINFER_BUILD_TIMING=1 cargo build --release -p pegainfer-kernels 2>&1 | tee /tmp/build1.log
   ```
   从日志整理一张表：阶段（`nvcc <file>` / `ar libkernels_cuda.a`）、条数、最慢五条、所有 nvcc 条目耗时之和 vs 总墙钟。回答：最慢的 TU 在 4.2 的优先级名单里吗？`Compiling CUDA translation units with N nvcc job(s)` 的 N 是多少、从哪条回落链来的？
3. **无 GPU 覆盖**。在没有 GPU（或没有 nvidia-smi）的环境：
   ```bash
   env PEGAINFER_CUDA_SM=120 cargo build --release -p pegainfer-kernels 2>&1 | tee /tmp/build2.log
   ```
   观察日志中的 `Detected CUDA SM targets: sm_120`，以及归一化结果——若你的 nvcc 列出 `compute_120f`，第二条 warning 是 `sm_120f`；否则出现回退告警且保持 `sm_120`。用 `nvcc --list-gpu-arch | grep 120` 解释你看到的是哪一种。
4. **解释 rerun 触发条件**。依据 [build.rs:2907-2930](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L2907-L2930) 及 2.1 节的 Cargo 语义，写出并验证（第二次构建看是否出现 `Compiling pegainfer-kernels`/nvcc warning，第三次同法）：
   - 什么都不改再 build → build script 不重跑（输出无上述 warning，秒回）；
   - `touch csrc/common.cuh` 后 build → 重跑且全量 nvcc（该头被几乎所有 TU include）；
   - `env PEGAINFER_CUDA_SM=89 cargo build ...` → 仅因环境变量取值变化而重跑；
   - 改动 `third_party/flashinfer` 子模块内的头 → 重跑（`tracked_dirs` 覆盖 FlashInfer/CCCL 各目录）。
   把四条实验的「触发源 → 对应 rerun 声明」写成一张两列表。
5. **预期结果**：一张耗时表、一份 SM 覆盖观察记录、一张 rerun 触发对照表。所有具体数值**待本地验证**；若第 3 步机器没有 nvcc，构建会在工具链发现阶段失败，此时把观察降级为「在 build.rs 里纸面追踪 `PEGAINFER_CUDA_SM=120` 的数据流」即可。

## 6. 本讲小结

- SM 探测三段式：`PEGAINFER_CUDA_SM`/`CUDA_SM` 环境变量优先（设了就不查硬件）→ `nvidia-smi --query-gpu=compute_cap` 自动探测 → 失败 fail-fast panic；`parse_sm_token` 负责把 `8.9`/`sm_120f` 等形态归一化成统一 token。
- 探测结果还要过 nvcc 能力这一关：`--list-gpu-arch` 决定 `120` 能否升级为家族目标 `120f`；`nvcc_arch_args` 为每个目标编 SASS，并为最大目标嵌入 PTX 以向前兼容。
- `.cu` 按 feature 过滤后打包成 `NvccTask`，稳定排序让 paged_attention 等热点 TU 最先编译；`Mutex<VecDeque>` + N worker 线程并发 nvcc，join 后统一断言退出码，`ar` 成 `libkernels_cuda.a` 静态链接进 crate。
- 工作循环无增量检查，每次重跑全量重编——真正的增量策略是 rerun 声明：`csrc/`、`build.rs`、六个环境变量、FlashInfer/CCCL/Deep* 第三方目录；被 `#include` 混入的 vendored 头要逐文件显式声明。
- FlashInfer 头由哨兵文件 `flashinfer/sampling.cuh` 校验定位；vendored CCCL 以 `-I` 压过工具链的 `-isystem` 头，解决 FlashInfer v0.6+ 依赖新版 CCCL API 的问题。
- `PEGAINFER_BUILD_TIMING` 输出每阶段墙钟耗时，`PEGAINFER_NVCC_JOBS` 控制并行度（回落链：显式设置 → cargo 的 `NUM_JOBS` → CPU 核数 → 1），两者切换都会触发 build script 重跑。

## 7. 下一步学习建议

下一讲 **u1-l4（构建管线 II：feature 门控的 AOT 生成与 cubin 嵌入）** 讲同一个 `build.rs` 的另外半边：`qwen35` 的 Triton AOT（`TritonKernelSpec`、`gen_triton_aot.py`、解释器探测链）、glm52/k3 的 TileLang 与 DeepGEMM/FlashMLA CUDA、以及 checked-in cubin 如何变成可链接符号。建议先读 `tools/triton/README.md` 与 `cubin/glm52/README.md` 热身。之后进入 u2，离开构建期、转向 Rust 侧运行时（`DeviceContext`、`GpuTensor` 与 FFI）。若想巩固本讲，可重读 [build.rs:2206-2246](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/build.rs#L2206-L2246) 的 `main()` 开场，自己画出完整阶段的时序草图。
