# 对比基线生态总览

## 1. 本讲目标

本讲是「工程实践、对比基线生态与二次开发」单元的开篇。前面六个单元里，我们已经逐个拆解过 TileLang 内核与若干单个基线（cuBLAS、Triton、BitBLAS、Marlin、CUTLASS、bitsandbytes）。本讲不再深入任何单个内核，而是退后一步，**把项目里出现的全部对比基线当成一个生态来俯瞰**。

读完本讲，你应当能够：

- 说出项目里出现的十余个基线各自是**什么**（厂商库 / DSL / 编译器 / PyTorch 参考）、**定位**是什么、**覆盖哪些算子**。
- 给定一个算子（如 W4A16 反量化、fp8 GEMM、MLA decode），能选出**合适的基线**作为对比标尺。
- 理解 `install_*.sh` 这一类依赖脚本是如何把第三方框架装进来的，以及它们的安装复杂度差异（`pip` 一行 vs 从源码编译 TVM）。
- 制作一张「基线 × 算子 × 架构」覆盖表，并指出哪些基线是 **AMD 专用**（aiter / CK / Ladder / rocBLAS）、哪些是 **NVIDIA 专用**（cuBLAS / CUTLASS / bitsandbytes / DeepGEMM）。

> 本讲承接 u2-l6（Triton 基线与 `do_bench`）。在那之后，你已经熟悉了「provider（实现）」这个概念：同一算子在同一架构下，会被多个 provider 各跑一遍，结果并列对比。本讲把全部 provider 一次罗列清楚。

## 2. 前置知识

### 什么是「基线」（baseline）

在性能基准测试里，**被评估的主角**（本项目是 TileLang 写的算子内核）需要一个或多个**参照物**来衡量「快还是慢」。这些参照物就是基线。一个公平的对比通常包含：

1. 一个**理论上限**或**厂商最优**基线（如 cuBLAS、rocBLAS、CK）——代表「硬件厂商调到极致能到多少」。
2. 一个**同类 DSL/编译器**基线（如 Triton、BitBLAS、Welder/Ladder）——代表「同行用相近抽象层能做到多少」。
3. 一个**专项内核**基线（如 Marlin 专做 W4A16、FA3 专做 Attention）——代表「针对该算子手搓极致能做到多少」。
4. 一个**正确但慢的参考实现**（如 PyTorch eager）——不参与性能冠军争夺，主要用来验证数值正确、顺便给出一个「朴素基线」下限。

本项目把每个 provider 放进一个**编号子目录**（详见 u1-l2），编号 `0.` 通常就是基线，主角 TileLang 通常在编号 `1.` 或 `2.` 或 `3.`（编号不固定，这是历史遗留）。

### 厂商、DSL、编译器、参考实现四类基线

| 类别 | 代表基线 | 特点 |
|------|----------|------|
| 厂商库 | cuBLAS、rocBLAS、CK、aiter | 二进制/闭源或 C++ 模板，针对自家硬件调优，通常是该硬件上的「天花板」 |
| DSL / 编译器 | Triton、BitBLAS、TileLang(主角)、Welder(Ladder) | 用高层 DSL 描述算子，靠 autotune 搜出好调度，可移植 |
| 专项内核 | Marlin、CUTLASS fpa_intb、FA3、bitsandbytes | 针对某一精度/算子手写或模板生成的 kernel |
| 参考实现 | torch (`torch.nn.Linear` / einsum) | 易读、正确、慢，用于校验与朴素下限 |

### 一个易混淆点：「基线名」≠「库名」

本项目目录名沿用了社区俗称，有时和实际 import 的库对不上，读源码时**以 import 语句为准**：

- `4.ladder_benchmark` 实际 import 的是 **`welder`**（Ladder 是 Welder 编译器的产品名）。
- `1.deepgemm_benchmark` 实际跑的是 **BitBLAS**（只是用 fp8 shape，对标 DeepGEMM 这个**工作负载**的名字）。
- `6.rocblas-benchmark` 目录是**空的**（占位，未实现）。

这三处会在后文逐一落到源码。

## 3. 本讲源码地图

本讲涉及的关键文件按基线分组：

