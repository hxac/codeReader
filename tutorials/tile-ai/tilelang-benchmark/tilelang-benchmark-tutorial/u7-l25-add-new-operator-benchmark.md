# 二次开发——新增一个算子基准

## 1. 本讲目标

本讲是整本学习手册的收尾篇。前面 24 讲我们把项目里现有的算子（GEMM、反量化、FlashAttention、MLA、卷积……）逐个拆解过，本讲反过来回答一个工程问题：

> **「我想给这个仓库新增一个算子（比如 RMSNorm 或 batched GEMM）的基准，应该建哪些目录、写哪些文件、按什么顺序编排、怎么接上现成的可视化管线？」**

学完本讲你应该能够：

1. 说出 tilelang-benchmark 一个完整算子目录的「标准件清单」（编号框架子目录、内核 `.py`、驱动 `.sh`、`data/`、`plot`/`extract` 脚本、`benchmark.sh`）。
2. 复用现有 cuBLAS/Triton 基线与「日志→数据→图表」可视化管线，而不是从零造轮子。
3. 理解目录命名约定，并能在动手前**识别历史遗留的命名不一致风险**，避免被脚本字面量误导。
4. 为一个新算子独立设计出目录结构、文件清单与 `benchmark.sh` 编排顺序。

本讲的立场是**工程约定 + 源码验证**：凡涉及「约定」的地方，我们都回到真实源码去核对，因为本仓库的约定并不总是被严格遵守（这是读这个项目最重要的意识，贯穿 u1-l3、u2-l7、u7-l24）。

## 2. 前置知识

本讲默认你已经学完前 24 讲，尤其依赖以下概念（不重复展开，只点出与本讲相关的部分）：

- **provider（实现/提供商）**：同一算子在同一架构下并列对比的多种实现（cuBLAS、Triton、BitBLAS、TileLang、Marlin……）。见 u7-l23。
- **三层目录**：架构目录（`hopper_benchmark/` 等）→ 算子目录（`dense_matmul/`）→ 编号框架子目录（`N.<framework>-benchmark/`）。见 u1-l2。
- **「日志→数据→图表」管线**：每个 provider 跑完写日志 → `data/*.py` 用正则抽 latency → `plot_*.py` 画 speedup 柱状图。见 u2-l7。
- **单位陷阱**：cuBLAS 输出 µs、Triton/BitBLAS 输出 ms、TileLang 打印标签写 `(s)` 实为 ms。跨框架对比前必须统一。见 u2-l4、u2-l6。
- **以代码为准**：本仓库多处注释/目录名/脚本字面量与真实代码不符，凡要复用都要 `ls` 与 `Read` 验证。见 u1-l3。

如果你对 TileLang 内核本身（`@autotune`/`@jit`/`@T.prim_func`）还不熟，本讲的「内核」一节只把它当作一个**接受 `--m/--n/--k` 命令行参数、吐出 `Best latency` 日志的黑盒**来对待，内核内部细节请回到 u3-l8～u3-l11。

## 3. 本讲源码地图

本讲以 `hopper_benchmark/dense_matmul/` 为「参考样板」算子，因为它**结构最完整**：既有顶层编排 `benchmark.sh`，又有齐备的 provider 子目录与 `data/`/`plot` 管线。我们逐层拆它，再对照其它算子指出差异。

| 文件 | 角色 | 本讲用来讲 |
| --- | --- | --- |
| `hopper_benchmark/dense_matmul/benchmark.sh` | 顶层编排脚本，按编号顺序串联各 provider | 4.4 编排 |
| `hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py` | TileLang 内核 + `__main__` 命令行入口 | 4.2 内核 |
| `hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.sh` | TileLang 驱动 shell：遍历 shape、`tee` 日志 | 4.2 shell 驱动 |
| `hopper_benchmark/dense_matmul/1.triton-benchmark/benchmark_float16.sh` | Triton 驱动 shell（对照组，命名有坑） | 4.2 shell 驱动 |
| `hopper_benchmark/dense_matmul/3.tilelang-benchmark/extract_benchmark_results.py` | 单 provider 内部抽 latency 的辅助脚本 | 4.3 管线 |
| `hopper_benchmark/dense_matmul/data/data_float16_gemm.py` | 把多 provider 日志汇总成 `(provider, [times])` 数据 | 4.3 管线（核心契约） |
| `hopper_benchmark/dense_matmul/plot_operator_figures_fp16_gemm.py` | 读 `data` 模块、算 speedup、画柱状图 | 4.3 管线 |
| `hopper_benchmark/dense_matmul/plot.sh` | 顺序调用多个 `plot_*.py` | 4.3 管线 |

样板之外，本讲还会**对照**三个算子的目录布局来揭示命名约定与不一致：`dense_matmul`、`flashattention`、`dequantize_matmul`（均位于 `hopper_benchmark/` 下）。

## 4. 核心概念与源码讲解

### 4.1 目录约定

#### 4.1.1 概念说明

新增算子的第一步不是写内核，而是**把目录建对**。本仓库的物理布局是三层：

