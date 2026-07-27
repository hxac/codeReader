# 跨架构适配

## 1. 本讲目标

本讲回答一个工程问题:**同一套算子,如何让它在 Ada(RTX 4090)、Ampere(A100)、Hopper(H100)、CDNA(MI300X)这四类完全不同的 GPU 上都能跑、并且跑得对?**

学完后你应当能够:

- 读懂 `CMakeLists.txt` 里 `CMAKE_CUDA_ARCHITECTURES` 与「按目标覆盖」两级架构设置,并能判断一个 `.cu` 程序**真正**编译给哪个架构。
- 区分两个常被混淆的概念:`target`(代码生成后端,`cuda`/`hip`/`auto`)与「宿主包」(独立 `tilelang` 还是 TVM 内置的 `tvm.tl`)。
- 看懂 cuBLAS 基线里 `cublasSetMathMode` + `CUBLAS_TENSOR_OP_MATH` 如何切换 Tensor Core,并理解这是 NVIDIA 专属旋钮。
- 根据算子在四大架构目录下的分布,为给定 GPU 选对目录、选对 target、选对基线 provider。

本讲承接 u7-l23(对比基线生态总览):那里讲的是「有哪些基线」,这里讲的是「同一个算子跨架构时要改哪些地方」。

## 2. 前置知识

在进入源码前,先建立四条直觉。

**直觉一:架构号是 NVIDIA 的「方言编号」。** NVIDIA 用 `sm_XX` 标记 GPU 代际,数字是计算能力(Compute Capability)的两位编码:

| 架构目录 | 代表卡 | 计算能力 | 编号 |
|----------|--------|----------|------|
| `ampere_benchmark` | A100 | 8.0 | **80** |
| `ada_benchmark` | RTX 4090 | 8.9 | **89** |
| `hopper_benchmark` | H100 | 9.0 | **90** |

AMD 的 CDNA(如 MI300X)不走这套编号,而用 `gfx` 代号(MI300X 为 `gfx942`),并且**根本不用 CUDA**,而用 HIP(可粗略理解为「AMD 版的 CUDA」)。所以「架构号」这个概念只对 NVIDIA 三代成立;一旦到了 CDNA,连编译器都换了。

**直觉二:同一个算子,源码可以「几乎不变」,但「接线」必须改。** TileLang 这类 DSL 的价值,正是把「数值怎么算」(算子语义)与「生成给谁」(`target`)解耦。改 `target` 就能把同一份 `T.gemm` 内核从 NVIDIA 搬到 AMD。但配套的基线(cuBLAS)、脚本环境变量(`CUDA_VISIBLE_DEVICES`)、目录命名都得跟着换。

**直觉三:`target` 选「后端」,「宿主包」选「框架」。** 这是本讲最容易踩的坑。CDNA 目录下你会看到两种写法:

- 独立 `tilelang` 包:`import tilelang`、`from tilelang.autotuner import autotune, jit`。
- TVM 内置的 `tvm.tl` 变体:`from tvm import tl`、`import tvm.tl.language as T`、`from tvm.tl.autotuner import *`。

**两者都可能写 `target="hip"`**——所以「`target="hip"`」本身**不能**告诉你用的是哪个宿主包。区分它们要看 import 路径、`profiler` 字段和返回值结构,这一点会在 4.2 详述。

**直觉四:基线 provider 按厂商分裂。** cuBLAS 是 NVIDIA 专属,CDNA 上没有;AMD 对应的是 rocBLAS / CK / aiter。所以「同一个算子的 0. 基线」在不同架构下是不同的库。这一点 u7-l23 已经铺过,本讲只点出它对「迁移」的影响。

## 3. 本讲源码地图

本讲涉及的关键文件,按「讲什么」分类:

| 文件 | 作用 |
|------|------|
| `hopper_benchmark/dense_matmul/0.cublas-benchmark/CMakeLists.txt` | cuBLAS 基线的构建脚本,演示两级 CUDA 架构设置 |
| `hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu` | cuBLAS 测试床,演示 `math mode` 与多精度 Tensor Core 路径(NVIDIA 专属) |
| `hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py` | 独立 `tilelang` 包的 GEMM 内核,`target="auto"`,含 Roller 的 `CUDA("cuda")` 架构假设 |
| `cdna_benchmark/conv_benchmark/benchmark_tilelang_conv.py` | TVM 内置 `tvm.tl` 变体的卷积内核,`target="hip"`、`profiler="tvm"`,作为 AMD 侧对照 |

辅助证据(正文中点名引用):各架构目录的 `CMakeLists.txt`、`cdna_benchmark/` 下若干 `.py` 的 import 与 target 用法、NVIDIA/AMD shell 脚本里的环境变量差异。

## 4. 核心概念与源码讲解

### 4.1 CMAKE_CUDA_ARCHITECTURES:两级架构设置与「以代码为准」

#### 4.1.1 概念说明

NVIDIA 的 nvcc 编译器需要知道「为哪一代 GPU 生成代码」。CMake 用 `CUDA_ARCHITECTURES` 目标属性来表达这一点,值就是上面直觉一里的编号(80/89/90)。这个属性有**两个层级**:

- **全局/项目级**:`CMAKE_CUDA_ARCHITECTURES` 变量,作为整个项目的默认值。
- **目标级**:用 `set_target_properties(<target> PROPERTIES CUDA_ARCHITECTURES "...")` 对单个可执行文件覆盖。

