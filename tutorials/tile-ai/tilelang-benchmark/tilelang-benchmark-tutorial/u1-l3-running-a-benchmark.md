# 运行一次基准测试

## 1. 本讲目标

学完本讲，你应该能够：

- 看懂 `hopper_benchmark/dense_matmul/benchmark.sh` 这个编排脚本如何把多个框架的测试「串」成一次完整运行。
- 区分两类完全不同的运行方式：cuBLAS 是 **CUDA C++ 程序，用 CMake 编译**；Triton / BitBLAS / TileLang 是 **Python 程序，由 shell 调用 python**。
- 说出每个步骤实际执行的命令，以及它产生的日志文件存放在哪里、长什么样。
- 建立一个关键意识：**读脚本时要对照真实目录，不能盲信脚本字面量**——本项目里就存在引用了不存在目录的脚本缺陷。

## 2. 前置知识

本讲承接 [u1-l2 目录组织约定](u1-l2-directory-layout.md)，你已经知道：

- 同一个算子（如 `dense_matmul`）下，会并列摆放多个**编号框架子目录**（`0.cublas-benchmark`、`1.triton-benchmark`、`2.bitblas-benchmark`、`3.tilelang-benchmark`），每个目录代表一个 **provider**（一种实现）。
- `0.` 开头通常是参考**基线**（baseline），编号决定运行顺序。
- 仓库的最终产出是一条「日志 → 数据 → 图表」的可视化管线。

本讲只回答一个问题：**「我敲下一条命令后，机器到底按什么顺序、跑了哪些程序、把结果写到了哪里？」** 我们以 `hopper_benchmark/dense_matmul`（H100 上的稠密矩阵乘）作为贯穿全篇的例子。

需要先解释两个名词：

- **cuBLAS**：NVIDIA 官方的线性代数库，底层是高度优化的 CUDA kernel。它是最常见的「参考基线」——别人再怎么优化，一般也拿 cuBLAS 当标尺比一比。
- **CMake**：一个跨平台的 C/C++ 构建工具。cuBLAS 基线是 C++ 写的，必须先用 CMake 编译成可执行文件；而 Triton / BitBLAS / TileLang 是 Python 脚本，`python xxx.py` 直接就能跑。这就是本讲最核心的差异。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [hopper_benchmark/dense_matmul/benchmark.sh](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/benchmark.sh) | **编排脚本**：按编号顺序依次进入各框架子目录、调用各自的脚本。 |
| [hopper_benchmark/dense_matmul/0.cublas-benchmark/compile_and_run.sh](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/compile_and_run.sh) | **cuBLAS 路径**：用 CMake 编译 C++ 程序，再运行它并把输出存成日志。 |
| [hopper_benchmark/dense_matmul/0.cublas-benchmark/CMakeLists.txt](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/CMakeLists.txt) | **CMake 构建配置**：声明项目、查找 CUDA/cuBLAS、定义可执行文件目标。 |
| [hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu) | **cuBLAS 基线主程序**：定义测试形状集、用 `cublasGemmEx` 跑多精度 GEMM、计时并按 CSV 格式打印。 |
| [hopper_benchmark/dense_matmul/0.cublas-benchmark/Readme.md](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/Readme.md) | cuBLAS 目录下的简易说明（手动编译步骤，内容不完整）。 |
| [hopper_benchmark/dense_matmul/1.triton-benchmark/benchmark_float16.sh](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/1.triton-benchmark/benchmark_float16.sh) | **Triton 路径**：典型的「shell 调用 python」模式。 |
| [hopper_benchmark/dense_matmul/2.bitblas-benchmark/benchmark_bitblas_matmul.sh](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/2.bitblas-benchmark/benchmark_bitblas_matmul.sh) | **BitBLAS 路径**：同样是 shell 调用 python，可作对照。 |
| [hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.sh](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.sh) | **TileLang 路径**：本系列主角，驱动方式与前两者一致。 |

## 4. 核心概念与源码讲解

### 4.1 编排脚本 benchmark.sh：三步串联

#### 4.1.1 概念说明

`benchmark.sh` 是一个算子目录的**总入口**。它的工作非常简单：按编号顺序，依次 `cd` 进每个框架子目录，执行那个目录里负责驱动的脚本，再 `cd ..` 回来。这样一条命令就能把同一算子在多个框架下的结果都跑出来，便于横向对比。