```
<架构>_benchmark/          例如 hopper_benchmark/
└── <算子>/                例如 dense_matmul/
    ├── benchmark.sh        顶层编排（可选，多数算子其实没有）
    ├── plot.sh             顺序调用各 plot_*.py（可选）
    ├── plot_*.py           每个 dtype/工况一张图
    ├── data/               每个 dtype/工况一个 data_*.py，汇总各 provider latency
    ├── pdf/  png/          图表产物（git 跟踪）
    ├── 0.<baseline>/       参考基线（cuBLAS 或 torch），编号决定运行顺序
    ├── 1.<framework>/      第一个对比框架
    ├── 2.<framework>/      ……
    └── N.tilelang[_-]benchmark/   TileLang 主角（编号不固定）
```

为什么要先按架构再按算子切？因为**同一算子在不同 GPU 上性能不可直接比较**（A100 上的 TFlops 搬不到 MI300X），所以架构是第一层、算子是第二层。编号框架子目录的**编号**有两个含义：①决定 `benchmark.sh` 的运行顺序；②`0.` 约定俗成是**参考基线**（标尺）。

#### 4.1.2 核心流程

给新算子选目录位置的决策树：

1. **选架构**：目标 GPU 是哪张卡？→ 进对应的 `*_benchmark/` 目录。NVIDIA 卡有 `ada_/ampere_/hopper_`，AMD 卡有 `cdna_`（见 u7-l24）。
2. **建算子目录**：用算子英文小写名，如 `rmsnorm`、`batched_matmul`。多数不带后缀；`cdna_` 下部分带 `_benchmark` 后缀（命名不统一）。
3. **建编号框架子目录**：每个 provider 一个，命名形如 `N.<framework>{-,_}benchmark`。`0.` 留给参考基线（NVIDIA 通常 cuBLAS，没有 cuBLAS 时用 torch eager）。
4. **建辅助目录与脚本**：`data/`、`pdf/`、`png/`、`plot_*.py`、（可选）`benchmark.sh`、`plot.sh`。

#### 4.1.3 源码精读：三个算子的真实布局对照

我们用 `ls` 看三个算子目录的真实结构（这是最可靠的「约定」证据）。

**样板 `dense_matmul`（结构最完整）**：

```
hopper_benchmark/dense_matmul/
├── 0.cublas-benchmark/      ← 参考基线（C++ 测试床）
├── 1.triton-benchmark/      ← 对比框架 1
├── 2.bitblas-benchmark/     ← 对比框架 2
├── 3.tilelang-benchmark/    ← TileLang 主角（编号 3）
├── benchmark.sh             ← 顶层编排
├── plot.sh
├── plot_operator_figures_{fp16_gemv,fp16_gemm,int8_gemv,int8_gemm}.py
├── data/  pdf/  png/
```

可见 provider 用 `-benchmark` 连字符后缀、TileLang 在编号 3。但这个约定**并不被严格遵守**，对照另外两个算子就暴露了：

- [hopper_benchmark/flashattention/](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention) 只有 `0.torch_benchmark/`、`1.tilelang_benchmark/`、`2.triton_benchmark/`——这里 **TileLang 是编号 1（不是 3）**，参考基线是 **torch 而非 cuBLAS**（因为 attention 没有 cuBLAS 一行接口），分隔符还混用了 `_benchmark` 下划线，且**没有顶层 `benchmark.sh`**。
- [hopper_benchmark/dequantize_matmul/](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dequantize_matmul) 是 `0.cublas-benchmark/`、`1.triton-benchmark/`、`3.tilelang-benchmark/`、`4.bitblas_benchmark/`、`5.marlin-benchmark/`——**编号缺 2（跳号）**，TileLang 又回到了 3，`bitblas` 用下划线、其它用连字符。

把三者放一起，能提炼出**约定的真实形态**（而非理想形态）：

| 算子 | TileLang 编号 | 参考基线 | 分隔符 | 有顶层 `benchmark.sh`？ |
| --- | --- | --- | --- | --- |
| dense_matmul | 3 | cuBLAS (`0.`) | `-` 为主 | ✅ |
| flashattention | 1 | torch (`0.`) | `_` | ❌ |
| dequantize_matmul | 3 | cuBLAS (`0.`) | `-`/`_` 混用 | ❌ |

> **结论**：`0.` = 参考基线、编号 = 运行顺序，这两条是硬约定；但 **TileLang 的具体编号、分隔符 `-`/`_`、是否跳号、是否有顶层 `benchmark.sh`** 都是软约定，因算子而异。新增算子时**照搬某个算子的字面编号会踩坑**，必须以目标算子实际 `ls` 为准。

另一个易被忽略的事实：`benchmark.sh` 这个名字在仓库里出现 12 次，但**只有 3 个是「算子级顶层编排」**（`hopper_benchmark/dense_matmul/`、`hopper_benchmark/deepgemm/`、`ada_benchmark/dense_matmul/`、`ada_benchmark/lowprecision_matmul/`）。其余大多在**单个 provider 目录内部**（如 `1.marlin_benchmark/benchmark.sh`），用途是「编译/安装该 provider 自己」，与「串联所有 provider」是两回事。不能见 `benchmark.sh` 就以为是编排脚本。

#### 4.1.4 代码实践

**实践目标**：用真实目录树验证「约定 vs 不一致」。

**操作步骤**：