**关键规则:目标级覆盖优先于全局默认。** 当两者冲突时,最终生效的是目标级那条。这是本模块最容易读错的地方——只看 `CMAKE_CUDA_ARCHITECTURES` 那一行,往往会得出错误结论。

注意:AMD/CDNA 不参与这套体系。`CUDA_ARCHITECTURES`、`CMakeLists.txt`、`.cu` 文件、cuBLAS 这条「C++ 编译型基线」整条链路都是 NVIDIA 专属;CDNA 走 HIP 与 rocBLAS/CK,**没有 `0.cublas-benchmark` 子目录**(本讲后续会验证)。

#### 4.1.2 核心流程

一个 `.cu` 程序「真正编译给哪个架构」的判定流程:

```text
读 CMakeLists.txt
   │
   ├─ 全局默认:if(NOT DEFINED CMAKE_CUDA_ARCHITECTURES) set(... 89)   ← 仅当外部未指定时生效
   │
   └─ 目标级:set_target_properties(<exe> PROPERTIES CUDA_ARCHITECTURES "80")
            │
            ▼
   实际架构 = 目标级值(若存在),否则回落到全局默认
```

之所以要做两级,是因为全局默认提供「省事的安全值」,而目标级允许个别程序(比如要用某代专有指令的程序)单独指定。但当维护者只改了其中一处,就会出现「目录名」与「实际编译架构」不符的残留。

#### 4.1.3 源码精读

先看 hopper 下 dense_matmul 的 cuBLAS 基线 CMake。全局默认设在 89:

[CMakeLists.txt:L3-L5](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/CMakeLists.txt#L3-L5) —— 仅当外部未定义 `CMAKE_CUDA_ARCHITECTURES` 时,把全局默认设为 89(Ada)。

但紧接着,目标级把同一个可执行文件钉死在 80:

[CMakeLists.txt:L68-L69](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/CMakeLists.txt#L68-L69) —— `add_executable` 后立刻 `set_target_properties(... CUDA_ARCHITECTURES "80")`,这一行**覆盖**了上面的 89。

所以这个「hopper」目录下的 cuBLAS 二进制,真正编译给的是 **sm_80(Ampere)**,既不是目录名暗示的 90(Hopper),也不是全局默认的 89(Ada)。这是典型的历史遗留:同一套 `.cu`/`CMakeLists.txt` 在 ada/ampere/hopper 三处几乎一字不差地复制,维护者改了全局默认却没动目标级那一行(反之亦然)。

把四个架构的 CMake 横向对比,这条「两级不一致」的现象看得最清楚:

| 目录 | 全局默认 `CMAKE_CUDA_ARCHITECTURES` | 目标级 `CUDA_ARCHITECTURES` | **实际编译架构** |
|------|----------|----------|----------|
| `ampere/dense_matmul` | 80 | 80 | 80 ✓ |
| `ampere/dequant_matmul` | 80 | 80 | 80 ✓ |
| `ada/dense_matmul` | **89** | **80** | **80**(与目录名 ada/89 不符) |
| `ada/lowprecision_matmul` | 89 | 89 | 89 ✓ |
| `hopper/dense_matmul` | **89** | **80** | **80**(与目录名 hopper/90 不符) |
| `hopper/dequantize_matmul` | 80 | 80 | 80 |
| `hopper/deepgemm` | 90 | 90 | 90 ✓(唯一原生 Hopper) |

(上表数值由各 `CMakeLists.txt` 的 `set(CMAKE_CUDA_ARCHITECTURES ...)` 与 `set_target_properties(... CUDA_ARCHITECTURES ...)` 两行直接读出。)

值得注意的对比:

- **`hopper/deepgemm` 是全仓库唯一真正钉在 90 的**,因为 FP8/DeepGEMM 是 Hopper(sm_90)原生特性,降级到 80 就没有意义。
- **`hopper/dense_matmul` 钉在 80 能跑**,是因为 sm_90 GPU(H100)向后兼容 sm_80 代码——Hopper 能运行 Ampere 的二进制,只是用不上 Hopper 的新指令(如 TMA、FP8)。这正是维护者「没改也能跑」、于是残留没人修的原因。
- cuBLAS 测试床的注释里甚至写着「For Maxwell GPUS / For Pascal GPUS」(见 `cublas_benchmark.cu` 顶部的 Usage 注释),这套 `.cu` 模板从很早的架构沿用至今,进一步印证它是「复制粘贴」迁移来的。

> 原则(贯穿整本手册):**读源码以代码为准,不盲信目录名与注释。** 判断「真正编译给谁」永远看 `set_target_properties` 那一行,而不是目录名。

#### 4.1.4 代码实践

**实践目标:** 在本仓库里找出「目录名」与「实际编译架构」不符的全部 cuBLAS 基线。

**操作步骤:**

1. 用只读命令列出所有 cuBLAS 的 CMake(本讲已替你跑过,你可以复核):
   ```bash
   find . -path "*/0.cublas-benchmark/CMakeLists.txt" -not -path "./.git/*"
   ```
2. 对每个文件,分别读两行:全局默认行(`set(CMAKE_CUDA_ARCHITECTURES ...)`)与目标级行(`set_target_properties(... CUDA_ARCHITECTURES ...)`)。
3. 列出两行不一致的目录。

**需要观察的现象:** `ada/dense_matmul` 与 `hopper/dense_matmul` 两处,目录名暗示的架构(89、90)与目标级实际值(80)不符。

**预期结果:** 应得到与上表一致的结论——这两处的实际编译架构都是 80,与「Ada=89、Hopper=90」的目录命名相矛盾。这是一处可被修复(把目标级改成对应架构号)也可被解释(向后兼容所以没人修)的真实不一致。

**待本地验证:** 若你手头有 H100,把 `hopper/dense_matmul` 目标级改成 90 重新编译,观察 cuBLAS 在 fp16 Tensor Core 列的延迟是否变化(理论上 `CUBLAS_TENSOR_OP_MATH` 路径在 sm_90 下可能选到更优 kernel,但 cuBLAS 内部行为以厂商库为准)。

#### 4.1.5 小练习与答案

**练习 1.** 为什么 `if(NOT DEFINED CMAKE_CUDA_ARCHITECTURES)` 这个 `if` 守卫是必要的?如果直接写 `set(CMAKE_CUDA_ARCHITECTURES 89)` 会怎样?

**参考答案:** 守卫保证只有「外部(如命令行 `-DCMAKE_CUDA_ARCHITECTURES=...`、CI 或上层 CMake)没有指定」时才填默认值,从而允许外部覆盖。如果去掉守卫直接 `set`,就会无视外部传入,强制写死 89,失去可配置性。

**练习 2.** 假设你把 `hopper/dense_matmul` 的 `set_target_properties(... CUDA_ARCHITECTURES "80")` 整行删掉,这个二进制会编译给哪个架构?

**参考答案:** 会回落到全局默认 89(Ada)。但 H100 是 sm_90,sm_89 代码在 H100 上同样能跑(向后兼容),只是又选了一档并不精准的架构——可见这条目标级行就算「错」也是「能跑的错」。

---

### 4.2 target auto/hip:代码生成后端 vs 宿主包

#### 4.2.1 概念说明

TileLang 内核写好后,要经过一次「编译」落到具体 GPU 指令。这次编译由 `@jit` 的 `target=` 参数决定**代码生成后端**:

- `target="cuda"` —— 生成 NVIDIA CUDA / PTX。
- `target="hip"` —— 生成 AMD HIP(AMD 版 CUDA,底层是 GCN/RDNA/CDNA 指令)。
- `target="auto"` —— 运行时自动探测当前 GPU,是 NVIDIA 就走 cuda、是 AMD 就走 hip。

`target` 只回答「指令给谁」,不回答「调度谁来搜」。后者由**宿主包**决定,而本仓库里存在两个宿主包:

| 维度 | 独立 `tilelang` 包 | TVM 内置 `tvm.tl` 变体 |
|------|--------------------|------------------------|
| import | `import tilelang as tl`<br>`import tilelang.language as T`<br>`from tilelang.autotuner import autotune, jit` | `from tvm import tl`<br>`import tvm.tl.language as T`<br>`from tvm.tl.autotuner import *` |
| profiler 字段 | 省略(默认) | 显式 `profiler="tvm"` |
| 调优返回值 | `best_result` 对象(`.latency/.config/.ref_latency/.kernel`) | 三元组 `(best_latency, best_config, ref_latency)` |
| 典型出现位置 | hopper dense_matmul、cdna dequantize/mla/blocksparse | cdna conv、cdna mha 的 test 文件 |

**核心结论:`target="hip"` 不能用来区分这两种宿主包**——两个宿主包在 CDNA 上都写 `target="hip"`。区分要靠 import 路径、`profiler` 字段和返回值结构。这是读 CDNA 源码时最常被误判的一点。

#### 4.2.2 核心流程

判定一个 TileLang 内核「跑在哪、由谁调度」的两步判别:

```text
第一步:看 target → 判后端
   target="cuda"  → NVIDIA
   target="hip"   → AMD
   target="auto"  → 运行时探测(NVIDIA/AMD 皆可)

第二步:看 import + profiler + 返回值 → 判宿主包
   import tilelang.* / 无 profiler / 返回对象  → 独立包
   import tvm.tl.*  / profiler="tvm" / 返回三元组 → tvm.tl 变体
```

两步相互独立:`target` 选硬件后端,宿主包选调度框架。CDNA 上常见的组合是「`tvm.tl` + `target="hip"`」与「独立 `tilelang` + `target="hip"` 或 `target="auto"`」并存。

#### 4.2.3 源码精读

**先看 NVIDIA 侧(独立 tilelang 包 + auto)。** hopper 的 dense matmul 内核:

[benchmark_tilelang_matmul.py:L147-L151](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L147-L151) —— `@jit(out_idx=[2], supply_type=..., target="auto")`,用独立 `tilelang` 包,`target="auto"` 让运行时探测(在 H100 上自动走 cuda)。

注意它的文件头 import 是独立包形态:

[benchmark_tilelang_matmul.py:L8-L10](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L8-L10) —— `import tilelang as tl` / `import tilelang.language as T` / `from tilelang.autotuner import autotune, jit`,这是独立包的典型写法。

**再看 AMD 侧(tvm.tl 变体 + hip)。** CDNA 的卷积内核:

[benchmark_tilelang_conv.py:L1-L7](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/conv_benchmark/benchmark_tilelang_conv.py#L1-L7) —— `from tvm import tl` / `import tvm.tl.language as T` / `from tvm.tl.autotuner import *`,这是 TVM 内置 `tvm.tl` 变体的典型 import。

它的 `@jit` 显式同时给出两个关键参数:

[benchmark_tilelang_conv.py:L47](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/conv_benchmark/benchmark_tilelang_conv.py#L47) —— `@jit(..., profiler="tvm", target="hip")`。`profiler="tvm"` 表明用的是 TVM 内置评估器(对应 `tvm.tl` 宿主),`target="hip"` 表明代码生成给 AMD。

**关键对比:同一个 CDNA 仓库里两种写法并存。** 用 grep 跨 `cdna_benchmark/` 看,这一点非常直观(以下为源码事实):

- 独立 `tilelang` 包 + `target="auto"`:`cdna_benchmark/mla_benchmark/.../benchmark_mla_decode_amd_tilelang.py`、`cdna_benchmark/blocksparse_attention/1.tilelang-benchmark/benchmark_tilelang_bsa.py`、`cdna_benchmark/dequantize_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul_fp16xint4.py`。
- 独立 `tilelang` 包 + `target="hip"`:`cdna_benchmark/gemm_benchmark/1.tilelang_benchmark/benchmark_tilelang_matmul.py`、`cdna_benchmark/mha_benchmark/benchmark_tilelang_mha.py`。
- `tvm.tl` 变体 + `profiler="tvm"` + `target="hip"`:`cdna_benchmark/conv_benchmark/benchmark_tilelang_conv.py`、`cdna_benchmark/mha_benchmark/test_tilelang_mha.py`。

可以看到:`target="auto"` 与 `target="hip"` 在同一块 MI300X 上都出现——`auto` 是「让运行时探测」,`hip` 是「显式声明」。两者都有效,选哪个更多是作者习惯。**但 `tvm.tl` 变体一律带 `profiler="tvm"`**,这是它最稳的识别标志。

> 再次强调:**「target 选后端、宿主包选框架」是两个独立维度。** 不要看到 `target="hip"` 就断定用的是 `tvm.tl`,也不要看到独立 `tilelang` 就断定它在 NVIDIA 上。

#### 4.2.4 代码实践

**实践目标:** 训练「用 import + profiler 判宿主包,用 target 判后端」的双维判别。

**操作步骤:**

1. 打开 `cdna_benchmark/gemm_benchmark/1.tilelang_benchmark/benchmark_tilelang_matmul.py`,读它的 import 与 `@jit` 行。
2. 打开 `cdna_benchmark/conv_benchmark/benchmark_tilelang_conv.py`,读它的 import 与 `@jit` 行(本讲已给出 L1-L7 与 L47)。
3. 做一张两行小表:每个文件的「宿主包」「profiler」「target」「返回值形态」。

**需要观察的现象:** 这两个文件 `target` 都是 `hip`(都在 MI300X 上),但一个用独立 `tilelang`、一个用 `tvm.tl`;两者的 `profiler` 字段(有/无 `tvm`)与返回值(对象/三元组)不同。

**预期结果:** 你会得出与 4.2.1 表格一致的结论——`target` 相同不等于宿主包相同。这正是初学者最易误判处。

#### 4.2.5 小练习与答案

**练习 1.** 某个 CDNA 内核文件里写了 `target="auto"`。仅凭这一行,你能判断它用的是独立 `tilelang` 还是 `tvm.tl` 吗?

**参考答案:** 不能。`target` 只决定后端(cuda/hip/auto),与宿主包无关。仓库里独立 `tilelang` 用 `target="auto"`(如 mla、blocksparse),`tvm.tl` 一律用 `target="hip"` 但那也不是 `target` 决定的——要靠 import 与 `profiler` 判断。

**练习 2.** 为什么 `target="auto"` 在跨架构迁移时「省事但有风险」?

**参考答案:** 省事在于一份代码不改 target 就能在 NVIDIA 与 AMD 上都跑(运行时探测)。风险在于:探测结果依赖运行环境,在 CI、容器或无 GPU 的机器上行为不可预期;显式写 `target="cuda"`/`"hip"` 更利于复现与排错。生产基准里更推荐显式 target。

---

### 4.3 cuBLAS Tensor Core math mode(NVIDIA 专属旋钮)

#### 4.3.1 概念说明

cuBLAS 基线是本项目「0.」参考基线,它用 NVIDIA 提供的 `cublasGemmEx` 跑多精度 GEMM。其中**是否启用 Tensor Core** 由两个旋钮联合控制:

- `cublasSetMathMode(handle, <mode>)`:设置 handle 的「数学模式」。
  - `CUBLAS_DEFAULT_MATH`:不允许降低精度,不主动用 Tensor Core 的非标准路径。
  - `CUBLAS_TENSOR_OP_MATH`:允许库在合适时用 Tensor Core(IMMA/HMMA)做 GEMM。
- `algo` 参数:`CUBLAS_GEMM_DFALT`(普通) vs `CUBLAS_GEMM_DFALT_TENSOR_OP`(优先 Tensor Core)。

这两个旋钮是**纯 NVIDIA 专属**的——它们来自 `cublas_v2.h`,在 AMD 上根本不存在。AMD 对应的能力由 rocBLAS/MIOpen/CK 各自的 API 提供(如 rocBLAS 的 `rocblas_gemm_ex` + `rocblas_gemm_algo`)。**这也是为什么 CDNA 目录下没有 `0.cublas-benchmark`**——本模块末尾会用源码验证这一点。

「Tensor Core」本身是硬件概念:Volta 起的 NVIDIA 卡有专门的矩阵乘加速单元(IMMA 做 int8,HMMA 做 fp16/bf16)。不同代际的 Tensor Core 指令形状与峰值不同(sm_80 的 HMMA 是 16×8×16,sm_90 还多了 FP8/TMA)。但 cuBLAS 把这些差异藏在 `math mode` 与 `algo` 后面,用户只需声明「我要不要用 Tensor Core」。

#### 4.3.2 核心流程

cuBLAS 测试床对每个 `(m,n,k)` 形状跑 5 列计时,流程是「先关 Tensor Core 跑 3 列,再开 Tensor Core 跑 2 列」:

```text
对每个 shape (m,n,k,a_t,b_t):
   cublasSetMathMode(CUBLAS_DEFAULT_MATH)        # 关 Tensor Core
      ├─ fp32  : time_gemm<float,float>(use_tensor_core=false)
      ├─ fp16  : time_gemm<uint16_t,uint16_t>(false)
      └─ int8  : time_gemm<uint8_t,int>(false)
   cublasSetMathMode(CUBLAS_TENSOR_OP_MATH)      # 开 Tensor Core
      ├─ fp16 TC: time_gemm<half,half>(use_tensor_core=true)
      └─ int8 TC: time_gemm<uint8_t,int>(true)
   输出一行 CSV:m,n,k,a_t,b_t, fp32, fp16, int8, fp16-TC, int8-TC
```

`time_gemm` 内部按输入 dtype 模板类型 `T1` 在**编译期**选定 `A_type/B_type/C_type/compute_type`,再用 `algo` 决定是否走 Tensor Core,最后 `cublasGemmEx` 一次性派发。

#### 4.3.3 源码精读

先看「algo 选 Tensor Core」这一行,它是 per-call 的旋钮:

[cublas_benchmark.cu:L107](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L107) —— `algo = use_tensor_core ? CUBLAS_GEMM_DFALT_TENSOR_OP : CUBLAS_GEMM_DFALT;`,根据传入的 `use_tensor_core` 布尔位选 algo。

再看 dtype → compute_type 的编译期分派(这是多精度路由的核心):

[cublas_benchmark.cu:L85-L105](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L85-L105) —— fp32 → `CUDA_R_32F` 全 32 位;fp16 → 输入 `CUDA_R_16F` 但 compute 也是 `CUDA_R_16F`;int8 → 输入 `CUDA_R_8I`、输出 `CUDA_R_32I`、compute `CUDA_R_32I`(即「窄输入、宽累加」,与 u4-l12 讲的混合精度约定一致)。

然后是 `main` 里两组 `cublasSetMathMode` 的实际调用:

[cublas_benchmark.cu:L224](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L224) —— 跑 fp32/fp16/int8 三列「非 Tensor Core」前,先 `cublasSetMathMode(handle, CUBLAS_DEFAULT_MATH)`。

[cublas_benchmark.cu:L269](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L269) —— 跑 fp16/int8 两列「Tensor Core」前,改成 `cublasSetMathMode(handle, CUBLAS_TENSOR_OP_MATH)`,并传 `use_tensor_core=true` 让 `time_gemm` 选 `CUBLAS_GEMM_DFALT_TENSOR_OP`。

最后验证「cuBLAS 是 NVIDIA 专属」对 CDNA 的影响——AMD 目录下根本没有 cuBLAS 基线(本讲已替你执行 `find cdna_benchmark -name "*cublas*"` 返回空)。CDNA 的 GEMM 基线 provider 是另一套:

- `cdna_benchmark/gemm_benchmark/` 下是 `0.torch_benchmark`(参考)、`1.tilelang_benchmark`、`2.triton_benchmark`、`3.ck_benchmark`(AMD 厂商库 CK)、`4.ladder_benchmark`,**没有任何 cuBLAS**。

也就是说,跨架构迁移时,「0. 基线」这个槽位在 NVIDIA 下是 cuBLAS、在 AMD 下要换成 CK 或 rocBLAS——这是 provider 层面的 unavoidable 改动。

> 小结:`math mode` + `algo` 是 cuBLAS 里「要不要用 Tensor Core」的两个旋钮,纯 NVIDIA 专属;AMD 由 CK/rocBLAS 自己的 API 承担同等角色。这构成了「跨架构时基线必换」的根因。

#### 4.3.4 代码实践

**实践目标:** 在 cuBLAS 测试床中定位「Tensor Core 开关」的两个旋钮,并理解它们如何配合。

**操作步骤:**

1. 打开 `hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu`。
2. 找到 L107 的 `algo = use_tensor_core ? ...`,确认它是 per-call 旋钮。
3. 找到 L224 与 L269 的两次 `cublasSetMathMode`,确认它们是 per-handle、分两段设置。
4. 跟踪一次完整调用:`main` 里 fp16 Tensor Core 那段(L269 之后)如何把 `use_tensor_core=true` 传进 `time_gemm`(L280-281),再由 `time_gemm` 在 L107 选 algo。

**需要观察的现象:** `math mode` 是在 handle 上设的(对后续所有 `cublasGemmEx` 生效),而 `algo` 是每次 `cublasGemmEx` 调用的入参;两者必须**同时**指向 Tensor Core,才会真正走 HMMA。

**预期结果:** 你会清楚地看到「Tensor Core 列」需要 `CUBLAS_TENSOR_OP_MATH` + `CUBLAS_GEMM_DFALT_TENSOR_OP` 两个条件齐备;缺任何一个都退回非 TC 路径。

**待本地验证:** 若你手头有 A100/Ada/H100,可对比同一 shape 下「fp16 非 TC 列」与「fp16 TC 列」的延迟,观察 Tensor Core 带来的加速比随代际的变化。

#### 4.3.5 小练习与答案

**练习 1.** 如果只把 `algo` 改成 `CUBLAS_GEMM_DFALT_TENSOR_OP`、却忘了把 `math mode` 设成 `CUBLAS_TENSOR_OP_MATH`,会发生什么?

**参考答案:** cuBLAS 仍会按 algo 尝试选用 Tensor Op 内核,但在 `CUBLAS_DEFAULT_MATH` 下库对精度更保守,可能拒绝某些 TC 路径或回退到非 TC 内核,结果不稳定或达不到峰值。两个旋钮必须一致地指向 TC,行为才符合预期。

**练习 2.** 为什么 CDNA 的 GEMM 基线里看不到 `cublasSetMathMode` 这种调用?

**参考答案:** cuBLAS 与 `cublas_v2.h` 是 NVIDIA 专属,MI300X 上无法链接。AMD 用 rocBLAS/CK,API 完全不同(如 `rocblas_gemm_ex`),不存在 `CUBLAS_TENSOR_OP_MATH` 这个符号。等价的「要不要用 Matrix Core(MFMA)」由 AMD 库各自的参数表达。

---

### 4.4 算子的架构分布:为什么 dense_matmul 只在 NVIDIA、conv/MLA 只在 CDNA?

#### 4.4.1 概念说明

本项目按架构分顶楼目录(`ada_/ampere_/hopper_/cdna_benchmark/`),但**每个架构下的算子集合并不相同**。先看全貌(由各顶楼目录的 `ls` 直接读出):

| 算子 | ada(89) | ampere(80) | hopper(90) | cdna(gfx942) |
|------|:---:|:---:|:---:|:---:|
| dense GEMM | `dense_matmul` | `dense_matmul` | `dense_matmul` | `gemm_benchmark`(改名) |
| dequant GEMM/GEMV | `dequant_matmul` | `dequant_matmul` | `dequantize_matmul` | `dequantize_matmul` |
| contiguous dequant | ✓ | ✓ | — | — |
| lowprecision(int8) | ✓ | — | — | — |
| **卷积 conv** | — | — | — | ✓ `conv_benchmark` |
| attention(MHA/FA) | — | — | `flashattention` | `mha_benchmark` |
| blocksparse attn | — | — | ✓ | ✓ |
| deepgemm(FP8) | — | — | ✓ | — |
| **MLA(DeepSeek)** | — | — | — | ✓ `mla_benchmark` |

注意「dense GEMM」一行:在 NVIDIA 三代里都叫 `dense_matmul`,但在 CDNA 里改叫 `gemm_benchmark`。所以 topic 里说的「dense matmul 仅 NVIDIA」精确含义是:**以 `dense_matmul` 这个目录名组织的、含 `0.cublas-benchmark` 的那套基准,只在 NVIDIA 三代出现**;AMD 上同一算子换了个目录名与一套 provider。

#### 4.4.2 核心流程

算子按架构分布不均,原因有三层,按重要性排序:

```text
原因一:厂商库绑定(硬约束)
   cuBLAS 只在 NVIDIA → 凡是把 cuBLAS 当 0. 基线的算子(dense_matmul)
                        自然只在 NVIDIA 三代出现
   CK/rocBLAS 只在 AMD  → 凡是把 CK 当基线的(conv)只在 CDNA 出现

原因二:硬件特性绑定(半硬约束)
   FP8/DeepGEMM 是 Hopper(sm_90)原生 → deepgemm 只在 hopper
   MLA(FlashMLA)在本仓库用的是 AMD 实现 → mla 只在 cdna

原因三:项目采样焦点(软约束)
   卷积在所有 GPU 上都能算,但本仓库只在 CDNA 评测 → 反映「这批 benchmark
   是为 MI300X 场景做的」,不代表硬件限制
```

换言之:**分布不均 ≠ 硬件不能算**。卷积在 A100 上当然能跑,只是这个仓库没测;理解这一点能避免「看到某算子只在某架构就以为硬件限制」的误判。

#### 4.4.3 源码精读

**证据一:dense GEMM 在 NVIDIA 三代都有 `0.cublas-benchmark`,在 CDNA 改名且无 cuBLAS。**

- NVIDIA 三代(均含 cuBLAS 基线):
  - `ada_benchmark/dense_matmul/0.cublas-benchmark/`
  - `ampere_benchmark/dense_matmul/0.cublas-benchmark/`
  - `hopper_benchmark/dense_matmul/0.cublas-benchmark/`
- CDNA:`cdna_benchmark/gemm_benchmark/` 下是 `0.torch_benchmark / 1.tilelang / 2.triton / 3.ck / 4.ladder`,**没有 cublas**,目录名也从 `dense_matmul` 改成 `gemm_benchmark`。

**证据二:conv 只在 CDNA,且其 shell 用 AMD 专属环境变量。**

CDNA 卷积内核是 `tvm.tl` 变体(见 4.2.3)。它的脚本用 `HIP_VISIBLE_DEVICES` 选卡:

`cdna_benchmark/conv_benchmark/benchmark_ladder.sh` 顶部 `export HIP_VISIBLE_DEVICES=0`(`benchmark_tilelang.sh`、`gemm_benchmark/4.ladder_benchmark/benchmark_ladder.sh` 等所有 CDNA 脚本同理)。对照 NVIDIA 侧 `hopper_benchmark/flashattention/0.torch_benchmark/benchmark_torch.sh` 用的是 `CUDA_VISIBLE_DEVICES`。环境变量的分裂同样是「跨架构必改」的一环。

**证据三:deepgemm 只在 hopper(90)。**

`hopper_benchmark/deepgemm/0.cublas-benchmark/CMakeLists.txt` 是全仓库唯一一对「全局默认 90 + 目标级 90」的 CMake(见 4.1.3 表格)。FP8 GEMM 是 Hopper 原生,这是「硬件特性绑定」的典型——降级到 80 就失去意义,所以它老老实实钉在 90,不像 dense_matmul 那样残留成 80。

**证据四:MLA 只在 cdna。**

`cdna_benchmark/mla_benchmark/1.tilelang_benchmark/benchmark_mla_decode_amd_tilelang.py`(u6-l20 详述)是 FlashMLA 的 AMD 实现。它用的是独立 `tilelang` 包 + `target="auto"`(见 4.2.3 grep 结果),与硬件特性 + 项目采样焦点双重相关。

> 小结:算子的架构分布由「厂商库绑定(硬)」「硬件特性(半硬)」「项目采样(软)」三层共同决定。读目录时要分清某算子「不在某架构」到底是不能跑、还是没测。

#### 4.4.4 代码实践

**实践目标:** 用源码事实构建一张「算子 × 架构 × provider」覆盖表,识别每类 provider 的厂商归属。

**操作步骤:**

1. 对四个顶楼目录分别 `ls`(本讲已替你执行),得到上表。
2. 在 `cdna_benchmark/gemm_benchmark/` 下 `ls`,确认其 provider 是 `torch/tilelang/triton/ck/ladder`,无 cuBLAS。
3. 用 grep 对照 NVIDIA 侧 dense_matmul 的 provider:
   ```bash
   ls hopper_benchmark/dense_matmul/   # 0.cublas 1.triton 2.bitblas 3.tilelang
   ls ampere_benchmark/dense_matmul/   # 0.cublas 1.triton 2.bitblas (无 tilelang 子目录)
   ls ada_benchmark/dense_matmul/      # 0.cublas 1.triton 2.tilelang (无 bitblas)
   ```
4. 标注每个 provider 的厂商归属:cuBLAS=NVIDIA、CK=AMD、aiter=AMD、ladder(实为 welder)=AMD、bitblas/triton/tilelang/torch=跨架构。

**需要观察的现象:** 即便是同一个「dense GEMM」算子,NVIDIA 与 CDNA 的 provider 列表也几乎不重叠(只有 tilelang/triton/torch 共有);cuBLAS 与 CK 完全互斥。

**预期结果:** 你应得到结论——跨架构迁移一个算子时,provider 列表必然要改:NVIDIA 的 `0.cublas` 在 AMD 上必须换成 `3.ck` 或 rocBLAS。这与 4.3 的「math mode 是 NVIDIA 专属」互为表里。

**待本地验证:** 若你有 MI300X,可尝试在 `cdna_benchmark/gemm_benchmark/` 下跑 `1.tilelang_benchmark`,对照其在 H100 上 `hopper/dense_matmul/3.tilelang-benchmark` 的 TFlops,观察「同一 tilelang 内核、不同 target」的性能差。

#### 4.4.5 小练习与答案

**练习 1.** 「卷积只在 CDNA 出现」是否说明 A100/H100 算不了卷积?

**参考答案:** 不是。卷积在任何现代 GPU 上都能算。本仓库只在 CDNA 测卷积,是「项目采样焦点」(为 MI300X 场景做这批基准)的软约束,不是硬件限制。

**练习 2.** 给定一个新算子,你想同时覆盖 NVIDIA 与 AMD,provider 列表至少要各放谁作为「厂商库基线」?

**参考答案:** NVIDIA 侧放 cuBLAS(若算子是 GEMM 类)或对应的 cuDNN;AMD 侧放 CK 或 rocBLAS。两者都不会跨厂商运行,所以必须分别维护。tilelang/triton/torch 可作为两边共有的对照。

---

## 5. 综合实践

**任务:把 hopper 的 dense_matmul TileLang 内核「搬到」MI300X,并列出所有必须修改的接线点。**

背景:`hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py` 是一份用独立 `tilelang` 包、`target="auto"`、Roller(`CUDA("cuda")`) 的 int8 GEMM 内核。现在要让它跑在 CDNA 的 MI300X 上,并纳入 `cdna_benchmark/` 的对比体系。

请按下列清单逐项给出改动(本实践为「源码阅读 + 设计」型,不要求真在 MI300X 上运行):

1. **target**:`@jit` 的 `target="auto"` 是否需要改?给出「保留 auto」与「显式改 hip」两种方案的取舍。(提示:参考 `cdna_benchmark/gemm_benchmark/1.tilelang_benchmark/benchmark_tilelang_matmul.py`,它用独立 `tilelang` + `target="hip"`。)

2. **Roller 架构**:`get_configs` 里 `from tilelang.carver.arch import CUDA; arch = CUDA("cuda")`([L34-L36](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L34-L36))是 NVIDIA 专属假设。迁移时如何处理?(至少给出「换 AMD 架构」与「退回 `with_roller=False` 暴搜」两条路,后者参考 CDNA 卷积 `benchmark_tilelang_conv.py` 的 `itertools.product` 写法。)

3. **dtype**:int8 在 MI300X 上走 MFMA,内核结构要不要改?为什么?(提示:这正是 DSL「结构与精度解耦」的价值。)

4. **基线 provider**:原 `0.cublas-benchmark` 在 AMD 上无法编译。要换成 CDNA 的哪个 provider?放进哪个目录?(提示:CDNA 的 dense GEMM 目录叫 `gemm_benchmark`,基线是 CK。)

5. **shell 环境变量**:把驱动脚本里的 `CUDA_VISIBLE_DEVICES` 改成什么?

6. **目录落点**:新文件应放在 `cdna_benchmark/` 下的哪个子目录?沿用哪个编号约定?

**参考要点(自我核对):**

1. `target` 可保留 `auto`(运行时探测为 hip),也可显式改 `target="hip"` 以利复现——后者更稳。本仓库 CDNA 同款内核(`gemm_benchmark/1.tilelang`)用的是显式 `hip`。
2. Roller 的 `CUDA("cuda")` 不能直接用于 AMD;要么换成 AMD/CDNA 的架构描述(具体类名**待确认**,需查当前 `tilelang.carver.arch` 是否提供 AMD arch),要么直接用 `with_roller=False` 走 `itertools.product` 暴搜(与 CDNA 卷积一致)。
3. int8 内核结构**无需改**:MI300X 支持 INT8 MFMA,`T.gemm` 配合 `dtype="int8"` + `target="hip"` 会自动落到 MFMA。这是 DSL 把「算什么」与「给谁算」解耦的直接收益。
4. cuBLAS 换成 CK(`3.ck_benchmark`),或 rocBLAS;目录从 `dense_matmul/` 改为 `gemm_benchmark/`。
5. `CUDA_VISIBLE_DEVICES` → `HIP_VISIBLE_DEVICES`。
6. 落点:`cdna_benchmark/gemm_benchmark/1.tilelang_benchmark/`(若已存在则并入),遵循 CDNA「`gemm_benchmark` 而非 `dense_matmul`」的命名。

> 通过这个综合实践你会看到:跨架构迁移时,**TileLang 内核本身几乎不动**(得益于 DSL),真正要改的是 target、Roller 架构假设、基线 provider、shell 环境变量、目录命名这五处「接线」。这也解释了为什么本项目要按架构分顶楼目录——把「能共用」与「必须分架构」的部分清晰隔开。

## 6. 本讲小结

- **两级架构设置**:cuBLAS 基线里 `CMAKE_CUDA_ARCHITECTURES`(全局默认)与 `set_target_properties(... CUDA_ARCHITECTURES ...)`(目标级)可能冲突,**目标级优先**;`hopper/dense_matmul` 目录名是 hopper(90)但实际编译给 sm_80,是典型的复制粘贴残留。
- **target vs 宿主包**:`target`(cuda/hip/auto)决定代码生成后端,「独立 `tilelang` vs `tvm.tl` 变体」决定宿主框架,两者独立;CDNA 上两种宿主包都可能写 `target="hip"`,要靠 import + `profiler` + 返回值区分。
- **math mode 是 NVIDIA 专属**:`CUBLAS_TENSOR_OP_MATH` + `CUBLAS_GEMM_DFALT_TENSOR_OP` 联合开启 Tensor Core;AMD 由 CK/rocBLAS 各自 API 承担,因此 CDNA 无 cuBLAS。
- **算子分布三层成因**:厂商库绑定(硬)、硬件特性(半硬,如 deepgemm 钉 90)、项目采样(软,如 conv 只测 CDNA);「不在某架构」≠「不能跑」。
- **跨架构迁移改五处**:target、Roller 架构假设、基线 provider(cuBLAS↔CK)、shell 环境变量(CUDA↔HIP)、目录命名(dense_matmul↔gemm_benchmark);TileLang 内核本体几乎不动。
- 一以贯之的原则:**读源码以代码为准**,目录名、注释、`target` 字面值都可能误导,真实行为只看对应的代码行。

## 7. 下一步学习建议

- **u7-l25 二次开发——新增一个算子基准**:本讲讲了「跨架构迁移」,下一篇讲「从零新增」,会把目录约定、`benchmark.sh` 编排、data/plot 管线串起来,正好承接本讲对 provider 与目录命名的讨论。
- **纵向深读**:想看清「target=hip 之后底层生成什么」,可读 `cdna_benchmark/conv_benchmark/benchmark_tilelang_conv.py`(u6-l21)与 `cdna_benchmark/gemm_benchmark/1.tilelang_benchmark/benchmark_tilelang_matmul.py`,对照 hopper 同名内核体会「同源内核、不同 target」。
- **基线生态复习**:若对「CK/aiter/rocBLAS 谁是 AMD 专属、cuBLAS/CUTLASS 谁是 NVIDIA 专属」还不够熟,回看 u7-l23 的 provider 厂商归属表,再回到本讲的 4.4 分布表相互印证。