#### 4.1.2 核心流程

把 `benchmark.sh` 的 13 行拆开看，它就是「三段几乎对称的代码块」：

```text
第 1 步：cd 0.cublas-benchmark → ./compile_and_run.sh → cd ..
第 2 步：cd 1.triton-benchmark → ./benchmark_float16.sh → cd ..
第 3 步：cd 2.tilelang-benchmark → ./benchmark_bitblas_matmul.sh → cd ..
```

注意第 1 步和第 2、3 步有一个本质差别：

- 第 1 步调用的 `compile_and_run.sh`，名字里带 **compile**（编译）——因为 cuBLAS 基线是 C++，得先编译。
- 第 2、3 步调用的是 `benchmark_xxx.sh`，里面直接 `python xxx.py`，**没有编译这一步**。

#### 4.1.3 源码精读

整个编排脚本如下，三个代码块一目了然：

[hopper_benchmark/dense_matmul/benchmark.sh:L1-L13](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/benchmark.sh#L1-L13) — 完整的三步编排。

第一步，cuBLAS 路径：

[hopper_benchmark/dense_matmul/benchmark.sh:L3-L5](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/benchmark.sh#L3-L5) — 进入 `0.cublas-benchmark` 目录，执行编译并运行脚本，然后回到上层目录。

第二步，Triton 路径：

[hopper_benchmark/dense_matmul/benchmark.sh:L7-L9](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/benchmark.sh#L7-L9) — 进入 `1.triton-benchmark`，运行 `benchmark_float16.sh`。

> ⚠️ **重要：第三步是个真实的 bug，照搬运行会失败。**

[hopper_benchmark/dense_matmul/benchmark.sh:L11-L13](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/benchmark.sh#L11-L13) — 这一段写的是 `cd 2.tilelang-benchmark` 然后执行 `./benchmark_bitblas_matmul.sh`。

但对照真实目录（见 4.1.4 的实践），磁盘上根本没有 `2.tilelang-benchmark` 这个目录。真实的目录是：

- `2.bitblas-benchmark`（里面有 `benchmark_bitblas_matmul.sh`）
- `3.tilelang-benchmark`（里面有 `benchmark_tilelang_matmul.sh`）

也就是说，脚本第三步的目录名和脚本名「张冠李戴」了：它想去跑 BitBLAS 的脚本，却写成了 TileLang 的目录名。这是 [u1-l2](u1-l2-directory-layout.md) 已经提醒过的「命名不一致」历史遗留之一。**结论：照搬 `./benchmark.sh` 会在第三步报 `No such file or directory` 而中断**；要跑全三个框架，需要手动修正目录名，或分别进入各目录单独运行。

#### 4.1.4 代码实践

1. **实践目标**：亲手验证「脚本字面量」与「磁盘真实结构」是否一致，养成读脚本必查目录的习惯。
2. **操作步骤**：
   - 在仓库根目录执行 `ls hopper_benchmark/dense_matmul/`，列出所有子目录。
   - 再 `cat hopper_benchmark/dense_matmul/benchmark.sh`，逐行比对。
3. **需要观察的现象**：
   - `ls` 会显示 `0.cublas-benchmark / 1.triton-benchmark / 2.bitblas-benchmark / 3.tilelang-benchmark` 四个框架目录。
   - `benchmark.sh` 第 11 行写的是 `2.tilelang-benchmark`，与磁盘上的 `2.bitblas-benchmark` 和 `3.tilelang-benchmark` 都对不上。
4. **预期结果**：你能复述出「第三步目录名错误、脚本名属于 BitBLAS」这个事实，并知道照搬运行会在第三步失败。
5. 上述 `ls` 与 `cat` 的对比结论可在本仓库直接得出；是否真的报错则「待本地在装有 CUDA 的机器上验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果要把 `benchmark.sh` 修正成「依次跑 cuBLAS、Triton、BitBLAS、TileLang 四个框架」，应该怎么改？

**参考答案**：把第三步的 `cd 2.tilelang-benchmark` 改为 `cd 2.bitblas-benchmark`（脚本名 `benchmark_bitblas_matmul.sh` 已经正确），并在其后追加一段：

```bash
cd 3.tilelang-benchmark
./benchmark_tilelang_matmul.sh
cd ..
```

**练习 2**：为什么第 1 步调用的脚本叫 `compile_and_run.sh`，而第 2、3 步叫 `benchmark_xxx.sh`？

**参考答案**：因为 cuBLAS 基线是 C++ 源码，必须先编译（compile）才能运行（run），所以脚本名强调「编译并运行」；而 Triton / BitBLAS / TileLang 是 Python，直接 `python xxx.py` 即可，没有显式编译阶段，因此只叫 benchmark。

---

### 4.2 cuBLAS 路径：compile_and_run.sh 与 CMake 构建

#### 4.2.1 概念说明

cuBLAS 基线是一条「**编译型**」路径：源码是 CUDA C++（`.cu` 文件），不能直接跑，必须先用 CMake 把它编译成一个可执行文件，然后再运行这个可执行文件。`compile_and_run.sh` 把这两步打包到了一起。这一小节讲解「编译」这一步。

#### 4.2.2 核心流程

cuBLAS 路径的执行流程可以画成：

```text
compile_and_run.sh
   │
   ├─ mkdir -p build        # 建立独立的构建目录（已被 .gitignore 忽略）
   ├─ cd build
   ├─ cmake ..              # 读取上一层 CMakeLists.txt，生成 Makefile
   ├─ make -j               # 多核并行编译，产出 build/cublas_benchmark 可执行文件
   ├─ cd ..
   └─ ./build/cublas_benchmark 2>&1 | tee benchmark_results.log
                            # 运行可执行文件，输出同时显示到屏幕并写入日志
```

`cmake ..` 之所以要 `cd build` 再写 `..`，是为了做**外部构建（out-of-source build）**：所有编译产物都落在 `build/` 里，不污染源码目录。注意 `0.cublas-benchmark/.gitignore` 里写的就是 `build`，所以这个目录不会被提交到 git。

#### 4.2.3 源码精读

[hopper_benchmark/dense_matmul/0.cublas-benchmark/compile_and_run.sh:L1-L7](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/compile_and_run.sh#L1-L7) — `mkdir -p build` 建目录、`cmake ..` 生成构建文件、`make -j` 编译、最后一行运行并把输出 tee 到 `benchmark_results.log`。`2>&1` 把标准错误合并进标准输出，`tee` 让日志同时出现在屏幕和文件里。

同目录下还有一份内容不完整的手动说明 [hopper_benchmark/dense_matmul/0.cublas-benchmark/Readme.md:L1-L5](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/Readme.md#L1-L5) ——它写着 `mkdir build / cd build / cmake .. / make -j / ./`，最后那个 `./` 显然被截断了（应当是 `./cublas_benchmark` 之类）。它其实就是 `compile_and_run.sh` 的「手动版」草稿，**以 `compile_and_run.sh` 为准**。

现在看 `cmake ..` 真正读取的构建配置 `CMakeLists.txt`：

[hopper_benchmark/dense_matmul/0.cublas-benchmark/CMakeLists.txt:L1-L7](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/CMakeLists.txt#L1-L7) — 声明项目，并设置默认 CUDA 架构。注意 [L3-L5](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/CMakeLists.txt#L3-L5) 里默认 `CMAKE_CUDA_ARCHITECTURES` 是 **89**（Ada，对应 RTX 4090），而本目录其实是为 Hopper（H100）准备的——这是一个值得注意的默认值与实际目标不一致的现象。

[hopper_benchmark/dense_matmul/0.cublas-benchmark/CMakeLists.txt:L10-L47](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/CMakeLists.txt#L10-L47) — 用 `find_path` / `find_library` 在 `CUDA_ROOT` 环境变量和常见路径（`/usr/local/cuda` 等）下查找 `cublas_v2.h`、`cudart`、`cublas`。找不到时会打印提示。这就是「依赖查找」。

[hopper_benchmark/dense_matmul/0.cublas-benchmark/CMakeLists.txt:L68-L72](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/CMakeLists.txt#L68-L72) — 这是构建的核心：把 `cublas_benchmark.cu` 编译成可执行文件 `cublas_benchmark`，并链接 `-lcublas -lcurand`。这里又有一个矛盾点：第 69 行 `set_target_properties(cublas_benchmark PROPERTIES CUDA_ARCHITECTURES "80")` 把目标架构**强制改成了 80（Ampere / A100）**，覆盖了前面默认的 89。也就是说「文件名说 hopper、默认架构 89、目标却编成 80」三处不一致——这正是 [u7-l24 跨架构适配](u7-l24-cross-architecture-adaptation.md) 会专门讨论的内容，这里先记住「同一份 CMake 里有多个架构设置、彼此可能打架」即可。

#### 4.2.4 代码实践

1. **实践目标**：理解 out-of-source 构建与可执行文件的产出位置。
2. **操作步骤**：阅读 `compile_and_run.sh`，回答——编译产物落在哪个目录？可执行文件叫什么名字？日志写到哪个文件？
3. **需要观察的现象**：根据脚本，`build/cublas_benchmark` 是产物，`benchmark_results.log` 是日志，二者都在 `0.cublas-benchmark/` 下。
4. **预期结果**：你能口述「`mkdir -p build` + `cmake ..` + `make -j` 三步把 `.cu` 变成 `build/cublas_benchmark`，最后一行运行它并 tee 到 `benchmark_results.log`」。
5. 实际编译需 CUDA 工具链，本环境无 GPU，「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `compile_and_run.sh` 要先 `cd build` 再 `cmake ..`，而不是直接在源码目录 `cmake .`？

**参考答案**：为了 out-of-source build，把所有中间产物（`.o`、Makefile、可执行文件）隔离在 `build/` 里，保持源码目录干净；同时 `build/` 被 `.gitignore` 忽略，不会污染版本库。

**练习 2**：`CMakeLists.txt` 里出现了两处 CUDA 架构设置（默认 89、目标强制 80），哪个会真正生效？

**参考答案**：`set_target_properties(... CUDA_ARCHITECTURES "80")` 是针对具体目标 `cublas_benchmark` 的设置，会覆盖全局默认的 89，所以最终按 80（Ampere）编译。（这一点的更多含义见 [u7-l24](u7-l24-cross-architecture-adaptation.md)。）

---

### 4.3 cuBLAS 日志怎么来：测试床与自适应计时

#### 4.3.1 概念说明

上一节讲的是「怎么把 `.cu` 编译成程序」。本节讲「这个程序运行后，日志里的那些数字是哪来的」。`cublas_benchmark.cu` 是一个典型的 **C++ 测试床（test harness）**：它内置一组测试形状，对每个形状跑 fp32 / fp16 / int8 / fp16-tensor-core / int8-tensor-core 多种精度，用 `std::chrono` 计时，最后按 CSV 格式打印一行。

#### 4.3.2 核心流程

```text
main()
  ├─ 打印 "Running inference benchmark" 与设备名
  ├─ 打印 CSV 表头
  └─ for 每个 (m, n, k, a_t, b_t) in inference_server_set:
        ├─ 打印 "m,n,k,a_t,b_t,"
        ├─ fp32   → time_gemm → 打印 ",延迟"
        ├─ fp16   → time_gemm → 打印 ",延迟"
        ├─ int8   → time_gemm → 打印 ",延迟"（M 补齐到 4 的倍数）
        ├─ 切到 CUBLAS_TENSOR_OP_MATH（启用 Tensor Core）
        ├─ fp16 TC → time_gemm → 打印 ",延迟"
        └─ int8 TC → time_gemm → 打印 ",延迟" + 换行
```

`time_gemm` 里有一个**自适应重复次数**的小设计，是理解 cuBLAS 计时公平性的关键：

1. 先 warmup 跑 1 次，用 `std::chrono` 测出单次耗时 \( t_1 \)（微秒）。
2. 令 `minimal_repeat_ms = 100`（毫秒），计算总测量时长至少要到 100 ms 所需的次数：

\[
\text{numRepeats} = \max\!\left(5,\; \left\lfloor \frac{100}{t_1 / 1000} \right\rfloor\right)
\]

3. 连续跑 `numRepeats` 次，取**平均**作为该形状的延迟。

这样做的目的是保证「快的 kernel 多跑几次、慢的 kernel 少跑几次」，让每次测量都累计到足够长的物理时间（≥100 ms），从而降低单次抖动带来的误差——这正是 [u2-l4 性能度量方法论](u2-l4-benchmark-methodology.md) 会系统讲解的「测量稳定性」思想在 C++ 基线里的体现。

#### 4.3.3 源码精读

[hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu:L22-L38](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L22-L38) — `inference_server_set`，一组 `(m, n, k, a_t, b_t)` 元组。注意所有形状 `b_t = true`（B 矩阵转置），这正是 NVIDIA 上常见的 TN 布局；这套形状与 README 里 `M0–M7` 等列族对应（详见 [u2-l4](u2-l4-benchmark-methodology.md)）。

[hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu:L81-L127](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L81-L127) — `minimal_repeat_ms = 100`（[L82](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L82)），先 warmup 一次测 `periter_duration`，再用 `numRepeats = max(5, ...)`（[L127](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L127)）决定正式测量要跑几轮。

[hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu:L203-L206](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L203-L206) — CSV 表头。注意表头里写的是 `... time (usec)`（微秒），但实际打印值是 `time_us / 1000.0`（见 [L237](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L237) 等处），即把微秒除以 1000 换算成**毫秒**。也就是说：**表头标注的单位（usec）和实际数值的单位（ms）对不上**——这是读日志时容易踩的坑，按毫秒理解才正确（严格说「待本地运行确认」，但按代码逻辑应是毫秒）。

[hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu:L269-L301](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L269-L301) — 通过 `cublasSetMathMode(CUBLAS_TENSOR_OP_MATH)` 切到 Tensor Core 路径，再分别跑 fp16 / int8 的 Tensor Core 版本。`cublasGemmEx` 根据 `compute_type` 自动选择对应的硬件指令。

#### 4.3.4 代码实践

1. **实践目标**：找出决定「最少测量 100 ms」的那行代码，理解自适应重复次数。
2. **操作步骤**：在 `cublas_benchmark.cu` 中定位 `minimal_repeat_ms` 与 `numRepeats`，代入一个假想单次耗时（例如 \( t_1 = 2000\,\mu s = 2\,ms \)），手算 `numRepeats`。
3. **需要观察的现象**：\( 100 / 2 = 50 \ge 5 \)，所以 `numRepeats = 50`；若 kernel 极慢（如 \( t_1 = 50\,ms \)），则 \( 100/50 = 2 < 5 \)，被 `max(5, …)` 抬到 5。
4. **预期结果**：能复述「快 kernel 多跑、慢 kernel 至少 5 次、总时长≥100 ms」这套自适应规则。
5. 具体运行数值「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：某个形状单次耗时 \( t_1 = 500\,\mu s \)，`numRepeats` 等于多少？

**参考答案**：\( t_1 = 0.5\,ms \)，\( 100 / 0.5 = 200 \ge 5 \)，所以 `numRepeats = 200`。

**练习 2**：CSV 表头写 `(usec)`，但代码里数值是 `time_us / 1000.0`。这会导致什么误解？

**参考答案**：会让读者误以为数值是微秒，但实际是毫秒。直接拿这个数和别的框架（单位通常是 ms）对比时，如果按微秒理解，会得出「cuBLAS 快了 1000 倍」的错误结论。务必按毫秒读这列。

---

### 4.4 Python 框架路径：shell 调用 python 的统一模式

#### 4.4.1 概念说明

第 2、3 步的 Triton / BitBLAS / TileLang 走的是完全不同的「**解释型**」路径：没有 CMake、没有编译，驱动脚本就是一个 `.sh`，里面把一组 `(m, n, k, dtype)` 形状写成 bash 数组，用 `for` 循环逐个 `python xxx.py --m … --n … --k …` 调用 Python 内核脚本，再用 `tee` 把每个形状的输出存成单独的日志文件。三个框架的 `.sh` 结构几乎一模一样，这是本节要掌握的「统一模式」。

#### 4.4.2 核心流程

一个典型的框架驱动脚本长这样（伪代码）：

```text
mkdir -p <日志目录>
shapes=( "m1 n1 k1"  "m2 n2 k2"  ... )      # bash 数组，列出所有形状
dtypes=( "A W out accum" )                  # 数据精度组合
for shape in shapes:
    read m n k <<< "$shape"
    for dtype_combo in dtypes:
        read A W out accum <<< "$dtype_combo"
        cmd="python ./benchmark_xxx.py --m $m --n $n --k $k ..."
        bash -c "$cmd 2>&1 | tee <日志目录>/benchmark_${m}_${n}_${k}_....log"
```

每个形状对应**一个独立的日志文件**，文件名里编码了形状和精度，方便后续 `data/*.py` 用正则把延迟解析出来（见 [u2-l7 数据提取与可视化](u2-l7-data-extraction-and-plotting.md)）。

#### 4.4.3 源码精读

Triton 路径，先建日志目录，再为每个形状调用 python：

[hopper_benchmark/dense_matmul/1.triton-benchmark/benchmark_float16.sh:L1](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/1.triton-benchmark/benchmark_float16.sh#L1) — `mkdir -p ./logs` 建日志目录。

[hopper_benchmark/dense_matmul/1.triton-benchmark/benchmark_float16.sh:L20-L33](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/1.triton-benchmark/benchmark_float16.sh#L20-L33) — 一连串 `python ./benchmark_triton_matmul_float16.py --m … --n … --k … 2>&1 | tee ./logs/benchmark_tilelang_m…_float16.log`。

> ⚠️ 这里又有一个命名不一致：日志文件名里写的是 `benchmark_tilelang_…`，但跑的明明是 **Triton** 脚本。这是历史遗留，后续 `data/*.py` 解析时必须认这个文件名，不能被「tilelang」字样误导。

BitBLAS 路径结构更规整，用 bash 数组 + 双层 for 循环（形状 × 精度）：

[hopper_benchmark/dense_matmul/2.bitblas-benchmark/benchmark_bitblas_matmul.sh:L47-L72](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/2.bitblas-benchmark/benchmark_bitblas_matmul.sh#L47-L72) — 双层循环，把形状和精度拆开，拼出 `python ./benchmark_bitblas_matmul.py --M … --N … --K … --A_dtype … --W_dtype … --out_dtype … --accum_dtype …`。

[hopper_benchmark/dense_matmul/2.bitblas-benchmark/benchmark_bitblas_matmul.sh:L57](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/2.bitblas-benchmark/benchmark_bitblas_matmul.sh#L57) — 日志文件名模板 `benchmark_logs/benchmark_${m}_${n}_${k}_${A_dtype}_${W_dtype}_${out_dtype}_${accum_dtype}.log`，形状和精度都编进了文件名。

注意 [L63](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/2.bitblas-benchmark/benchmark_bitblas_matmul.sh#L63) 的参数是大写 `--M/--N/--K`（BitBLAS 的约定），而 Triton / TileLang 用小写 `--m/--n/--k`——不同框架的 argparse 参数名不统一，这也是「对比基线生态」里需要注意的细节。

TileLang 路径与 BitBLAS 几乎同构，只是当前默认跑 int8 精度：

[hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.sh:L25-L28](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.sh#L25-L28) — `dtypes` 数组当前激活的是 `"int8 int8 int32 int32"`（float16 那行被注释掉）。

[hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.sh:L47](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.sh#L47) — `cmd="python ./benchmark_tilelang_matmul.py --m ${m} --n ${n} --k ${k}"`。

#### 4.4.4 代码实践

1. **实践目标**：体会「shell 调用 python」这一统一模式，并能预测日志文件名。
2. **操作步骤**：阅读 `2.bitblas-benchmark/benchmark_bitblas_matmul.sh`，假设当前 `shapes` 第一个元素是 `1 1024 8192`、`dtypes` 是 `float16 float16 float16 float16`，写出它实际执行的那条命令，以及产生的日志文件完整路径。
3. **需要观察的现象**：命令应为 `python ./benchmark_bitblas_matmul.py --M 1 --N 1024 --K 8192 --A_dtype float16 --W_dtype float16 --out_dtype float16 --accum_dtype float16`；日志文件为 `2.bitblas-benchmark/benchmark_logs/benchmark_1_1024_8192_float16_float16_float16_float16.log`。
4. **预期结果**：你能复述「形状 × 精度 → 一条 python 命令 → 一个日志文件」的对应关系。
5. 是否真产生该日志「待本地在装有对应框架的环境中验证」。

#### 4.4.5 小练习与答案

**练习 1**：BitBLAS 用 `--M/--N/--K`（大写），TileLang 用 `--m/--n/--k`（小写）。这种不统一会带来什么风险？

**参考答案**：复制粘贴命令时容易大小写写错，导致 argparse 报 `unrecognized arguments`。写跨框架的统一驱动时要特别注意每个脚本各自约定的参数名。

**练习 2**：为什么 Triton 的日志文件名里会出现 `tilelang` 字样？这对后续数据处理有什么影响？

**参考答案**：历史遗留命名错误（脚本跑的是 Triton，文件名却写了 tilelang）。影响是：`data/*.py` 在定位 Triton 日志时，必须按 `benchmark_tilelang_*.log` 这个真实文件名去找，而不能想当然地去找 `benchmark_triton_*.log`。

---

## 5. 综合实践

把本讲的「三步调用 + 两种运行方式」串起来，完成下面这张「执行追踪表」。请只通过**读源码**（不实际运行）填写：

| 步骤 | benchmark.sh 调用的目录 | 实际执行的驱动脚本 | 运行方式（编译型 / 解释型） | 产生的日志文件路径 | 日志里的关键输出 |
| --- | --- | --- | --- | --- | --- |
| 1 | `0.cublas-benchmark` | `compile_and_run.sh` | ? | ? | ? |
| 2 | `1.triton-benchmark` | `benchmark_float16.sh` | ? | ? | ? |
| 3 | （脚本写成 `2.tilelang-benchmark`，实际应为 ?） | ? | ? | ? | ? |

**要求**：

1. 补全「运行方式」列：第 1 步是编译型（CMake + make），第 2、3 步是解释型（shell 调用 python）。
2. 补全「日志路径」列：第 1 步是 `0.cublas-benchmark/benchmark_results.log`；第 2 步是 `1.triton-benchmark/logs/benchmark_tilelang_m…_float16.log`（每个形状一个）；第 3 步按你修正后的目录填写。
3. 补全「关键输出」列：第 1 步是 CSV（`m,n,k,…` 各精度延迟，注意表头 usec 实为 ms）；第 2 步是 `Mean Latency … ms, Mean performance: … TFLOPS`；第 3 步（TileLang）是 `Best latency (s) / Best TFlops / Best config`。
4. 最后用一句话指出 `benchmark.sh` 第三步的错误，并写出你修正后的完整三步（或四步）调用顺序。

完成后，你就拥有了「从一条 `./benchmark.sh` 命令到每一份日志文件」的完整心智模型，这也是后续 [u2-l4 性能度量方法论](u2-l4-benchmark-methodology.md) 与 [u2-l7 数据提取与可视化](u2-l7-data-extraction-and-plotting.md) 的起点。

## 6. 本讲小结

- `benchmark.sh` 是算子目录的总编排脚本，按编号顺序 `cd` 进各框架子目录、调用各自驱动脚本。
- cuBLAS 走**编译型**路径：`compile_and_run.sh` 用 CMake + make 把 `.cu` 编译成 `build/cublas_benchmark`，再运行并 tee 到 `benchmark_results.log`。
- Triton / BitBLAS / TileLang 走**解释型**路径：`.sh` 里用 `for` 循环对每个形状 `python xxx.py …`，每个形状产出一个独立日志文件。
- cuBLAS 的 C++ 测试床用 `std::chrono` 计时，靠 `minimal_repeat_ms = 100` 自适应决定重复次数，保证总测量时长 ≥100 ms。
- cuBLAS 日志按 CSV 打印多精度延迟，但表头 `(usec)` 与实际毫秒值不一致；Triton / TileLang 日志分别打印 `Mean Latency / TFLOPS` 与 `Best latency / Best TFlops / config`。
- 本项目存在多处历史遗留不一致：`benchmark.sh` 第三步目录名错误、Triton 日志文件名误写为 `tilelang`、CMake 默认架构 89 与目标架构 80 不符——**读脚本必查真实目录与文件名**。

## 7. 下一步学习建议

本讲只解决了「怎么跑、日志在哪」。接下来建议：

- 进入 [u2-l4 性能度量方法论](u2-l4-benchmark-methodology.md)：系统学习 FLOPS / TFlops 计算、warmup 与 rep、shape 配置表，理解日志里那些数字到底代表多少性能。
- 进入 [u2-l5 cuBLAS 参考基准](u2-l5-cublas-reference-harness.md)：深入 `cublas_benchmark.cu` 的 `cublasGemmEx` 多精度调用与 `tensor.h` 工具，把本讲粗略带过的 C++ 测试床讲透。
- 进入 [u2-l7 数据提取与可视化](u2-l7-data-extraction-and-plotting.md)：看 `data/*.py` 如何用正则从本讲这些日志里把延迟解析出来、生成对比图。

如果想直接接触本系列的「主角」，也可以跳到 [u3-l8 TileLang 内核骨架](u3-l8-tilelang-kernel-skeleton.md)，但建议先完成 u2 单元，建立「公平对比」的方法论底座。