| 基线 | 关键文件 | 作用 |
|------|----------|------|
| BitBLAS | `hopper_benchmark/dense_matmul/2.bitblas-benchmark/benchmark_bitblas_matmul.py` | BitBLAS 入口：`MatmulConfig`+`Matmul`+`profile_latency` |
| DeepGEMM(=BitBLAS fp8) | `hopper_benchmark/deepgemm/1.deepgemm_benchmark/benchmark_bitblas_matmul.py`、同目录 `.sh` | 用 BitBLAS 跑 fp8/e4m3 GEMM，对标 DeepGEMM 工作负载 |
| aiter (AMD) | `cdna_benchmark/mla_benchmark/0.aiter_benchmark/benchmark_alter.py`、`install_aiter.sh` | AMD AITER 库做 MLA decode |
| CK (AMD) | `cdna_benchmark/gemm_benchmark/3.ck_benchmark/benchmark.sh` | AMD Composable Kernel 的 C++ 可执行 |
| Ladder/Welder (AMD) | `cdna_benchmark/gemm_benchmark/4.ladder_benchmark/benchmark_ladder_gemm.py`、同目录 `.sh` | Welder 编译器生成 GEMM |
| Marlin | `ampere_benchmark/contiguous_dequant_matmul/1.marlin_benchmark/install_marlin.sh` | W4A16 专项内核依赖安装 |
| CUTLASS fpa_intb | `ada_benchmark/dequant_matmul/2.cutlass_fpa_intb_benchmark/install_dependency.sh` | TVM+CUTLASS 流程依赖安装 |
| bitsandbytes | `ada_benchmark/dequant_matmul/3.bitsandbytes_benchmark/install_bitsandbytes.sh` | NF4 量化库安装 |
| FA3 | `cdna_benchmark/mha_benchmark/benchmark_fa3_mha.py` | flash_attn 库做 MHA |
| torch | `cdna_benchmark/dequantize_matmul/0.torch_benchmark/`（参考实现） | PyTorch eager 参考实现 |

> cuBLAS 与 Triton 的源码已在 u2-l5、u2-l6 精读过，本讲只引用结论。

## 4. 核心概念与源码讲解

### 4.1 核心通用基线：cuBLAS / Triton / BitBLAS

#### 4.1.1 概念说明

这三个是项目里**出场率最高**的基线，几乎每个算子目录都有它们的身影，构成对比的「铁三角」：