1. 在仓库根目录执行 `ls hopper_benchmark/dense_matmul/`，确认上面样板布局。
2. 执行 `ls hopper_benchmark/dequantize_matmul/`，找到「缺 2、TileLang 是 3」的证据。
3. 用 `find . -name benchmark.sh` 列出所有同名脚本，区分哪些在算子顶层、哪些在 provider 内部。

**需要观察的现象**：`dequantize_matmul` 目录里确实没有 `2.*`；`find` 的结果里多数 `benchmark.sh` 路径含 `marlin_benchmark/` 或 `cutlass_fpa_intb_benchmark/`（provider 内部）。

**预期结果**：能画出一张「算子 → TileLang 编号 → 是否有顶层编排」的对照表，并得出「编号不可照搬」的结论。

**待本地验证**：上述 `ls`/`find` 的输出取决于本地工作副本，请实际运行确认。

#### 4.1.5 小练习与答案

**Q1**：新增 `flashattention` 算子时，为什么参考基线用 `0.torch_benchmark` 而不是 `0.cublas-benchmark`？

**参考答案**：cuBLAS 没有现成的 FlashAttention 单行接口（attention 包含在线 softmax，不是一次 `cublasGemmEx`），所以用 PyTorch eager（`F.scaled_dot_product_attention` 或手写 softmax）作参考实现。这体现「`0.` = 参考基线」是硬约定，但**基线具体是谁取决于算子有没有厂商库一行接口**。

**Q2**：你在 `dequantize_matmul` 下看到 `3.tilelang-benchmark` 但没有 `2.*`，这说明什么？要不要「补一个 `2.` 占位」？

**参考答案**：说明编号会跳号（历史上可能删过某个 provider）。编号只决定 `benchmark.sh` 运行顺序、不要求连续，**不需要补占位**；盲目补号反而会让 `benchmark.sh` 引用不存在的目录而报错（见 4.4）。

---

### 4.2 内核 + shell 驱动

#### 4.2.1 概念说明

每个 provider 子目录里，核心是一对文件：

- **内核 `.py`**：实现算子并暴露**命令行入口**（`argparse` 接 `--m/--n/--k` 等 shape 参数，跑完打印一行带关键字的 latency）。它对 shell 是个黑盒——shell 只关心「给它 shape 参数、它吐日志」。
- **驱动 `.sh`**：遍历一组 `(shape, dtype)` 组合，对每组 `python xxx.py --m .. --n .. --k ..` 并用 `tee` 把 stdout 写进一个**文件名能反推 shape 的日志**。

这一对是「解释型 provider」的通用模式（Triton/BitBLAS/TileLang 都这样）。cuBLAS 是「编译型」例外：它没有 `.sh` 驱动 python，而是 `compile_and_run.sh` 用 CMake 把 `.cu` 编成二进制（见 u1-l3、u2-l5）。本节聚焦解释型。

#### 4.2.2 核心流程

驱动 shell 的执行模板（伪代码）：

```
mkdir -p <日志目录>
for shape in <shape 列表>:
    for dtype in <dtype 列表>:
        日志名 = 拼接(shape, dtype)          # 文件名要能反推 shape/dtype
        python ./内核.py --m .. --n .. --k .. 2>&1 | tee 日志名
```

关键设计点是**日志文件名**：它必须把 shape/dtype 编码进去，因为下游 `data/*.py` 要靠文件名（或日志内的 `m,n,k` 子串）定位某条延迟。命名格式是 provider 之间的**契约接缝**，新建 provider 时要对齐。

#### 4.2.3 源码精读

先看 TileLang 驱动 shell 的真实实现：[hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.sh:3-56](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.sh#L3-L56)。它先把 shape 列表放进 bash 数组（L7-22），把 dtype 组合放进另一个数组（L25-28，这里只启用 `int8 int8 int32 int32`），然后双层 `for` 循环（L31-36）。日志文件名在 [L41](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.sh#L41) 拼出：

```bash
log_file="benchmark_logs/benchmark_${m}_${n}_${k}_${A_dtype}_${W_dtype}_${out_dtype}_${accum_dtype}.log"
```

这条命名把 `m/n/k/四个 dtype` 全编码进文件名，是下游定位的依据。真正的调用在 [L47-L51](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.sh#L47-L51)：构造 `python ./benchmark_tilelang_matmul.py --m .. --n .. --k ..` 命令，再 `bash -c "$cmd 2>&1 | tee ${log_file}"` 把 stdout+stderr 合并写盘。

被调用的内核 `.py` 末尾有标准命令行入口：[benchmark_tilelang_matmul.py:251-265](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L251-L265) 用 `argparse` 接 `--m/--n/--k/--with_roller`，再在 [L268](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L268) 算 `total_flops = 2*M*N*K`。内核跑完打印的关键行在 [L278-L279](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L278-L279)：

```python
print(f"Best latency (s): {best_latency}")
print(f"Best TFlops: {total_flops / best_latency * 1e-9:.3f}")
```

注意 **`Best latency (s)` 这行**——它带关键字 `Best latency`，正是下游正则匹配的锚点；而标签写 `(s)` 但实际数值是 **ms**（单位陷阱，见 u2-l4）。`best_result` 的字段取出在 [L271-L274](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L271-L274)（`.latency/.config/.ref_latency`，注意 docstring 还停留在旧「三元组」说法，以代码为准）。

**对照组——Triton 驱动 shell 的命名坑**：[benchmark_float16.sh:1](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/1.triton-benchmark/benchmark_float16.sh#L1) 先 `mkdir -p ./logs`，然后 [L20-L33](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/1.triton-benchmark/benchmark_float16.sh#L20-L33) 是一行一个 shape 的 `python ... | tee ./logs/benchmark_tilelang_m..._float16.log`。这里有个**历史遗留 bug**：Triton 的日志文件名误写成 `benchmark_tilelang_m...`（应为 `triton`），但因为它**自己内部一致**（生成与读取都用这个名字），所以管线仍能跑通——只是文件名有误导性。这提醒我们：日志命名只要 provider 内部自洽即可，但**新 provider 应取规范名字**，避免给后人挖坑。

#### 4.2.4 代码实践

**实践目标**：跟踪一个 shape 从 shell 到日志的全过程，理解「文件名契约」。

**操作步骤**：

1. 读 `benchmark_tilelang_matmul.sh`，挑 `shapes` 数组里第一个 `"1024 1024 8192"`、dtype `int8 int8 int32 int32`。
2. 手工拼接它对应的 `log_file` 文件名（按 L41 的模板）。
3. 读 `benchmark_tilelang_matmul.py` 的 `__main__`，确认这组参数会让内核打印 `Best latency (s): ...`。

**需要观察的现象**：日志文件名应为 `benchmark_logs/benchmark_1024_1024_8192_int8_int8_int32_int32.log`，日志内能 grep 到 `Best latency` 与 `Best TFlops` 两行。

**预期结果**：你能不运行就写出任意 `(shape, dtype)` 对应的日志路径，并指出下游要用日志里的哪个数字。

**待本地验证**：实际日志数值依赖 GPU 与 TileLang 安装，本仓库已归档，运行需自备环境。

#### 4.2.5 小练习与答案

**Q1**：为什么 shell 要把 stdout 和 stderr 都 `2>&1 | tee` 到日志？

**参考答案**：内核的 latency 打印走 stdout，但 autotune 进度、警告、报错走 stderr。合并写盘既能保证 latency 行进日志，也能在内核崩溃时留下堆栈，方便排查为什么某个 shape「数据缺失」（对应 4.3 的 `-1` 哨兵）。

**Q2**：如果你想给新算子加一个 dtype（比如 fp8），shell 里最少改哪几处？

**参考答案**：①在 `dtypes` 数组加一行（如 `"e4m3 e4m3 fp32 fp32"`）；②确认内核 `.py` 的 `dtype/accum_dtype` 能接受该 dtype（TileLang 内核里 `dtype` 往往写死，需参数化）；③日志文件名模板已含 dtype 变量，无需改；④下游 `data/*.py` 的正则与 provider 列表通常按 dtype 分文件，可能要新建一个 `data_<dtype>_<op>.py`。

---

### 4.3 data/plot 管线

#### 4.3.1 概念说明

各 provider 各跑各的、日志格式五花八门（cuBLAS 是一张 CSV 大表、Triton/BitBLAS 每 shape 一文件、TileLang 又一个文件名约定）。要把它们画进同一张 speedup 对比图，需要一个**汇总层**：`data/*.py`。它的产物是一个简单到不能再简单的数据结构——`(provider 名, [各 shape 的 latency])` 二元组列表——作为 data 脚本与画图脚本之间的**契约**。画图脚本只认这个结构，不关心日志长什么样。

这就是本仓库最有复用价值的部分：**新增算子时，只要照搬这套 data/plot 骨架、换掉正则与 shape 列表，就能直接得到对比图。**

#### 4.3.2 核心流程

「日志→数据→图表」三段式：

1. **日志**：每个 provider 的 `.sh` 跑完留下日志（4.2）。
2. **数据（data 脚本）**：`data/data_<dtype>_<op>.py` 用正则从各 provider 日志抽 latency，填进 `matmul_times_data = [(provider, [latency...]), ...]`，并暴露 `matmul_providers`（shape 标签列表）。未采集到的格子用 `-1` 占位。
3. **图表（plot 脚本）**：`plot_operator_figures_<dtype>_<op>.py` `from data.<...> import matmul_times_data, matmul_providers`，以 cuBLAS 为 1× 基线算 speedup，画柱状图，存 `pdf/`、`png/`。

#### 4.3.3 源码精读

**契约数据结构**在 [data/data_float16_gemm.py:5-11](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/data/data_float16_gemm.py#L5-L11)：

```python
matmul_providers = ["M0","M1",...,"M12"]          # 13 个 shape 的标签
matmul_times_data = [
    ('cuBLAS-W$_{FP16}$A$_{FP16}$', [-1, -1, ...]),  # 13 个 -1 占位
    ('Triton-W$_{FP16}$A$_{FP16}$', [-1, -1, ...]),
    ('BitBLAS-W$_{FP16}$A$_{FP16}$', [36.313, 41.244, ...]),
]
```

`-1` 是**哨兵值**，表示「该 shape 的该 provider 数据未采集」（干净仓库没跑过基准，所以 cuBLAS/Triton 全是 -1，只有 BitBLAS 留了旧数据）。`matmul_times_data` 就是契约——下游 plot 只读它。

**三条解析路径**展示了「如何对付格式各异的日志」：

- **cuBLAS**（一张 CSV 大表）：[get_and_print_cublas, L15-L23](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/data/data_float16_gemm.py#L15-L23)。因为所有 shape 在同一文件，靠 `f"{m},{n},{k}" in line` 定位行，再 `re.findall(r"\d+\.\d+", line)[-2]` 取倒数第二个浮点（即 fp16 Tensor Core 那列，见 u2-l7）。日志路径在 [L40](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/data/data_float16_gemm.py#L40) 指向 `../0.cublas-benchmark/benchmark_results.log`。
- **Triton**（每 shape 一文件）：[get_and_print_triton, L48-L54](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/data/data_float16_gemm.py#L48-L54)。靠文件名定位，取 `[-2]`。日志路径模板在 [L71](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/data/data_float16_gemm.py#L71)——注意它写的是 `benchmark_tilelang_m_{m}_n_{n}_k_{k}.log`，正好对应 4.2 提到的 Triton 文件名误写 bug，两者自洽所以能跑。
- **BitBLAS**（每 shape 一文件）：[get_and_print_bitblas, L80-L86](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/data/data_float16_gemm.py#L80-L86)。取 `[-1]`（唯一浮点）。日志路径在 [L103](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/data/data_float16_gemm.py#L103) 指向 `../2.bitblas-benchmark/benchmark_logs/benchmark_{m}_{n}_{k}_float16_float16_float16_float16.log`。

每段都有 `if not os.path.exists(log_path): continue`（如 [L41-L42](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/data/data_float16_gemm.py#L41-L42)），日志缺失就跳过、保留 `-1`——这就是「干净仓库图不可信」的成因。

**单 provider 的轻量抽取**：除 `data/*.py` 外，provider 目录里常放一个 `extract_benchmark_results.py` 只看自己一家。如 [3.tilelang-benchmark/extract_benchmark_results.py:21-31](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/extract_benchmark_results.py#L21-L31) 直接 `if "Best latency" in line` 匹配（正是 4.2 内核打印的那行），取 `[0]`（第一个浮点）。它不参与跨 provider 对比，只是调试用。

**画图脚本**读 data 模块、算 speedup：[plot_operator_figures_fp16_gemm.py:3-4](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/plot_operator_figures_fp16_gemm.py#L3-L4) `from data.data_float16_gemm import matmul_times_data, matmul_providers`。speedup 计算在 [L11-L23](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/plot_operator_figures_fp16_gemm.py#L11-L23)：以 `'cuBLAS-W$_{FP16}$A$_{FP16}$'` 为 1× 基线，`speedup = cublas_time / framework_time`，>1 即更快，并在 [L64](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/plot_operator_figures_fp16_gemm.py#L64) 画 `y=1` 黑色虚线。最后 [L117-L118](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/plot_operator_figures_fp16_gemm.py#L117-L118) 存 `pdf/`、`png/`。`plot.sh`（[L1-L7](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/plot.sh#L1-L7)）只是 `mkdir pdf png` 后顺序调用四个 `plot_*.py`。

> **复用要点**：`plot_*.py` 与具体算子无关，它只认 `(provider, [times])` 契约。新增算子时，把 `plot_operator_figures_fp16_gemm.py` 整文件复制改名，改三处即可——`from data.<新模块> import`、`_1x_baseline` 字符串、标题与 savefig 文件名。

#### 4.3.4 代码实践

**实践目标**：跟踪 BitBLAS 在 `M0`（shape `16384,16384,16384`）的 latency 从日志流到柱状图。

**操作步骤**：

1. 在 `data_float16_gemm.py` 找到 BitBLAS 解析段（L79-108），确认 `M0` 对应 shape `[16384,16384,16384]`（L88-101 的第 0 个）。
2. 拼出它的日志路径（L103 模板）：`../2.bitblas-benchmark/benchmark_logs/benchmark_16384_16384_16384_float16_float16_float16_float16.log`。
3. 确认 `get_and_print_bitblas` 取 `[-1]`，把数值 36.313 填进 `matmul_times_data[2][1][0]`。
4. 在 `plot_operator_figures_fp16_gemm.py` 看 speedup 公式：`cublas_time(M0) / 36.313`——但 cuBLAS 全是 -1，所以 speedup 会算出负数（哨兵值污染）。

**需要观察的现象**：BitBLAS 的 36.313（ms）会作为该 shape 的柱高基准之一；但因 cuBLAS 基线是 -1，speedup 为负，图不可信。

**预期结果**：你能说清「为什么干净仓库下 speedup 图不可信」——因为 1× 基线（cuBLAS）尚未采集，`-1/36.313` 是负数。这正好印证 u2-l7 的 `-1` 哨兵陷阱。

**待本地验证**：跑完所有 provider 后 `-1` 才会被真实 latency 替换，图才可信。

#### 4.3.5 小练习与答案

**Q1**：cuBLAS 解析取 `[-2]`、BitBLAS 取 `[-1]`，为什么不统一？

**参考答案**：日志列数不同。cuBLAS 的 CSV 一行含多精度多列（fp32/fp16/int8 等），`[-2]` 精确选 fp16 Tensor Core 列；BitBLAS 日志每文件只一个 latency 浮点，`[-1]` 即是它。下标是按各家日志格式裁剪的，**新增 provider 时必须先 `cat` 一条日志数清楚列数再定下标**，不能照搬。

**Q2**：新算子想加第 4 个 provider（比如 Marlin），`data/*.py` 要改哪里？

**参考答案**：①在 `matmul_times_data` 列表追加一行 `('Marlin-...', [-1]*N)`；②新增一个 `get_and_print_marlin` 函数（按 Marlin 日志格式写正则、定下标、定日志路径模板）；③在循环里调用它填充该行。`matmul_providers`（shape 标签）不变，因为 shape 数量没变。

---

### 4.4 benchmark.sh 编排

#### 4.4.1 概念说明

`benchmark.sh` 是算子目录顶层的**编排脚本**：按 provider 编号顺序，`cd` 进每个子目录、执行该 provider 自己的脚本、再 `cd ..` 回来。它的价值是「一条命令跑完整个算子的所有对比」。但请注意 4.1.3 的结论：**顶层 `benchmark.sh` 是少数派**，多数算子要手动逐个 provider 跑。所以这一节既是「怎么用」，也是「怎么写一个不踩坑的」。

#### 4.4.2 核心流程

编排模板（极简）：

```
cd 0.<baseline>-benchmark;  ./<该 provider 脚本>;  cd ..
cd 1.<framework>-benchmark; ./<该 provider 脚本>;  cd ..
...
```

注意三个要点：①顺序就是编号顺序（基线在前，保证下游 speedup 有 1× 参考）；②每个 provider 用**各自的脚本名**（cuBLAS 是 `compile_and_run.sh`、Triton 是 `benchmark_float16.sh`、TileLang 是 `benchmark_tilelang_matmul.sh`），没有统一接口；③脚本很简单，但也正因简单，**目录名/脚本名写错就会整段失败**。

#### 4.4.3 源码精读：编排与它的历史 bug

[hopper_benchmark/dense_matmul/benchmark.sh:1-13](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/benchmark.sh#L1-L13) 全文只有三段：

```bash
cd 0.cublas-benchmark
./compile_and_run.sh      # cuBLAS：编译型，CMake 编 .cu
cd ..

cd 1.triton-benchmark
./benchmark_float16.sh    # Triton：解释型，python 循环
cd ..

cd 2.tilelang-benchmark    # ⚠️ 这个目录不存在！
./benchmark_bitblas_matmul.sh
cd ..
```

前两段正确：`0.` 是 cuBLAS 基线、`1.` 是 Triton。**第三段有两个坑**：

1. **目录名张冠李戴**：脚本写 `cd 2.tilelang-benchmark`，但 4.1.3 的 `ls` 显示真实目录是 `2.bitblas-benchmark`（根本没有 `2.tilelang-benchmark`）。bash 在 `cd` 失败时（无 `set -e`）不会退出，而是留在当前目录（`dense_matmul/`），随后执行 `./benchmark_bitblas_matmul.sh`——而该脚本在 `dense_matmul/` 顶层也不存在，于是这段报 `No such file or directory`。
2. **它其实是 BitBLAS，不是 TileLang**：要调的脚本叫 `benchmark_bitblas_matmul.sh`，确属 `2.bitblas-benchmark/`（我们 4.1 已 `ls` 确认该脚本存在）。也就是说这一段本意是跑 BitBLAS，但目录名误写成了 tilelang。

> **结论**：照搬 `benchmark.sh` 字面量会直接报错。正确写法应是把第三段目录改成 `2.bitblas-benchmark`，并视需要再补一段 `3.tilelang-benchmark` 跑 TileLang 主角（样板里 TileLang 编号是 3，却根本没进编排——这也是个遗漏）。这是「以代码/实际目录为准，不盲信脚本字面量」原则（u1-l3）最鲜活的例证。

顺带留意：cuBLAS 段调的是 `compile_and_run.sh`（编译型，见 u2-l5），其余段调 `.sh` 驱动 python（解释型，见 4.2）——编排脚本统一了入口为「各 provider 的 `.sh`」，把编译/解释的差异封装在子目录里。

#### 4.4.4 代码实践

**实践目标**：为 `dense_matmul` 写一份**正确且完整**的 `benchmark.sh`。

**操作步骤**：

1. 先 `ls` 每个 `*.*-benchmark/` 子目录，记下各自真实的驱动脚本名（`compile_and_run.sh` / `benchmark_float16.sh` / `benchmark_bitblas_matmul.sh` / `benchmark_tilelang_matmul.sh`）。
2. 按 `0./1./2./3.` 编号顺序，重写 `benchmark.sh`，把第三段目录改为 `2.bitblas-benchmark`，并补上 `3.tilelang-benchmark` 段。
3. （可选）在开头加 `set -e`，让任一段失败即停，避免错误级联。

**示例代码**（非项目原代码，为新写的修正版）：

```bash
#!/bin/bash
set -e
for d in 0.cublas-benchmark 1.triton-benchmark 2.bitblas-benchmark 3.tilelang-benchmark; do
  echo "=== running $d ==="
  (cd "$d" && ls *.sh)   # 先确认该目录真实存在的脚本名
done
```

**需要观察的现象**：`ls *.sh` 会显示每个 provider 真实的脚本名，暴露原 `benchmark.sh` 里 `2.tilelang-benchmark` 与 `benchmark_bitblas_matmul.sh` 的不匹配。

**预期结果**：得出一份「目录→真实脚本名」映射表，并据此改出可运行的编排。

**待本地验证**：实际能否跑通依赖 GPU 与各框架安装（仓库已归档）；本实践重在「核对目录与脚本名」的工程动作，不要求真跑。

#### 4.4.5 小练习与答案

**Q1**：为什么编排脚本把 cuBLAS（`0.`）放在最前面？

**参考答案**：cuBLAS 是参考基线（1× 标尺）。下游 `data/*.py` 的 speedup 以它为分母，画图脚本以它画 `y=1` 虚线。先跑基线，后续 provider 跑完即可立即对照；从工程上也方便「先把基线日志备好，再逐个对比」。

**Q2**：如果某个 provider 还没装好（比如 Marlin 要 git 编译），编排脚本该怎么处理？

**参考答案**：不要让缺一个 provider 就整条编排挂掉。两种做法：①在每段前加 `[ -x ./xxx.sh ] || exit 0` 之类的保护，缺则跳过；②不用 `set -e`，让单段失败不影响其它。同时该 provider 的 latency 会保持 `-1` 哨兵（4.3），下游自动跳过。

---

## 5. 综合实践

**任务**：为新算子 **RMSNorm**（LayerNorm 的无均值变体，带宽受限的归一化算子）在 `hopper_benchmark/` 下设计一套完整的基准。要求复用现有基线与可视化管线，产出目录结构、文件清单、`benchmark.sh` 编排顺序与 data/plot 复用方案。

> 选 RMSNorm 而非 batched GEMM 的理由：batched GEMM 几乎是 GEMM 的批量复制（内核改 `T.Kernel` 多一维即可，复用度太高、学不到新约定）；RMSNorm 是 reduction+elementwise 算子，没有 cuBLAS 一行接口，能逼你做出「参考基线用谁」「shape 维度怎么定义」等真实工程决策，正好检验你对本讲的掌握。

### 5.1 设计要点与决策

1. **参考基线**：RMSNorm 无 cuBLAS 接口 → 参照 `flashattention` 用 `0.torch_benchmark`（`torch.nn.functional.rms_norm` 或手写 `x / sqrt(mean(x²)+eps) * weight`）。
2. **对比框架**：`1.triton-benchmark`（Triton reduction 内核）、`2.tilelang-benchmark`（TileLang `T.reduce_sum` + elementwise）。
3. **shape 维度**：RMSNorm 典型入参是 `[batch, hidden]`，按 hidden 归约。shape 表用 `(batch, hidden)`，如 `(4096,4096)`、`(8192,8192)`、`(1, 11008)`（decode）等，仿照 V/M 族命名定义一组 `N0..N7` 标签。
4. **算力量度**：RMSNorm 带宽受限，TFlops 不是好指标，应同时报「有效带宽 GB/s」。但为复用现成 plot 脚本，先沿用 latency + speedup。
5. **命名一致性**：吸取 4.1～4.4 的教训，**统一用 `-benchmark` 连字符后缀**、**日志名带 `rmsnorm` 而非抄成 tilelang**、**编排脚本目录名以 `ls` 为准**。

### 5.2 目录结构与文件清单

```
hopper_benchmark/rmsnorm/
├── benchmark.sh                       # 新写：编排 0/1/2
├── plot.sh                            # 复用 dense_matmul 模板
├── plot_operator_figures_fp16_rmsnorm.py   # 复制 plot_operator_figures_fp16_gemm.py 改名
├── data/
│   └── data_fp16_rmsnorm.py           # 复制 data_float16_gemm.py 改名
├── pdf/   png/                        # 产物
├── 0.torch-benchmark/                 # 参考基线（参照 flashattention）
│   ├── benchmark_rmsnorm.py           # torch 实现 + __main__ 接 --batch --hidden
│   └── benchmark_rmsnorm.sh           # 遍历 shape、tee 日志
├── 1.triton-benchmark/
│   ├── benchmark_triton_rmsnorm.py
│   ├── benchmark_fp16.sh
│   └── (logs/)                        # 运行时生成
└── 2.tilelang-benchmark/
    ├── benchmark_tilelang_rmsnorm.py  # T.reduce_sum + elementwise
    ├── benchmark_tilelang_rmsnorm.sh
    └── extract_benchmark_results.py   # 复制 dense_matmul 同名文件
```

### 5.3 benchmark.sh 编排顺序（新写，修正版）

```bash
#!/bin/bash
# 编排顺序 = provider 编号；0. 是参考基线（torch）
set -e
for d in 0.torch-benchmark 1.triton-benchmark 2.tilelang-benchmark; do
  echo "=== running $d ==="
  # 先核对真实脚本名，避免 4.4 的字面量坑
  sh=$(ls "$d"/*.sh | head -1)
  (cd "$d" && bash "$(basename "$sh")")
done
```

> 关键改进：①目录名以 `ls` 实测为准（避免 `2.tilelang` vs `2.bitblas` 式张冠李戴）；②`set -e` + 显式循环，顺序即编号；③参考基线用 torch（无 cuBLAS 接口）。

### 5.4 data/plot 复用方案

- **data 脚本**：复制 `data_float16_gemm.py` 为 `data_fp16_rmsnorm.py`，改三处——`matmul_providers` 换成 `N0..N7`（你的 shape 标签）、shape 列表换成 `(batch, hidden)` 元组、三个 `get_and_print_*` 函数的日志路径模板与下标按新 provider 日志格式重定（务必先 `cat` 一条日志数清楚取数下标，见 4.3 Q1）。契约 `matmul_times_data` 结构不变。
- **plot 脚本**：复制 `plot_operator_figures_fp16_gemm.py` 为 `plot_operator_figures_fp16_rmsnorm.py`，改三处——`from data.data_fp16_rmsnorm import`、`_1x_baseline` 改成 `'torch-...'`（因为基线是 torch 不是 cuBLAS）、标题与 savefig 改名。speedup 逻辑、柱状图、`y=1` 虚线全部复用。
- **plot.sh**：复制后只保留一行 `python plot_operator_figures_fp16_rmsnorm.py`。

### 5.5 验收清单（自检）

- [ ] 三个 provider 各自 `__main__` 都接同样的 shape 参数、打印带统一关键字（如 `Best latency`）的行。
- [ ] `data_fp16_rmsnorm.py` 跑完 `print(matmul_times_data)` 不再有 `-1`（除非真没采集）。
- [ ] `plot` 出图后，`torch` 基线柱高为 1（speedup 自身为 1），其余柱可读。
- [ ] `benchmark.sh` 里每个目录名都经过 `ls` 核对，不存在「字面量 vs 真实目录」不符。

> **待本地验证**：本实践为「设计 + 复用规划」，数值结果依赖 GPU 与框架安装，需在真实环境落地后核对。

## 6. 本讲小结

- 一个完整算子目录的**标准件**：编号框架子目录（`0.` 基线 + `N.` 对比框架）、内核 `.py`（带 `argparse` 入口）、驱动 `.sh`（遍历 shape、`tee` 日志）、`data/*.py`（汇总契约）、`plot_*.py` + `plot.sh`（画图）、可选顶层 `benchmark.sh`（编排）。
- **硬约定**：`0.` = 参考基线、编号 = 运行顺序；**软约定**：TileLang 的具体编号、`-`/`_` 分隔符、是否跳号、是否有顶层 `benchmark.sh` 都因算子而异，照搬会踩坑。
- 「日志→数据→图表」管线是**最值得复用**的部分：`data` 与 `plot` 之间靠 `(provider, [latency])` 契约解耦，新增算子换正则与 shape 列表即可，plot 脚本几乎全复用。
- 驱动 shell 的**日志文件名是 provider 间的契约接缝**，必须把 shape/dtype 编码进文件名；命名要内部自洽（Triton 误写成 `tilelang` 但自洽仍能跑），但新算子应取规范名。
- 本仓库的 `benchmark.sh` 多数在 provider 内部（编译/安装用），只有少数在算子顶层（编排用）；`dense_matmul/benchmark.sh` 第三段目录名 `2.tilelang-benchmark` 是历史 bug（实际是 `2.bitblas-benchmark`），是「以代码为准」原则的最佳例证。
- 新增算子的核心动作不是写内核，而是**先 `ls` 核对目录与脚本名、对齐日志命名契约、复用 data/plot 骨架**，最后才是在 TileLang 里实现算子本体（回到 u3/u4/u5 的内核写法）。

## 7. 下一步学习建议

本讲是手册收尾，没有「下一讲」。建议你沿以下方向继续：

1. **动手落地一个新算子基准**：按第 5 节的 RMSNorm 设计真正实现一遍（TileLang 内核里归约用 `T.reduce_sum`、elementwise 用 fragment 上的逐元素循环，可参考 u4-l13 的线程级归约与 u5-l16 的 fragment 缓冲写法）。这是检验全手册掌握程度的最好方式。
2. **横向读其它算子的目录**：把 `hopper_benchmark/dequantize_matmul/`、`cdna_benchmark/mla_benchmark/`、`cdna_benchmark/conv_benchmark/` 三个算子按本讲的「标准件清单」逐项对照，看它们各自省略了哪些件、为什么（例如 cdna 用 tvm.tl 变体而非独立 tilelang 包，见 u6-l21）。
3. **追上游项目 TileOps**：本仓库已归档，后续工作迁移到 TileOps（见 u1-l1 的归档提示）。新增算子的工程约定在那里可能已演进，建议对照 TileOps 的目录结构验证本讲总结的约定哪些仍然成立。
4. **回归内核本身**：本讲把内核当黑盒。如果你要写的不是「套用 GEMM 模板」的算子（如 RMSNorm 这种 reduction+elementwise），需要回到 u3-l8～u3-l11（autotune/jit/prim_func、块级原语、Roller、swizzle）重新选合适的调度原语——并非所有算子都走 `T.gemm`。