- **cuBLAS**：NVIDIA 官方 BLAS 库（C++）。通过 `cublasGemmEx` 一个函数靠模板类型编译期分派 fp32/fp16/int8 三路径，是 NVIDIA GPU 上 GEMM 的事实标尺。本项目用 CUDA C++ 测试床调用（u2-l5）。
- **Triton**：OpenAI 的块级 GPU DSL，Python 写内核、`@triton.jit` 编译、`tl.dot` 自动走 Tensor Core。是 TileLang 最直接的「同代 DSL」对标对象（u2-l6）。
- **BitBLAS**：微软的算子编译栈，Python 层用 `MatmulConfig`+`Matmul` 描述算子，内部同样带 Roller 式调优。覆盖精度极广（fp16/bf16/fp8/int8/**int4/int2/int1/nf4/fp4**），是「一个库打天下」的代表。

#### 4.1.2 核心流程

三者计时口径不同，跨框架对比前必须统一（单位陷阱已在 u2-l4、u2-l6 反复强调）：

| 基线 | 语言 | 计时方式 | 返回单位 | autotune |
|------|------|----------|----------|----------|
| cuBLAS | CUDA C++ | `std::chrono` + `cudaDeviceSynchronize` | **µs**（但 CSV 表头误写） | 无（厂商已调好） |
| Triton | Python DSL | `triton.testing.do_bench` | **ms** | `@triton.autotune` |
| BitBLAS | Python DSL | `matmul.profile_latency()` | ms（内部封装） | `enable_tuning=True` |

BitBLAS 的运行流程是「配置 → 构建 → 自测延迟」三步，全部封装在一个 operator 对象里：

```
MatmulConfig(M,N,K,A_dtype,W_dtype,out_dtype,accum_dtype,...)   # 描述算子语义
        ↓
Matmul(config, target=..., enable_tuning=True)                  # 编译+调优
        ↓
matmul.profile_latency()                                        # 内部计时，返回 ms
```

#### 4.1.3 源码精读

BitBLAS 入口脚本的 import 段直接暴露了它的技术栈——它自带 Roller（`TensorCorePolicy`/`DefaultPolicy`/`CUDA`），与 TileLang 的 Roller 同源：

- [hopper_benchmark/dense_matmul/2.bitblas-benchmark/benchmark_bitblas_matmul.py:4-12](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/2.bitblas-benchmark/benchmark_bitblas_matmul.py#L4-L12) —— 从 `bitblas` 导入 `Matmul`/`MatmulConfig`，并导入 `TensorCorePolicy`、`CUDA`、`apply_and_build` 等 Roller 组件，说明 BitBLAS 内部也是「描述算子 + Roller 推调度」的范式，和 TileLang 是表兄弟。

配置与延迟测量的主体循环：

- [hopper_benchmark/dense_matmul/2.bitblas-benchmark/benchmark_bitblas_matmul.py:178-193](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/2.bitblas-benchmark/benchmark_bitblas_matmul.py#L178-L193) —— `config = config(*input_args)` 把命令行参数实例化为 `MatmulConfig`；`matmul = operator(config, target=target, enable_tuning=True, backend="tir")` 触发编译与调优；`matmul.profile_latency()` 返回 kernel 延迟。对比 TileLang 需要手写 `@autotune`+`@jit`+`best_result.latency`，BitBLAS 把这套流程全藏进了一个对象方法里。

cuBLAS 与 Triton 的入口已在 u2-l5、u2-l6 讲过，此处不重复。

#### 4.1.4 代码实践

1. **目标**：对比三大基线的「入口复杂度」。
2. **步骤**：
   - 打开 `hopper_benchmark/dense_matmul/` 下的 `0.cublas-benchmark/`、`1.triton-benchmark/`、`2.bitblas-benchmark/` 三个目录。
   - 分别找到「从命令行拿到 M/N/K，到打印出一行 latency」的核心代码段。
3. **观察**：cuBLAS 要先 CMake 编译（`compile_and_run.sh`），Triton 与 BitBLAS 都是 `python xxx.py`。
4. **预期**：cuBLAS 最重（编译型 C++），Triton 中等（手写 `@triton.jit` 内核 + `do_bench`），BitBLAS 最轻（一个 `profile_latency()` 调用搞定）。
5. **待本地验证**：实际运行需相应硬件与已安装的 cuBLAS / Triton / BitBLAS。

#### 4.1.5 小练习与答案

**练习 1**：BitBLAS 脚本里 `enable_tuning=True` 的作用是什么？把它设成 `False` 会怎样？
**答案**：开启 BitBLAS 内部的 Roller 调优，在首次运行时搜索调度配置并缓存。设为 `False` 则用默认调度直接编译，首次延迟更低但运行时性能可能更差。

**练习 2**：为什么 cuBLAS 不需要 autotune，而 Triton / BitBLAS / TileLang 都需要？
**答案**：cuBLAS 是厂商闭源库，调度已由 NVIDIA 针对自家架构调到最优；Triton / BitBLAS / TileLang 是可编程 DSL，需要根据具体 shape + dtype 搜出好的 tile/warp/pipeline 配置。

---

### 4.2 量化反量化基线：Marlin / CUTLASS fpa_intb / bitsandbytes

#### 4.2.1 概念说明

这三个基线专攻**低比特权重矩阵乘**（W4A16 / W4A8 / nf4 等），即大模型推理里「权重预量化、激活原精度」的反量化 GEMM（详见 u4-l14、u4-l15）。它们各有定位：

- **Marlin**（IST-DASLab）：W4A16 **专项高速内核**。权重预先打包成 `int32` 容器、per-channel scale，运行时用极致的手写汇编/PTX 做解包与乘加，是 W4A16 上的性能冠军之一。需从 git 源码编译。
- **CUTLASS fpa_intb**：用 **TVM Relax** 描述、由 **CUTLASS** 生成「群组反量化 GEMM」。`fpa_intb` = "fp activation, int (blockwise) weight"。依赖最重（要编译整个 TVM 并钉死 commit）。
- **bitsandbytes nf4**：**NF4**（NormalFloat 4-bit，QLoRA 提出的非均匀量化）+ blockwise 128 量化。`pip` 一行安装，易用性最高，但调度不可细控。

#### 4.2.2 核心流程

三者的依赖安装复杂度是它们最大的差异点，也是本模块的重点。安装复杂度可排序为：

\[
\text{bitsandbytes (pip 一行)} \;<\; \text{Marlin (git clone + pip install -e)} \;<\; \text{CUTLASS fpa_intb (源码编译 TVM)}
\]

CUTLASS fpa_intb 之所以最重，是因为它把整个 TVM 当作宿主，还要开启 CUTLASS 后端并钉死在一个特定 commit 上，任何 TVM 版本漂移都可能让流程跑不通。

#### 4.2.3 源码精读

**Marlin 安装脚本**——git clone 仓库 + 可编辑安装，并用环境变量钉死 CUDA 架构 8.0：

- [ampere_benchmark/contiguous_dequant_matmul/1.marlin_benchmark/install_marlin.sh:1-5](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/ampere_benchmark/contiguous_dequant_matmul/1.marlin_benchmark/install_marlin.sh#L1-L5) —— `export TORCH_CUDA_ARCH_LIST="8.0"` 钉死 Ampere；`git clone https://github.com/IST-DASLab/marlin`；`pip install -e .` 可编辑安装。这个 `8.0` 也说明 Marlin 在本项目的目标架构是 Ampere（A100）。

**CUTLASS fpa_intb 安装脚本**——最重的依赖：克隆 TVM、checkout 固定 commit、cmake 开启 CUTLASS 后端、`make -j 16` 编译：

- [ada_benchmark/dequant_matmul/2.cutlass_fpa_intb_benchmark/install_dependency.sh:2-14](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/ada_benchmark/dequant_matmul/2.cutlass_fpa_intb_benchmark/install_dependency.sh#L2-L14) —— `git clone ... tvm --recursive` 后 `git checkout 2bf3a0a4...`（钉死 commit，避免 API 漂移）；依次 `echo "set(USE_CUTLASS ON)"` 等开启 CUDA/LLVM/CUBLAS/CUTLASS 后端；`cmake -DCMAKE_CUDA_ARCHITECTURES="80" ..` + `make -j 16`。整段约 14 行，却要编译一个完整的 TVM，是全项目**最重的依赖**。

**bitsandbytes 安装脚本**——最轻：

- [ada_benchmark/dequant_matmul/3.bitsandbytes_benchmark/install_bitsandbytes.sh:3](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/ada_benchmark/dequant_matmul/3.bitsandbytes_benchmark/install_bitsandbytes.sh#L3) —— 一行 `pip install bitsandbytes`。易用性换来的是无法细控调度，性能通常不如 Marlin / TileLang。

#### 4.2.4 代码实践

1. **目标**：理解三个量化基线的「安装成本 vs 可控性」权衡。
2. **步骤**：依次打开上面三个 `install_*.sh`，统计每个脚本会执行多少条命令、是否需要联网克隆仓库、是否需要编译 C++。
3. **观察**：bitsandbytes 1 行 pip；Marlin 4 行（clone + install）；CUTLASS fpa_intb 14 行（编译 TVM）。
4. **预期**：脚本行数大致对应复现该基线的难度；越可控的基线（Marlin/CUTLASS）安装越重。
5. **待本地验证**：实际安装需对应 GPU（Ampere/Ada）与编译工具链。

#### 4.2.5 小练习与答案

**练习 1**：为什么 CUTLASS fpa_intb 脚本要用 `git checkout <固定 commit>` 而不是用最新版 TVM？
**答案**：TVM 的 Relax/CUTLASS 集成 API 仍在演进，钉死 commit 是为了锁定脚本编写时验证过可用的版本，避免上游 API 变化导致流程跑不通。这是「依赖最重」基线的典型代价。

**练习 2**：Marlin 脚本里 `TORCH_CUDA_ARCH_LIST="8.0"` 说明它的目标硬件是什么？
**答案**：sm80 即 NVIDIA Ampere（A100）。这与本项目把 Marlin 放在 `ampere_benchmark/` 下的目录约定一致。

---

### 4.3 fp8 与 AMD 生态基线：DeepGEMM / aiter / CK

#### 4.3.1 概念说明

这一组横跨「fp8 精度前沿」与「AMD GPU 生态」两个话题：

- **DeepGEMM**：DeepSeek 开源的 fp8 GEMM 库，是 Hopper 上 fp8（e4m3/e5m2）GEMM 的性能标杆。**本项目里名为 `deepgemm` 的目录实际用 BitBLAS 跑 fp8 shape 来对标这个工作负载**（见 4.3.3），并非直接调用 DeepGEMM 库。
- **aiter**（AMD）：Attention Instant Tuned Effective Runtime，AMD 官方的注意力/M LA 高性能算子库（用于 MI300 系列推理）。本项目用它做 MLA decode 的标尺。
- **CK**（Composable Kernel，AMD）：AMD 的 C++ 模板 GPU 算子库，类似 CUTLASS 之于 NVIDIA。本项目通过运行它编译出的 C++ 可执行文件（`tile_example_gemm_basic`、`tile_example_fmha_fwd`）来取延迟。

> 关键区分：cuBLAS / CUTLASS / DeepGEMM 是 **NVIDIA 专用**；aiter / CK 是 **AMD 专用**。它们互为镜像——cuBLAS↔rocBLAS、CUTLASS↔CK。

#### 4.3.2 核心流程

**「deepgemm」对标流程**（实为 BitBLAS fp8）：

```
benchmark_bitblas_matmul.sh 遍历 (shape × dtype 组合)
        ↓
dtype 组合含 "e4m3_float8 e4m3_float8 float32 float32"   # fp8 激活 + fp8 权重 + fp32 累加
        ↓
python benchmark_bitblas_matmul.py --A_dtype e4m3_float8 --W_dtype e4m3_float8 ...
        ↓
BitBLAS 内部走 fp8 Tensor Core 路径，输出 latency
```

**aiter MLA 流程**：

```
import aiter  →  aiter.mla.mla_decode_fwd(q, kv_buffer, out, indptr, indices, ...)  →  run_perftest 取延迟
```

**CK 流程**（纯 C++，不走 Python）：

```
./composable_kernel/build/bin/tile_example_gemm_basic -m=... -n=... -k=...   →  终端打印延迟
```

#### 4.3.3 源码精读

**「deepgemm」目录实为 BitBLAS fp8**——同一个 `benchmark_bitblas_matmul.py` 文件名，但 dtype 选项里多了 fp8：

- [hopper_benchmark/deepgemm/1.deepgemm_benchmark/benchmark_bitblas_matmul.py:48-64](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/deepgemm/1.deepgemm_benchmark/benchmark_bitblas_matmul.py#L48-L64) —— `--A_dtype` 的 `choices` 含 `e4m3_float8`、`e5m2_float8`、`bfloat16` 等，比 dense_matmul 版本的 BitBLAS 多了 fp8 选项。文件顶部还有 `sys.path.append('./bitblas')`，说明这里用的是**本地一份 bitblas 源码**而非 pip 包。这印证了「以代码为准」：目录叫 deepgemm，跑的是 BitBLAS 的 fp8 路径。

驱动脚本明确列出 fp8 dtype 组合：

- [hopper_benchmark/deepgemm/1.deepgemm_benchmark/benchmark_bitblas_matmul.sh:29-35](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/deepgemm/1.deepgemm_benchmark/benchmark_bitblas_matmul.sh#L29-L35) —— `dtypes` 数组里有 `"e4m3_float8 e4m3_float8 float32 float32"` 和 `"e5m2_float8 e5m2_float8 float32 float32"`，这就是对标 DeepGEMM 的 fp8 测试集；同数组还有 fp16/bf16/int8，作为多精度对照。

**aiter 做 MLA decode**——import `aiter` 后直接调用 `aiter.mla.mla_decode_fwd`：

- [cdna_benchmark/mla_benchmark/0.aiter_benchmark/benchmark_alter.py:6](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/mla_benchmark/0.aiter_benchmark/benchmark_alter.py#L6) —— `import aiter`，这是 AMD AITER 库的入口。
- [cdna_benchmark/mla_benchmark/0.aiter_benchmark/benchmark_alter.py:176-188](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/mla_benchmark/0.aiter_benchmark/benchmark_alter.py#L176-L188) —— `(attn_logits, attn_lse), us_asm = run_perftest(aiter.mla.mla_decode_fwd, q, kv_buffer..., sm_scale)` 调用 AMD 官方 MLA 内核并取微秒级延迟；随后 `ms = us_asm/1e3; tflops = total_flops/ms*1e-9` 换算成 TFlops。注意这里 `total_flops` 的 qk_flops 含 `dim + qk_rope_head_dim` 而 pv_flops 只含 `dim`（与 u6-l20 一致：值 V 只取潜在部分）。

**aiter 安装脚本**——源码可编辑安装：

- [cdna_benchmark/mla_benchmark/0.aiter_benchmark/install_aiter.sh:4-5](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/mla_benchmark/0.aiter_benchmark/install_aiter.sh#L4-L5) —— `cd aiter`（目录里自带一份 aiter 源码）后 `python3 setup.py develop` 可编辑安装。

**CK 用 C++ 可执行取延迟**——不走 Python，直接跑二进制：

- [cdna_benchmark/gemm_benchmark/3.ck_benchmark/benchmark.sh:10-13](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/gemm_benchmark/3.ck_benchmark/benchmark.sh#L10-L13) —— 直接调用 `./composable_kernel/build/bin/tile_example_gemm_basic -m=8192 -n=1024 -k=8192`，参数化 M/N/K 与 stride，输出 `tee` 到结果文件。这与 cuBLAS 用 C++ 测试床的思路一致（编译型基线），只是 cuBLAS 调库、CK 调模板示例。

#### 4.3.4 代码实践

1. **目标**：识别「基线名 vs 实际库」的错位，并区分编译型 vs 脚本型基线。
2. **步骤**：
   - 打开 `hopper_benchmark/deepgemm/1.deepgemm_benchmark/benchmark_bitblas_matmul.py`，确认它 import 的是 `bitblas` 而非 `deep_gemm`。
   - 打开 `cdna_benchmark/gemm_benchmark/3.ck_benchmark/benchmark.sh`，确认 CK 不走 Python 而是直接跑 `bin/`。
3. **观察**：deepgemm 目录 = BitBLAS fp8；CK = 纯 C++ 二进制。
4. **预期**：再次体会「以代码为准」；CK 的计时输出格式与 Python 基线完全不同，下游抽取要单独写正则。
5. **待本地验证**：CK 与 aiter 需 MI300 系列 AMD GPU；DeepGEMM 对标需 Hopper。

#### 4.3.5 小练习与答案

**练习 1**：`deepgemm` 目录里的脚本为什么文件名是 `benchmark_bitblas_matmul.py`？
**答案**：因为它实际用 BitBLAS 跑 fp8 shape，对标的是 DeepGEMM 这个**工作负载/性能标杆**，而非调用 DeepGEMM 库本身。目录名沿用了社区俗称。

**练习 2**：CK 与 cuBLAS 在「调用方式」上有何共性？
**答案**：两者都是**编译型**基线——cuBLAS 通过 CUDA C++ 测试床调 `cublasGemmEx`，CK 直接运行编译好的 C++ 模板可执行 `tile_example_gemm_basic`，都不像 Triton/BitBLAS/TileLang 那样用 Python `python xxx.py` 驱动。

---

### 4.4 编译器与参考实现：Ladder / torch / FA3

#### 4.4.1 概念说明

最后一组覆盖「编译器型基线」「参考实现」与「专项 Attention 库」：

- **Ladder**：实际 import 的是 **Welder** 编译器（`import welder`）。Welder/Ladder 是一个支持多后端的算子编译栈，本项目在 MI300 上用它做 GEMM 标尺，代表「另一种编译器型 DSL」的视角。
- **torch**（`0.torch_benchmark`）：PyTorch eager 的参考实现（如 `torch.nn.Linear`、`einsum`+`softmax`）。**正确但慢**，主要给数值校验当 ground truth，顺便作为朴素基线下限。
- **FA3**（FlashAttention-3）：通过 `flash_attn` 库的 `flash_attn_qkvpacked_func` 调用。FlashAttention 系列是 Attention 的事实最优库，FA3 是其在 Hopper/CDNA 上的高性能版本，是 Attention 算子的「专项冠军」标尺。

#### 4.4.2 核心流程

**Welder/Ladder** 流程：用 `welder.arch.MI300()` 绑定架构 → 用 `tvm.te`/`tir` 描述算子 → `welder` 编译调度 → 计时。

**torch 参考** 流程：`torch.nn.Linear(K,N).cuda().half()` 构造 → warmup 5 次 → 计时 10 次取均值。

**FA3** 流程：`flash_attn_qkvpacked_func(qkv, causal=...)` 一次调用 → warmup 5 次 → 计时 10 次。TFlops 按

\[
\text{total\_flops} = 2 \times (2 \cdot B \cdot H \cdot N_{\text{ctx}}^2 \cdot d_{\text{head}}), \quad \text{causal 时} \times 0.5
\]

换算（与 u2-l4、u5-l17 一致）。

#### 4.4.3 源码精读

**Ladder 实为 Welder**——import 段直接暴露：

- [cdna_benchmark/gemm_benchmark/4.ladder_benchmark/benchmark_ladder_gemm.py:1-15](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/gemm_benchmark/4.ladder_benchmark/benchmark_ladder_gemm.py#L1-L15) —— `import welder`、`from welder.graph import IRNode, OutputNode`、`from welder.policy import *`、`from tvm.script import tir as T`、`arch = welder.arch.__getattribute__("MI300")()`。这印证了「Ladder 目录 = Welder 编译器」。驱动脚本 `benchmark_ladder.sh` 也设了 `export PYTHONPATH=.../welder/python` 和 `LADDER_HOME`，进一步坐实。

**torch 参考实现**——朴素 `nn.Linear`：

- [cdna_benchmark/dequantize_matmul/0.torch_benchmark/](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/dequantize_matmul/0.torch_benchmark/) 目录下的脚本用 `model = torch.nn.Linear(K, N, bias=False).cuda().half()`、`ref_program(A): return model(A)`、warmup 5 + 计时 10。它不追求性能，只给「正确答案」和朴素下限。

**FA3**——flash_attn 库调用：

- [cdna_benchmark/mha_benchmark/benchmark_fa3_mha.py:4](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/mha_benchmark/benchmark_fa3_mha.py#L4) —— `from flash_attn.flash_attn_interface import flash_attn_qkvpacked_func as flash_attn_func`，即 Dao-AILab FlashAttention 库的打包 QKV 接口。
- [cdna_benchmark/mha_benchmark/benchmark_fa3_mha.py:40-47](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/mha_benchmark/benchmark_fa3_mha.py#L40-L47) —— `total_flops = 2 * flops_per_matmul`（两次矩阵乘），`if casual: total_flops *= 0.5`（causal 折半），`tflops = total_flops / ref_latency * 1e-12`。注意这里用的是朴素 `time.time()` + `torch.cuda.synchronize` 计时，而非 `do_bench`，是参考实现的典型写法。

#### 4.4.4 代码实践

1. **目标**：区分「编译器型基线」与「参考实现」。
2. **步骤**：
   - 打开 `benchmark_ladder_gemm.py` 第 1 行确认 `import welder`。
   - 打开 `benchmark_fa3_mha.py` 第 4 行确认 `flash_attn`。
   - 对比两者的计时方式：Welder 用自身 `PopenPoolExecutor` 选最优；FA3 用朴素 `time.time()`。
3. **观察**：Welder/Ladder 是完整的编译+调优栈（有 policy、arch、cost model）；FA3 只是调用一个已编译好的库。
4. **预期**：编译器型基线首次运行慢（要编译搜调度），库型基线首次运行即快。
5. **待本地验证**：Welder 需 MI300 + 本地源码；flash_attn 需匹配 GPU 架构。

#### 4.4.5 小练习与答案

**练习 1**：Ladder 目录里的脚本为什么 import 的是 `welder` 而不是 `ladder`？
**答案**：Ladder 是 Welder 编译器的产品名，开源代码包名是 `welder`。目录名用社区俗称 Ladder，实际依赖的是 Welder 的 Python 包。这是「以 import 为准」的又一例。

**练习 2**：FA3 脚本里 `total_flops *= 0.5` 在什么条件下触发？为什么是 0.5？
**答案**：在 `casual=True`（因果注意力）时触发。因为 causal 掩码把分数矩阵 $S=QK^\top$ 的上三角置为 $-\infty$，有效计算量约为整方阵的一半，故算功折半（与 u2-l4、u5-l17 一致）。

---

### 4.5 依赖管理：install_*.sh 的三种范式

> 这一节把散落各处的 `install_*.sh` 归纳成三种范式，作为「为给定算子选基线」时的可行性参考。

#### 4.5.1 概念说明

本项目的对比基线大多是**第三方框架**，不会随仓库一起分发，需要读者自行安装。每个需要特殊安装的基线都带一个 `install_*.sh`，它们可归为三类：

| 范式 | 代表 | 安装成本 | 可控性 |
|------|------|----------|--------|
| pip 直装 | bitsandbytes | 最低 | 最低 |
| 源码可编辑安装 | Marlin、aiter | 中 | 中 |
| 编译整个宿主框架 | CUTLASS fpa_intb（编译 TVM） | 最高 | 最高 |

#### 4.5.2 核心流程

```
pip install bitsandbytes                          # 范式 1：一行 pip
git clone <repo> && pip install -e .              # 范式 2：克隆 + 可编辑安装
git clone tvm && checkout commit && cmake && make # 范式 3：编译整个框架
```

项目里**已存在的 `install_*.sh`**（通过 `ls **/install_*.sh` 确认）只在 **ada / ampere 的量化目录** 与 **cdna 的 aiter 目录**出现——Triton / BitBLAS / cuBLAS / TileLang 假定读者已装好（pip 可得或随 CUDA 提供），没有专门的 install 脚本。

#### 4.5.3 源码精读

三种范式各举一例（已在 4.2.3、4.3.3 引用过，这里汇总）：

- pip 直装：[ada_benchmark/dequant_matmul/3.bitsandbytes_benchmark/install_bitsandbytes.sh:3](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/ada_benchmark/dequant_matmul/3.bitsandbytes_benchmark/install_bitsandbytes.sh#L3)
- 源码可编辑安装：[ampere_benchmark/contiguous_dequant_matmul/1.marlin_benchmark/install_marlin.sh:2-4](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/ampere_benchmark/contiguous_dequant_matmul/1.marlin_benchmark/install_marlin.sh#L2-L4)（`git clone ... && cd && pip install -e .`）、[cdna_benchmark/mla_benchmark/0.aiter_benchmark/install_aiter.sh:4-5](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/mla_benchmark/0.aiter_benchmark/install_aiter.sh#L4-L5)（`cd aiter && python3 setup.py develop`）。
- 编译宿主框架：[ada_benchmark/dequant_matmul/2.cutlass_fpa_intb_benchmark/install_dependency.sh:2-14](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/ada_benchmark/dequant_matmul/2.cutlass_fpa_intb_benchmark/install_dependency.sh#L2-L14)（克隆 TVM + checkout + cmake + make）。

#### 4.5.4 代码实践

1. **目标**：盘点全项目所有 `install_*.sh` 及其范式。
2. **步骤**：在仓库根目录执行 `ls **/install_*.sh`，逐个打开判断属于哪种范式。
3. **观察**：会看到 Marlin/CUTLASS/bitsandbytes 各 ada 与 ampere 两份（内容基本一致）、aiter 一份。
4. **预期**：没有任何 install 脚本出现在 hopper_benchmark 下——hopper 的基线（cuBLAS/Triton/BitBLAS/TileLang）都假定已装好。

#### 4.5.5 小练习与答案

**练习**：如果一个新基线只能从源码编译安装，且依赖一个仍在快速演进的宿主框架，应当如何在 install 脚本里保证可复现？
**答案**：用 `git checkout <固定 commit>` 钉死宿主框架版本（如 CUTLASS fpa_intb 脚本对 TVM 的做法），并在脚本里写死 `CMAKE_CUDA_ARCHITECTURES` 等关键开关，避免默认值漂移。

## 5. 综合实践

**任务**：制作一张「基线 × 算子 × 架构」覆盖表，并标注 AMD 专用 / NVIDIA 专用基线。

### 操作步骤

1. 在仓库根目录执行下面这条命令，列出所有编号框架子目录：

   ```bash
   ls -d */*/*/ | grep -E '[0-9]+\.' | sort
   ```

2. 把输出整理成一张表，行为「基线」，列为「(架构, 算子)」，单元格标注编号目录（如 `0.`、`3.`）。

3. 参考答案（节选，按本讲实测的目录树）：

   | 基线 | 定位 | 出现位置（架构/算子） | 归属 |
   |------|------|------------------------|------|
   | cuBLAS | NVIDIA 厂商 GEMM | hopper/ada/ampere: dense、dequant、deepgemm、lowprecision、contiguous_dequant | NVIDIA 专用 |
   | Triton | 块级 DSL | 全架构: dense、dequant、flashattention、blocksparse、mla、gemm、mha | 跨架构 |
   | BitBLAS | 多精度 DSL | hopper/ada/ampere/cdna: dense、dequant、deepgemm、lowprecision、contiguous_dequant | 跨架构 |
   | Marlin | W4A16 专项 | ada/ampere/cdna: dequant、contiguous_dequant | 跨架构（注：cdna 版可能为移植） |
   | CUTLASS fpa_intb | TVM+CUTLASS 群组反量化 | ada/ampere: dequant、contiguous_dequant | NVIDIA 专用 |
   | bitsandbytes | NF4 量化 | ada/ampere: dequant | NVIDIA 专用 |
   | DeepGEMM(=BitBLAS fp8) | fp8 GEMM 对标 | hopper: deepgemm | NVIDIA 专用 |
   | aiter | AMD Attention/MLA | cdna: mla | **AMD 专用** |
   | CK | AMD C++ 模板库 | cdna: gemm | **AMD 专用** |
   | Ladder/Welder | 编译器 | cdna: gemm | **AMD 专用（本仓库内）** |
   | rocBLAS | AMD 厂商 GEMM | cdna: dequant（**目录为空，占位**） | **AMD 专用** |
   | FA3 (flash_attn) | Attention 专项库 | cdna: mha | 跨平台（本仓库仅 cdna） |
   | torch | 参考实现 | hopper/cdna: flashattention、dequant、blocksparse、gemm | 跨架构 |

4. **观察要点**：
   - **AMD 专用**：aiter、CK、Ladder/Welder、rocBLAS（占位）——只出现在 `cdna_benchmark/`。
   - **NVIDIA 专用**：cuBLAS、CUTLASS fpa_intb、bitsandbytes、DeepGEMM——只出现在 `ada/ampere/hopper_benchmark/`。
   - **跨架构**：Triton、BitBLAS、torch、TileLang（主角）、Marlin。
   - `dense_matmul` 仅在 NVIDIA 三架构出现；`conv_benchmark`、`mla_benchmark`、`mha_benchmark` 多见 CDNA——这与各算子的硬件相关性有关（见 u7-l24）。
   - rocBLAS 目录为空，是「计划中但未实现」的占位，不要误以为它有可运行脚本。

5. **预期结果**：得到一张可复现的覆盖表，能据此为任意算子选出合适的基线组合（厂商库 + 同类 DSL + 专项内核 + torch 参考）。

## 6. 本讲小结

- 项目里十余个对比基线可分四类：**厂商库**（cuBLAS/rocBLAS/CK/aiter）、**DSL/编译器**（Triton/BitBLAS/Welder-Ladder/TileLang）、**专项内核**（Marlin/CUTLASS fpa_intb/FA3/bitsandbytes）、**参考实现**（torch）。
- 「基线名 ≠ 库名」：`ladder` 实为 `welder`、`deepgemm` 实为 BitBLAS fp8、`rocblas` 目录为空占位——**读源码以 import 为准**。
- 依赖安装分三种范式：pip 直装（bitsandbytes）< 源码可编辑安装（Marlin/aiter）< 编译整个宿主框架（CUTLASS fpa_intb 编译 TVM，最重且钉死 commit）。
- 基线按架构分布：aiter/CK/Ladder/rocBLAS 为 **AMD 专用**；cuBLAS/CUTLASS/bitsandbytes/DeepGEMM 为 **NVIDIA 专用**；Triton/BitBLAS/torch/Marlin 跨架构。
- 各基线计时口径各异（cuBLAS µs、Triton/BitBLAS ms、CK 走 C++ 二进制），跨框架对比前必须统一单位与口径（承接 u2-l4 的单位陷阱）。
- 本项目 `install_*.sh` 只覆盖量化基线（ada/ampere）与 aiter（cdna），hopper 的常用基线假定读者已自行安装。

## 7. 下一步学习建议

- **u7-l24 跨架构适配**：本讲给出了基线「在哪些架构出现」，下一讲解释「为什么这么分布」——`CMAKE_CUDA_ARCHITECTURES`、`target="auto"` vs `target="hip"`、cuBLAS tensor core math mode，以及把同一 GEMM 内核迁移到 MI300X 要改哪些 target。
- **u7-l25 新增一个算子基准**：当你想新增算子时，本讲的覆盖表就是「该选哪些基线并列对比」的决策依据——下一讲会把目录约定、内核+shell 驱动、data/plot 管线、`benchmark.sh` 编排串成完整工程流程。
- 若想深挖单个基线内核：cuBLAS 见 u2-l5、Triton 见 u2-l6、BitBLAS/Marlin/CUTLASS/bitsandbytes 的量化对比见 u4-l15、FA3/Attention 见 u5-l16~u5-l19、aiter/CK 所在的 MLA 见 u6-l20。
