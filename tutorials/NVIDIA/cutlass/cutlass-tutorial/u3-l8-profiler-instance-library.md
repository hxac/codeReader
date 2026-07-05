# CUTLASS Profiler 与实例库

## 1. 本讲目标

本讲要把读者从「会写一个 GEMM 内核」（见 u1-l6、u2-l9）推进到「能用 NVIDIA 官方工具批量生成、编译、测量成百上千个内核，并选出最快的那个」。

读完本讲，你应当能够：

- 用 `cutlass_profiler` 这个命令行工具对 CUTLASS 内核做**功能验证**（与 cuBLAS/cuDNN 比对）与**性能测量**（输出 GFLOP/s）。
- 看懂 CUTLASS 内核的**过程名（procedural name）**——例如 `cutlass_tensorop_s16816gemm_f16_256x128_32x3_nn_align8` 与 `cutlass3x_sm90_tensorop_gemm_f16_f16_f32_f16_f32_128x128x64_2x1x1_0_ntn_align8` 每一段的含义，并据此写出过滤表达式。
- 理解 `CUTLASS_LIBRARY_KERNELS` 这个 CMake 变量如何控制「只编译我想要的那批内核」，从而把动辄数小时的构建压到分钟级。
- 讲清 CUTLASS **实例库（Instance Library）**的代码生成闭环：Python 脚本 `generator.py` 在 CMake 配置期跑一遍，按过滤规则筛出内核、生成 `.cu` 源文件、再回写到 CMake 里编译成 `cutlass_library` 共享/静态库。

## 2. 前置知识

本讲默认你已掌握：

- **CUTLASS 的构建流程**（u1-l2）：知道 `cmake .. -DCUTLASS_NVCC_ARCHS=<sm> && make`，知道 `CUTLASS_NVCC_ARCHS` 决定为哪些 SM 生成代码、带 `a` 后缀（如 `90a`）才允许用 Tensor Core 加速指令。
- **GEMM 的层次化分解**（u1-l1、u2-l6/7）：知道一个内核由「数据类型 + tile 形状 + cluster 形状 + 流水线级数 + 调度策略」等一组配置参数唯一确定。
- **CUTLASS 3.x 的 Schedule 标签**（u2-l7）：知道 `KernelTmaWarpSpecialized`/`Cooperative`/`Pingpong` 等策略会写进内核名里。

本讲只涉及**外部工具链**（CMake + Python 脚本 + 一个 CLI 可执行文件），不涉及任何 GEMM 内部的算子细节，因此**不依赖 GPU**也能读懂大部分内容；只有「跑 profiler 测 GFLOP/s」这一步需要真实硬件。

### 两个核心概念

- **实例（instance）**：把 CUTLASS 的模板（如 `cutlass::gemm::device::Gemm<...>` 或 3.x 的 `GemmUniversalAdapter<...>`）填入一组具体模板参数后，得到的一个**可编译、可调用的具体内核**。一个实例 = 一组配置。
- **实例库（Instance Library）**：CUTLASS 把成千上万个实例的「实例化代码」批量生成、编译、打包成一个动态/静态库（`libcutlass.so` / `libcutlass.a`），并给每个实例起一个**唯一的过程名**。profiler 就是这个库的一个「货架前台」，按名字取货、运行、计时。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/README.md) | 「Performance Profiling」一节，给出构建 profiler 与筛选子集内核的官方示例命令。 |
| [media/docs/cpp/profiler.md](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/media/docs/cpp/profiler.md) | profiler 的完整手册：所有命令行选项、GEMM/Conv 参数、3.x 内核命名规则、`CUTLASS_LIBRARY_INSTANTIATION_LEVEL` 四位数码机制。 |
| [tools/profiler/CMakeLists.txt](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/tools/profiler/CMakeLists.txt) | 构建 `cutlass_profiler` 可执行文件，列出它的所有源文件，并把它链接到 `cutlass_lib`。 |
| [tools/library/CMakeLists.txt](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/tools/library/CMakeLists.txt) | 实例库的构建中枢：在配置期调用 `generator.py`，回写并 `include` 生成的 `manifest.cmake`，编译出 `cutlass_library`。 |
| [python/cutlass_library/generator.py](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/python/cutlass_library/generator.py) | 代码生成器入口脚本（`__main__`）：按架构调用 `GenerateSMxx`，把候选内核塞进 `Manifest`，最后 `emit` 成源文件与 cmake 片段。 |
| [python/cutlass_library/manifest.py](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/python/cutlass_library/manifest.py) | `Manifest` 类：持有所有候选内核，用 `filter()` 按 `--kernels/--ignore-kernels/--exclude-kernels` 与架构做筛选，`emit_manifest_cmake()` 写出 cmake 片段。 |
| [python/cutlass_library/gemm_operation.py](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/python/cutlass_library/gemm_operation.py) | `GemmOperation.procedural_name()`：按模板拼出内核过程名（2.x 与 3.x 两套模板）。 |
| [tools/library/include/cutlass/library/singleton.h](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/tools/library/include/cutlass/library/singleton.h) | 运行时单例：进程内唯一的 `Manifest` + `OperationTable`，profiler 启动时从这里按名查内核。 |

> 说明：本讲原计划规格里提到的 `python/cutlass_library/gemm_math.py` 在当前 HEAD 中**并不存在**（该目录下与「math/类型」相关的逻辑实际分散在 `gemm_operation.py`、`library.py` 等文件里）。为保证引用真实，本讲改用上述实际存在的文件。这也是 CUTLASS 这类大型项目源码演进中的常态——动手前务必先 `ls`/`grep` 确认文件是否还在。

## 4. 核心概念与源码讲解

### 4.1 CUTLASS Profiler 用法

#### 4.1.1 概念说明

`cutlass_profiler` 是一个**命令行驱动的测试与性能测量环境**。它本身不做 GEMM 计算，而是充当实例库的「前台」：你告诉它「跑哪些内核、什么问题形状（M/N/K）」，它就负责

1. 在运行时**按名字**从实例库里取出对应内核；
2. 按你指定的分布（uniform/gaussian/sequential）**初始化**输入矩阵 A/B/C；
3. **运行** CUTLASS 内核；
4. 可选地用 **cuBLAS / cuDNN / 设备端参考实现**做**正确性验证**（disposition: Passed/Failed）；
5. **计时**并报告 Runtime(ms)、Memory(GiB/s)、Math(GFLOP/s)。

它由 [tools/profiler/CMakeLists.txt](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/tools/profiler/CMakeLists.txt) 构建，源文件清单涵盖了每种算子的「profiler」：

```cpp
// tools/profiler/CMakeLists.txt#L35-L59（节选）
set(CUTLASS_TOOLS_PROFILER_SOURCES
  src/main.cpp
  src/cutlass_profiler.cu
  src/options.cu
  ...
  src/gemm_operation_profiler.cu
  src/grouped_gemm_operation_profiler.cu
  src/block_scaled_gemm_operation_profiler.cu
  src/conv2d_operation_profiler.cu
  ...
)
```

每多支持一类算子（如 BlockScaledGemm），就多一个 `*_operation_profiler.cu`。注意它在链接期**依赖 `cutlass_lib`**（即实例库本体）与可选的 cuBLAS/cuDNN：

```cmake
# tools/profiler/CMakeLists.txt#L86-L95（节选）
target_link_libraries(
  cutlass_profiler
  PRIVATE
  cutlass_lib
  ...
  $<$<BOOL:${CUTLASS_ENABLE_CUBLAS}>:nvidia::cublas>
  ...
  )
```

没有 `cutlass_lib`，profiler 就是个空壳——里面没有任何内核可跑。这正是「profiler 建立在实例库之上」的体现。

#### 4.1.2 核心流程

profiler 一次运行的执行过程可概括为：

```text
cutlass_profiler --operation=Gemm --kernels=<glob> --m=.. --n=.. --k=..
        │
        ▼
1. Singleton::get()  ──►  进程内唯一的 Manifest + OperationTable
        │                      （由 cutlass_lib 在加载时自注册填充）
        ▼
2. 按 --operation 选 OperationProfiler（如 GemmOperationProfiler）
        │
        ▼
3. 用 --kernels 的通配符在 OperationTable 里筛出一批内核名
        │
        ▼
4. 对「每个内核 × 每个问题形状（M,N,K 的笛卡尔积）」：
     a. 分配并初始化 A/B/C/D 显存（按 --dist 分布）
     b. warmup-iterations 次预热
     c. profiling-iterations 次（或 profiling-duration 内）循环启动内核并计时
     d. 用 cuBLAS/cuDNN 等验证 D 的正确性（--verification-enabled）
     e. 打印一个结果块 + 累计到 CSV
        │
        ▼
5. 输出 GFLOP/s（Math 一行）与可选的 --output=report.csv
```

运行时单例 `Singleton` 持有「货架」：

```cpp
// tools/library/include/cutlass/library/singleton.h#L46-L60
class Singleton {
public:
  Manifest manifest;
  OperationTable operation_table;
public:
  Singleton();
  static Singleton const &get();
};
```

`OperationTable` 把所有已注册内核按 (operation_kind, 数据类型, tile, …) 建索引，profiler 的 `--kernels` 通配符就在这张表上做匹配筛选。`Singleton::get()` 返回进程级唯一实例，实例库（`cutlass_lib`）在加载时由生成的 `initialize_all(manifest)` 函数把内核塞进去（见 4.3）。

#### 4.1.3 源码精读：典型输出与命令

profiler 的标准用法在 README 与 profiler.md 中有完整示例。下面这条命令测量一类 Tensor Core FP16 GEMM 内核：

```bash
# README.md#L410-L412 / L507-L511
./tools/profiler/cutlass_profiler --kernels=cutlass_tensorop_s*gemm_f16_*_nt_align8 \
                                  --m=3456 --n=4096 --k=4096
```

它会输出一段结构化结果块（节选自 README 实测输出）：

```text
# README.md#L415-L440（节选）
=============================
  Problem ID: 1
        Provider: CUTLASS
   OperationKind: gemm
       Operation: cutlass_tensorop_s1688gemm_f16_256x128_32x2_nt_align8
          Status: Success
    Verification: ON
     Disposition: Passed
          cuBLAS: Passed
       Arguments: --gemm_kind=universal --m=3456 --n=4096 --k=4096 --A=f16:column \
                  --B=f16:row --C=f32:column --alpha=1 --beta=0 ... --inst_m=16 \
                  --inst_n=8 --inst_k=8 --min_cc=75 --max_cc=1024
           Bytes: 118489088  bytes
           FLOPs: 115992428544  flops
         Runtime: 1.55948  ms
          Memory: 70.7616 GiB/s
            Math: 74378.8 GFLOP/s
```

要点：

- **`Operation`** 一行就是被测内核的**过程名**（见 4.2 解读）。
- **`Disposition: Passed` + `cuBLAS: Passed`** 表示与 cuBLAS 比对通过（`--verification-providers=cublas`，默认开启）。
- **`Math: 74378.8 GFLOP/s`** 就是性能数字；GFLOP/s 由 `FLOPs / Runtime` 算出，其中 FLOPs = \(2MNK\)（一次 GEMM 的乘加浮点量）。
- **`Arguments`** 一行是 profiler 自动**回显的完整参数**，可直接复制成下次命令行，便于复现某一具体内核。

profiler 支持的关键运行期选项（来自 [media/docs/cpp/profiler.md](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/media/docs/cpp/profiler.md) 的 `--help` 输出，L174-L324）：

| 选项 | 作用 |
| --- | --- |
| `--operation=<kind>` | 选算子：`Gemm`/`Conv2d`/`Conv3d`/`SparseGemm`/`RankK`/`Trmm`/`Symm`/`GroupedGemm` 等。 |
| `--kernels=<glob>` | **运行时**按过程名通配符筛选内核（与构建期 `CUTLASS_LIBRARY_KERNELS` 是两套独立过滤，但语法一致）。 |
| `--m / --n / --k` | 问题形状；支持单值、逗号列表、`start:end:increment` 区间，做 schmoo 扫描。 |
| `--A=f16:column --B=f16:row` | 指定数据类型 + 布局（`row`/`column`）。 |
| `--profiling-iterations=<N>` | 计时迭代次数；为 0 则按 `--profiling-duration` 计时。 |
| `--verification-providers=cublas` | 用哪些参考实现验证（GEMM 默认 cuBLAS，Conv 默认 cuDNN）。 |
| `--output=report.csv` | 把所有结果写成 CSV，便于做透视表/画图。 |
| `--use-cuda-graphs=<bool>` | 用 CUDA Graph 启动，消除 launch 开销对小问题的干扰。 |
| `--enable-kernel-performance-search --sort-results-flops-per-sec` | 穷举搜索并按 GFLOP/s 排序，直接给出「最快内核」。 |

#### 4.1.4 代码实践

1. **实践目标**：在没有 GPU 的环境下也能走通「构建 profiler」这条链路；有 GPU 时再补上「测量一个 FP16 内核的 GFLOP/s」。
2. **操作步骤**：
   - 在仓库根目录新建空构建目录：`mkdir build && cd build`。
   - 配置（先用最小子集，详见 4.4）：`cmake .. -DCUTLASS_NVCC_ARCHS='80' -DCUTLASS_LIBRARY_KERNELS=cutlass_tensorop_s*gemm_f16_*_nt_align8`。
   - 编译 profiler：`make cutlass_profiler -j$(nproc)`。
   - 运行（需要 GPU 与匹配的 SM80）：`./tools/profiler/cutlass_profiler --operation=Gemm --kernels=cutlass_tensorop_s*gemm_f16_*_nt_align8 --m=3456 --n=4096 --k=4096`。
3. **需要观察的现象**：profiler 会为**每一个**通过过滤的内核打印一个上文那样的结果块；若你的过滤表达式命中多个 tile（如 128x128、256x128），会看到多个块依次输出，最后还有一张汇总 CSV 表。
4. **预期结果**：每个块的 `Disposition` 为 `Passed`、`cuBLAS: Passed`，`Math` 一行给出该内核的 GFLOP/s；不同 tile 的内核 GFLOP/s 不同——这正是 profiler 选型价值的体现。
5. **若无法确定运行结果**：在无 GPU 环境下，`make cutlass_profiler` 能成功链接出可执行文件即说明「实例库生成 + 编译」链路通畅；实际 GFLOP/s 数字**待本地验证**（依赖具体 GPU 型号与频率）。

#### 4.1.5 小练习与答案

- **练习 1**：profiler 报告里 `FLOPs: 115992428544` 对应 `--m=3456 --n=4096 --k=8192`，请用手算验证它是 \(2MNK\)。
  - **答案**：\(2 \times 3456 \times 4096 \times 8192 = 231{,}956{,}545{,}536\)，与 profiler 输出一致（这是 README Tensor Core 示例的形状）。
- **练习 2**：`--m=1024:4096:256` 表示什么？
  - **答案**：M 从 1024 到 4096、步长 256，即 M ∈ {1024, 1280, …, 4096}，profiler 会对该区间内每个 M 都跑一遍（schmoo 扫描）。

---

### 4.2 kernel 过滤与命名规则

#### 4.2.1 概念说明

要在成千上万个实例里「只挑我想要的那几个」，必须先有一套**唯一且可读的名字**，再有一套**通配符匹配规则**。CUTLASS 的做法是：

- 每个实例在生成时由 `GemmOperation.procedural_name()` 拼出一个**过程名**，这个名字把「架构/算子类/数据类型/tile/cluster/布局/对齐/调度」全部编码进去，做到**见名知配置**。
- 构建期用 CMake 变量 `CUTLASS_LIBRARY_KERNELS`、运行期用 profiler 选项 `--kernels`，两者共用同一套**含 `*` 的通配符语法**对过程名做子串匹配。

#### 4.2.2 核心流程：过程名是怎么拼出来的

过程名的拼接逻辑在 [python/cutlass_library/gemm_operation.py](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/python/cutlass_library/gemm_operation.py#L361-L392)：

```python
# gemm_operation.py#L361-L392（节选）
@functools.cached_property
def _procedural_name(self):
  opcode_class_name = OpcodeClassNames[self.tile_description.math_instruction.opcode_class]
  if self.arch >= 90:
    # ---- CUTLASS 3.x（Hopper 及以后）----
    kernel_name_template = "cutlass{p}_sm{ar}_{op}_{ex}{ct}{cs}_{l}_{s}_align{al}{t}{k}{e}"
    tile_shape = self.get_collective_tile_shape()
    return kernel_name_template.format(
        p = self.prefix,                      # "3x"
        ar = self.arch,                       # 90 / 100 / ...
        op = opcode_class_name,               # tensorop / simt
        ex = self.extended_name_3x(),         # gemm_f16_f16_f32_f16_f32（A_B_Acc_C_D）
        ct = ...,                             # 集体 tile 形状 128x128x64
        cs = ...,                             # cluster 形状 2x1x1
        l = self.tile_description.stages,     # 流水线级数（0 表示自动推断）
        s = self.layout_name_3x(),            # ntn 等（A,B,C 布局）
        al = ...,                             # max(A.alignment, B.alignment)
        t = ..., k = ..., e = ...)            # tile scheduler / 主循环 / epilogue 调度后缀
  else:
    # ---- CUTLASS 2.x（Ampere 及以前）----
    threadblock = self.tile_description.procedural_name()
    return "cutlass{p}_{op}_{ex}_{tb}_{l}_align{a}".format(
        p = self.prefix,                      # 空（2.x 无 "3x"）
        op = opcode_class_name,               # tensorop / simt
        ex = self.extended_name(),            # s16816gemm_f16（指令形状+数据类型）
        tb = threadblock,                     # 256x128_32x3（CTA tile + stages）
        l = self.layout_name(),               # nn / nt / tn / tt
        a = ...)                              # 对齐
```

两套模板对应两类名字：

- **2.x 名字**：`cutlass_tensorop_s16816gemm_f16_256x128_32x3_nn_align8`
  - `tensorop` 用 Tensor Core（`simt` 则用 CUDA Core）；`s16816` 指令形状 16×8×16、`s` 单精度累加；`f16` 输入类型；`256x128_32x3` 是 CTA tile 256×128、K=32、3 级流水；`nn` 表示 A、B 都列主序；`align8` 最大对齐 8。
- **3.x 名字**（[profiler.md#L582-L606](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/media/docs/cpp/profiler.md#L582-L606)）：`cutlass3x_sm90_tensorop_gemm_f16_f16_f32_f16_f32_128x128x64_2x1x1_0_ntn_align8`
  - `cutlass3x` 标识 3.x API；`sm90` 架构；`gemm_f16_f16_f32_f16_f32` 依次是 A、B、Accumulator、C、D 的类型；`128x128x64` 集体 tile；`2x1x1` cluster；`0` 流水线级数占位（0 = 让 CollectiveBuilder 自动算）；`ntn` 是 A/B/C 布局（n=column/t=row）；`align8` 对齐。

> 关键点：**过程名是配置的「指纹」**。两个实例只要任何一项配置不同（tile、cluster、布局、累加类型……），名字就不同，因此过滤表达式能精确到「一类配置」。

#### 4.2.3 源码精读：通配符匹配规则

过滤的真正算法在 [python/cutlass_library/manifest.py](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/python/cutlass_library/manifest.py#L615-L623)：

```python
# manifest.py#L615-L623
def _filter_string_matches(self, filter_string, haystack):
  ''' Returns true if all substrings appear in the haystack in order'''
  substrings = filter_string.split('*')
  for sub in substrings:
    idx = haystack.find(sub)
    if idx < 0:
      return False
    haystack = haystack[idx + len(sub):]
  return True
```

它的语义是「**把过滤串按 `*` 切成若干子串，要求它们按顺序依次出现在过程名里**」。注意几个反直觉点：

- **不是正则**，`*` 就是「任意字符（含空）」，没有 `?`、没有字符类。
- **只要求「按顺序出现」，不锚定首尾**。`s*gemm_f16` 能匹配 `cutlass_tensorop_s16816gemm_f16_...`，因为 `s`、`gemm_f16` 按序出现即可。
- 多个过滤串之间是**逗号分隔的「或」关系**（见 4.3 的 `kernel_names` 循环），任一命中即保留。

举几个例子（过程名 = `cutlass_tensorop_s1688gemm_f16_256x128_32x2_nt_align8`）：

| 过滤串 | 是否命中 | 原因 |
| --- | --- | --- |
| `s*gemm_f16` | ✅ | `s` → `1688`，`gemm_f16` 按序出现 |
| `*nt_align8` | ✅ | 末尾子串 `nt_align8` 出现 |
| `nt_align8*` | ✅ | 末尾 `*` 切出空串，恒成立（等价于含 `nt_align8`） |
| `f32` | ❌ | 该内核是 f16 输入，过程名里没有 `f32` |
| `s*nt` | ✅ | `s`、`nt` 按序出现 |

#### 4.2.4 代码实践

1. **实践目标**：在不构建的情况下，凭过程名反推内核配置，并自检过滤表达式是否正确。
2. **操作步骤**：
   - 取一条 3.x 名字：`cutlass3x_sm90_tensorop_gemm_f16_f16_f32_f16_f32_128x128x64_2x1x1_0_ntn_align8`。
   - 逐段标注：`cutlass3x` / `sm90` / `tensorop` / `gemm` / `f16(A) f16(B) f32(Acc) f16(C) f32(D)` / `tile 128×128×64` / `cluster 2×1×1` / `stages=0(自动)` / `ntn(A=col,B=row,C=col)` / `align8`。
   - 写出「只挑 A=列主序、B=行主序（即 nt）的 FP16、单精度累加 Tensor Core 内核」的过滤串：`cutlass_tensorop_s*gemm_f16_*_nt_align8`（2.x）或 `cutlass3x_sm*_tensorop_gemm_f16_f16_f32_*_ntn_align8`（3.x）。
3. **需要观察的现象**：把同一过滤串分别用于**构建期**（`-DCUTLASS_LIBRARY_KERNELS=...`）和**运行期**（`--kernels=...`），命中范围应当一致——因为二者底层都走 `_filter_string_matches`。
4. **预期结果**：能准确说出每个过滤串命中了哪些配置段。
5. **若无法确定运行结果**：可对照 profiler 实际输出里的 `Operation:` 行人工核验；纯逻辑题，不依赖 GPU。

#### 4.2.5 小练习与答案

- **练习 1**：写一个过滤串，匹配「所有 Blackwell SM100、FP16 输入、FP32 累加的 3.x GEMM 内核」。
  - **答案**：`cutlass3x_sm100_tensorop_gemm_f16_f16_f32_*`（A/B 都是 f16、Acc 是 f32；`*` 兜底 tile/cluster/布局）。
- **练习 2**：`cutlass_tensorop_s*gemm_f16_*_nt_align8` 中的两个 `*` 分别吞掉了什么？
  - **答案**：第一个 `*` 吞掉指令形状（如 `1688`/`16816`）与可能的 `gemm` 前缀；第二个 `*` 吞掉 tile 形状（如 `256x128_32x2`）。整体即「任意 FP16 单精度累加 Tensor Core、NT 布局、8 字节对齐」。

---

### 4.3 实例库的代码生成机制

#### 4.3.1 概念说明

CUTLASS 不可能手写上万个内核的实例化代码，而是用 **Python 在 CMake 配置期「现写现编译」**：

- `python/cutlass_library/generator.py` 是**生成器**，它枚举「架构 × 数据类型 × tile × cluster × 调度」的组合，为每个组合构造一个 `GemmOperation` 等对象。
- `python/cutlass_library/manifest.py` 的 `Manifest` 类负责**收口与筛选**：只有通过 `filter()` 的内核才被保留，并生成对应的 `.cu` 源文件 + 一个总的 `manifest.cmake`。
- `tools/library/CMakeLists.txt` 在配置期用 `execute_process` 跑这个生成器，再 `include` 生成的 `manifest.cmake`，把这些源文件挂到 `cutlass_library` 目标上编译。

这套机制的妙处在于：**「生成哪些内核」完全由命令行参数（`--kernels` 等）决定，源码里不固化任何内核清单**。换个过滤串，重新 cmake，就得到一个全新的库。

#### 4.3.2 核心流程

```text
cmake 配置期（tools/library/CMakeLists.txt）
   │  execute_process( python generator.py
   │      --kernels "${CUTLASS_LIBRARY_KERNELS}"
   │      --architectures "${CUTLASS_NVCC_ARCHS}"
   │      --ignore-kernels / --exclude-kernels / --kernel-filter-file ... )
   ▼
generator.py __main__
   │  manifest = Manifest(args)
   │  for each arch: GenerateSMxx(manifest, cuda_version)   # 构造 GemmOperation 并 manifest.append
   │  manifest.emit(GeneratorTarget.Library)                # 写 generated/*.cu + initialize_all.cpp
   ▼
Manifest.filter(operation)  ──  逐内核判定去留：
   │  ① 架构区间（min_cc ≤ 目标 cc ≤ max_cc，且共享内存够用）
   │  ② operation kind（--operations）
   │  ③ 去重（procedural_name 唯一）
   │  ④ include 列表 kernel_names（CUTLASS_LIBRARY_KERNELS，含 'all'/'' 语义）
   │  ⑤ ignore 列表（仅当 include 非空生效）
   │  ⑥ kernel-filter-file（正则白名单）
   │  ⑦ exclude 列表（恒生效，连 filter-file 也能排除）
   ▼
manifest.cmake 被回写并 include() 进 CMake
   │  cutlass_target_sources(cutlass_library_objs PRIVATE  <generated/*.cu>)
   │  cutlass_add_cutlass_library(SUFFIX gemm_sm80_...) <同批源>
   ▼
编译期：生成出 cutlass_library（.so/.a），
        每个 .cu 内的 initialize_all_<kind>_operations(manifest) 把内核注册进 Manifest
   ▼
运行期：cutlass_profiler 启动 → Singleton::get().operation_table 取内核
```

#### 4.3.3 源码精读

**(a) CMake 在配置期调用生成器**——这是整个闭环的入口：

```cmake
# tools/library/CMakeLists.txt#L342-L379（节选）
execute_process(
  WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}/../../python/cutlass_library
  COMMAND ${CMAKE_COMMAND} -E env PYTHONPATH=...
    ${Python3_EXECUTABLE} ${CUTLASS_SOURCE_DIR}/python/cutlass_library/generator.py
    --operations "${CUTLASS_LIBRARY_OPERATIONS}"
    --build-dir ${PROJECT_BINARY_DIR}
    --curr-build-dir ${CMAKE_CURRENT_BINARY_DIR}
    --generator-target library
    --architectures "${CUTLASS_NVCC_ARCHS_ENABLED}"
    --kernels "${CUTLASS_LIBRARY_KERNELS}"                 # ← 关键：include 过滤
    --instantiation-level "${CUTLASS_LIBRARY_INSTANTIATION_LEVEL}"
    --ignore-kernels "${CUTLASS_LIBRARY_IGNORE_KERNELS}"   # ← 仅当 kernels 非空生效
    --exclude-kernels "${CUTLASS_LIBRARY_EXCLUDE_KERNELS}" # ← 恒生效
    --kernel-filter-file "${KERNEL_FILTER_FILE}"
    --selected-kernel-list "${CUTLASS_LIBRARY_GENERATED_KERNEL_LIST_FILE}"
    ...
)
...
set(CUTLASS_LIBRARY_MANIFEST_CMAKE_FILE ${CMAKE_CURRENT_BINARY_DIR}/generated/manifest.cmake)
if(EXISTS "${CUTLASS_LIBRARY_MANIFEST_CMAKE_FILE}")
  include(${CUTLASS_LIBRARY_MANIFEST_CMAKE_FILE})          # ← 把生成结果接回 CMake
endif()
```

注意 `execute_process` 是在 **`cmake ..` 配置阶段**同步执行的——所以你会看到配置时屏幕上刷过 `Completed generation of library instances.`，并能在 `build/tools/library/library_instance_generation.log` 查到「哪些内核被生成/被剔除」的明细。

**(b) 生成器主流程**——按架构生成，再统一 emit：

```python
# generator.py#L12573-L12622（节选）
if __name__ == "__main__":
  parser = define_parser()
  args = parser.parse_args()
  ...
  manifest = Manifest(args)
  ...
  GenerateSM50(manifest, args.cuda_version)
  GenerateSM60(manifest, args.cuda_version)
  ...
  GenerateSM80(manifest, args.cuda_version)
  GenerateSM90(manifest, args.cuda_version)
  ...
  if blackwell_enabled_arch:
    GenerateSM100(manifest, args.cuda_version)
    GenerateSM120(manifest, args.cuda_version)

  if 'library' in args.generator_target.split(','):
    manifest.emit(GeneratorTarget.Library)
  ...
  if args.selected_kernel_list is not None:
    # 把本次实际选中的内核名清单写到 generated_kernels.txt
    with open(args.selected_kernel_list, 'w') as file_writer:
      for line in manifest.selected_kernels:
        file_writer.write("%s\n" % line)
```

每个 `GenerateSMxx` 内部会构造大量 `GemmOperation(...)` 并 `manifest.append(op)`；`append` 调 `filter` 决定保留与否（见 4.2 的 `_filter_string_matches`）。命令行参数定义里，`--kernels` 的语义被明确写成「`all`=全部、留空=仅默认子集、否则=逗号分隔过滤串」：

```python
# generator.py#L12542-L12553（节选）
parser.add_argument("--kernels", default='', help='Comma-delimited list to filter kernels by name.  '
                    'Specifying this as "all" includes ALL the kernels, '
                    'while not specifying this includes only the default set of kernels.')
parser.add_argument("--ignore-kernels", default='', ... # 仅当 --kernels 非空才生效
parser.add_argument("--exclude-kernels", default='', ... # 恒生效，可排除 filter-file 命中的内核
```

**(c) Manifest 筛选与去重**——这是 `CUTLASS_LIBRARY_KERNELS` 真正落地的地方：

```python
# manifest.py#L626-L697（filter，节选关键判定）
def filter(self, operation):
  # ① 架构区间 + 共享内存够用
  enabled = not (self.filter_by_cc)
  for cc in self.compute_capabilities_baseline:
    if cc >= operation.tile_description.minimum_compute_capability and \
       cc <= operation.tile_description.maximum_compute_capability and \
       (cc not in SharedMemPerCC or SharedMemPerCC[cc] >= CalculateSmemUsage(operation)):
      enabled = True; break
  if not enabled: return False
  # ② operation kind
  if len(self.operations_enabled) and not operation.operation_kind in self.operations_enabled:
    return False
  name = operation.procedural_name()
  # ③ 去重
  if name in self.operations_by_name.keys(): return False
  # ④ include 列表（CUTLASS_LIBRARY_KERNELS）
  if len(self.kernel_names):
    enabled = False
    for name_substr in self.kernel_names:                 # 逗号分隔 = 或
      if self._filter_string_matches(name_substr, name):
        enabled = True; break
    # ⑤ ignore 列表（仅当 include 非空时才看）
    for name_substr in self.ignore_kernel_names:
      if self._filter_string_matches(name_substr, name):
        enabled = False; break
  # ⑥ kernel-filter-file（正则白名单，可反向剔除）
  ...
  # ⑦ exclude 列表（恒生效）
  for name_substr in self.exclude_kernel_names:
    if self._filter_string_matches(name_substr, name):
      enabled = False; break
  return enabled
```

`Manifest.__init__` 把 CMake 透传过来的字符串解析成列表，并对 `--kernels` 做特殊处理：值为 `all` 时 `kernel_names` 留空（=不过滤=全要），否则按逗号切分：

```python
# manifest.py#L542-L548
if args.kernels == 'all':
  self.kernel_names = []
else:
  self.kernel_names = [x for x in args.kernels.split(',') if x != '']
self.ignore_kernel_names = [x for x in args.ignore_kernels.split(',') if x != '']
self.exclude_kernel_names = [x for x in args.exclude_kernels.split(',') if x != '']
```

**(d) 回写 cmake 片段**——把选中内核的源文件挂到编译目标：

```python
# manifest.py#L735-L758（emit_manifest_cmake，节选）
def emit_manifest_cmake(self, manifest_path, top_level_path, source_files):
  with open(manifest_path, "w") as manifest_file:
    manifest_file.write("cutlass_target_sources(cutlass_library_objs PRIVATE\n    ...\n)\n\n")
    for kind in self.operations.keys():
      ...  # all_<kind>_operations.cu
    for kind in self.operations.keys():
      for min_cc in sorted(self.operations[kind].keys()):
        for subclass in sorted(source_files[kind][min_cc].keys()):
          manifest_file.write(f"cutlass_add_cutlass_library(\n  SUFFIX {kind}_sm{min_cc}_{subclass}\n")
          for source_file in source_files[kind][min_cc][subclass]:
            manifest_file.write("    %s\n" % source_file)
```

这里的 `cutlass_add_cutlass_library` 正是 [tools/library/CMakeLists.txt#L89-L236](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/tools/library/CMakeLists.txt#L89-L236) 定义的那个函数：它把一批 `.cu` 编成 `OBJECT` 库，再聚合成共享库 `cutlass_library` 与静态库 `cutlass_library_static`（带 `SUFFIX` 时拆成多个子库，最后 link 进主库）。

**(e) 运行时注册**——生成的 `initialize_all.cpp` 调用每个算子类别的初始化函数，把内核塞进 `Manifest`（即 4.1 里 `Singleton::get()` 持有的那张表）：

```python
# manifest.py#L425-L436（生成的 initialize_all.cpp 模板）
self.top_level_initialize_kind = '''
        void initialize_all_${kind}_operations(Manifest &manifest) {
${fn_calls}
        }'''
self.top_level_initialize = '''
        void initialize_all(Manifest &manifest) {
            manifest.reserve(${operation_count});
${fn_calls}
        }'''
```

每个 `initialize_all_<kind>_operations` 内部会 `new` 出 `library::Operation` 并 `manifest.append(...)`，profiler 启动时一次性调完 `initialize_all`，整个货架就摆好了。

#### 4.3.4 代码实践

1. **实践目标**：亲眼看到「生成器在配置期产出了哪些文件、选中了哪些内核」。
2. **操作步骤**：
   - 用极小过滤串配置：`cmake .. -DCUTLASS_NVCC_ARCHS='80' -DCUTLASS_LIBRARY_KERNELS=cutlass_simt_sgemm_128x128_8x2_nn_align1`。
   - 配置完成后，查看选中清单：`cat build/tools/library/generated_kernels.txt`。
   - 查看生成器日志：`cat build/tools/library/library_instance_generation.log | head -50`，体会「included due to / NOT included due to」的判定。
   - 查看回写的 cmake 片段：`find build -name manifest.cmake -path '*library*'`，确认它 `cutlass_add_cutlass_library(SUFFIX gemm_sm80_...)` 并列出源文件。
3. **需要观察的现象**：`generated_kernels.txt` 应只有一行（你指定的那个内核）；`manifest.cmake` 里只引用了与之相关的少量 `.cu`；对比「不设 `CUTLASS_LIBRARY_KERNELS`（默认子集）」时文件会大很多。
4. **预期结果**：过滤串越窄，生成的 `.cu` 越少，`make cutlass_profiler` 越快。
5. **若无法确定运行结果**：文件名与路径以本机 build 树为准；如生成清单为空，检查过滤串是否拼错（参考 4.2.3 的匹配语义）。

#### 4.3.5 小练习与答案

- **练习 1**：为什么 `--ignore-kernels` 只在 `--kernels` 非空时才生效，而 `--exclude-kernels` 恒生效？
  - **答案**：见 [manifest.py#L683-L694](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/python/cutlass_library/manifest.py#L683-L694) 的注释——`ignore` 是历史选项，为不破坏向后兼容（旧脚本依赖「不设 kernels 时得到默认子集」），它的判定被包在 `if len(self.kernel_names)` 里；`exclude` 是后引入的新选项，专门用来在任意情况下都能剔除（连 filter-file 命中的也能排除）。
- **练习 2**：`generator.py` 里为什么要把生成结果回写成 `manifest.cmake` 再 `include`，而不是直接在 CMake 里 `add_library`？
  - **答案**：因为源文件清单是 Python 在配置期**动态算出来**的（依赖过滤结果与 CUDA 版本裁剪），CMake 静态写不死；回写一个 `.cmake` 片段再 `include`，是让动态产物接入 CMake 构建系统的标准做法。

---

### 4.4 选择性编译以减少耗时

#### 4.4.1 概念说明

实例库最大的痛点是**编译时间**。CUTLASS 的配置空间巨大：仅 Hopper（SM90）一种架构，把所有 tile/cluster/schedule 组合展开就是「**数百万级**」内核（见 [profiler.md#L36-L43](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/media/docs/cpp/profiler.md#L36-L43)）。全量构建既慢（数小时甚至编译失败），产物二进制也大到可能触发链接器上限。

CUTLASS 提供**四个互相正交的「减负」手段**：

1. **过滤编译**（`CUTLASS_LIBRARY_KERNELS`）：只生成名字匹配的内核——最常用、效果最猛。
2. **实例化级别**（`CUTLASS_LIBRARY_INSTANTIATION_LEVEL`）：用一个四位数码精细控制「展开多少组合」，专门为 SM90/SM100 设计。
3. **Unity Build**（`CUTLASS_UNITY_BUILD_ENABLED`）：把多个内核塞进同一个编译单元，减小二进制、绕开链接器限制。
4. **默认子集**：什么都不设时，生成器只产出「每类一个 tile」的极小子集，保证开箱即用。

#### 4.4.2 核心流程：实例化级别（四位数码）

`CUTLASS_LIBRARY_INSTANTIATION_LEVEL` 是一个**四位十进制整数**（不足四位左补零），从右到左四位依次控制（SM90 版本，见 [profiler.md#L53-L94](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/media/docs/cpp/profiler.md#L53-L94)）：

| 位（从右起） | 控制维度 | 含义 |
| --- | --- | --- |
| 第 0 位 | 指令形状（Instruction Shape） | 0=仅默认；1=加 TF32；2=2 的幂；3=其余 |
| 第 1 位 | MMA 形状倍率（MMA Multiplier） | 0~3 递增；9=穷举 |
| 第 2 位 | Cluster 形状 | 0=仅(1,2,1)；越大可选 CTA 数越多（1→2→4→8→16） |
| 第 3 位 | 调度剪枝（Schedule Pruning） | 0=按 generator 默认剪枝；≥1=不剪枝 |

例如 `500` 补零为 `0500`，表示：指令形状 level 0（默认）、MMA 倍率 level 0（仅 `(2,1,4)`）、Cluster level 5（1/2/4/8/16 CTA）、调度 level 0（默认剪枝）。级别越高，生成的内核越多、编译越久。Manifest 里对应的取值逻辑：

```python
# manifest.py#L573-L591
def get_instantiation_level(self, pruned_level=0, default_level=111, exhaustive_level=9992):
  # 0 = 0000 生成最少；9999 生成所有组合
  if self.instantiation_level > 0:
    return self.instantiation_level           # 用户显式指定的四位数码
  elif self.is_kernel_filter_set_to_all:      # instantiation_level=='max' 且 kernels 非空
    return exhaustive_level
  elif self.kernel_filter == '':              # 没设 kernels
    return pruned_level                       # 最小剪枝集
  else:
    return default_level                      # 设了 kernels 但没设级别 → 中等默认
```

注意 `is_kernel_filter_set_to_all`（[manifest.py#L561](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/python/cutlass_library/manifest.py#L561)）要求「`instantiation-level=max` **且** `kernels` 非空」——文档明确说明：SM90/SM100 下 `CUTLASS_LIBRARY_KERNELS` **必须非空**，否则光是生成与过滤这些内核就要花数小时。

#### 4.4.3 源码精读：典型「减负」命令

README 给出的「只编译一个 SGEMM 内核」是过滤编译的极简例子，把构建压到秒级：

```bash
# README.md#L450-L455
cmake .. -DCUTLASS_NVCC_ARCHS='75;80' \
         -DCUTLASS_LIBRARY_KERNELS=cutlass_simt_sgemm_128x128_8x2_nn_align1
make cutlass_profiler -j16
```

而「Hopper 上展开更多组合」则需要实例化级别配合 Unity Build：

```bash
# profiler.md#L45-L51
cmake .. \
  -DCUTLASS_NVCC_ARCHS="90a" \
  -DCUTLASS_LIBRARY_KERNELS="cutlass3x_sm90_tensorop_gemm_f16_f16_f32_void_f32_*" \
  -DCUTLASS_LIBRARY_INSTANTIATION_LEVEL="max" \
  -DCUTLASS_UNITY_BUILD_ENABLED=ON
```

> ⚠️ 教训性提示（来自 profiler.md）：实例化级别设到 `max` 会展开海量组合，「**并非所有配置都经过测试，部分可能编译失败或在运行时启动失败**」。生产环境应先窄过滤、小级别跑通，再逐步放宽。

#### 4.4.4 代码实践（本讲主实践任务）

> **任务**：用 `CUTLASS_LIBRARY_KERNELS` 过滤，只编译「一类 FP16、单精度累加、NT 布局」的 Tensor Core GEMM 内核，再用 `cutlass_profiler` 测量其 GFLOP/s。

1. **实践目标**：完整走一遍「过滤编译 → 测速」闭环，体会选择性编译对耗时的量级影响。
2. **操作步骤**：
   - 清空并新建 build：`rm -rf build && mkdir build && cd build`。
   - **窄过滤构建**：`cmake .. -DCUTLASS_NVCC_ARCHS='80' -DCUTLASS_LIBRARY_KERNELS='cutlass_tensorop_s*gemm_f16_*_nt_align8'`，记录 `time make cutlass_profiler -j$(nproc)` 的耗时 \(T_1\)。
   - 测速：`./tools/profiler/cutlass_profiler --operation=Gemm --kernels='cutlass_tensorop_s*gemm_f16_*_nt_align8' --m=3456 --n=4096 --k=4096 --output=fp16_report.csv`。
   - （可选对比）再清空 build，**不设** `CUTLASS_LIBRARY_KERNELS`（默认子集），同样 `time make cutlass_profiler`，记录耗时 \(T_2\)，比较 \(T_1\) 与 \(T_2\)。
3. **需要观察的现象**：
   - `generated_kernels.txt` 里只列出 FP16 NT 的少量内核。
   - 窄过滤下 `make` 明显更快（量级差异，常达数十倍）。
   - profiler 每个 kernel 块的 `Math` 行给出 GFLOP/s；`fp16_report.csv` 汇总所有 tile 的性能，可据此**选 GFLOP/s 最高的内核**作为最终配置。
4. **预期结果**：得到一张「内核名 → GFLOP/s」的对照表，能直接挑出该问题形状下最快的 tile。例如 README 同类示例给出 `cutlass_tensorop_s1688gemm_f16_256x128_32x2_nt_align8` 在 3456×4096×4096 上约 **74378.8 GFLOP/s**（Ampere，具体值依硬件）。
5. **若无法确定运行结果**：编译耗时 \(T_1, T_2\) 与 GFLOP/s 绝对值**待本地验证**（取决于 CPU、GPU、CUDA 版本）；但「窄过滤更快、报告能选出最优 tile」这一**相对结论**是确定的。

#### 4.4.5 小练习与答案

- **练习 1**：实例化级别 `0500` 在 SM90 下表示什么？
  - **答案**：指令形状 level 0（仅默认）、MMA 倍率 level 0（仅 `(2,1,4)`）、Cluster level 5（1/2/4/8/16 CTA）、调度 level 0（默认剪枝）。见 [profiler.md#L89-L94](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/media/docs/cpp/profiler.md#L89-L94)。
- **练习 2**：为什么 SM90 下官方强制 `CUTLASS_LIBRARY_KERNELS` 非空？
  - **答案**：SM90 的配置组合达百万级，即便只做「生成 + 过滤」（还没编译）就要数小时；必须先用 `--kernels` 收窄候选集，生成器才跑得动（见 [profiler.md#L40-L43](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/media/docs/cpp/profiler.md#L40-L43)）。
- **练习 3**：`CUTLASS_UNITY_BUILD_ENABLED=ON` 解决什么问题？
  - **答案**：把多个内核实例放进同一编译单元，**减小最终二进制体积**，避免某些平台因库太大触发「relocation truncated to fit」等链接器限制（[tools/library/CMakeLists.txt#L126-L137](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/tools/library/CMakeLists.txt#L126-L137) 还专门为大于 2GB 的库加了 `-mcmodel=medium`）。

## 5. 综合实践

把本讲四个模块串起来，完成一次「**选型任务**」：

> 假设你要为某 Attention 计算里的一个固定形状 \(M=N=4096, K=4096\)（FP16 输入、FP32 累加）在 Ampere（SM80）上选一个最快的 CUTLASS 内核。请：

1. **生成候选**：用 `CUTLASS_LIBRARY_KERNELS='cutlass_tensorop_s*gemm_f16_*_nt_align8'` 配置并构建 `cutlass_profiler`，把候选 FP16 NT 内核都编译出来。
2. **核对命名**：从 `generated_kernels.txt` 里挑 3 个名字，按 4.2 的规则逐段标注其配置（指令形状、tile、stages）。
3. **穷举测速**：用 `cutlass_profiler --operation=Gemm --kernels='cutlass_tensorop_s*gemm_f16_*_nt_align8' --m=4096 --n=4096 --k=4096 --output=select.csv`（可加 `--enable-kernel-performance-search --sort-results-flops-per-sec` 直接排序）。
4. **选定并复现**：从 `select.csv` 里取 GFLOP/s 最高的一行，复制其 `Operation` 名与 `Arguments` 行，单独再跑一次确认；这就是你最终选用的内核配置。
5. **反思**：对比「默认子集构建」与「窄过滤构建」的 `make` 耗时，记录量级差异；并解释为什么 CUBLAS 通常「开箱即快」而 CUTLASS 需要 profiler 选型（因为 cuBLAS 内置了启发式选型，CUTLASS 把选型权交给你）。

这个任务覆盖了：**过程名解读（4.2）→ 过滤编译（4.3/4.4）→ profiler 测速（4.1）→ 选型决策** 的完整链路，正是 CUTLASS 工程师日常调优的真实工作流。

## 6. 本讲小结

- `cutlass_profiler` 是实例库的命令行前台：负责**按名取内核 → 初始化 → 运行 → 验证（cuBLAS/cuDNN）→ 计时**，输出 Runtime/Memory/Math(GFLOP/s)。
- 每个内核有唯一的**过程名**，把架构/算子类/数据类型/tile/cluster/布局/对齐/调度全部编码进去；2.x 与 3.x（`cutlass3x_sm90_...`）是两套命名模板。
- 过滤用「按 `*` 切片、子串按序出现」的 `_filter_string_matches` 语义，**不是正则**；构建期 `CUTLASS_LIBRARY_KERNELS` 与运行期 `--kernels` 共用同一套语法与同一份实现。
- 实例库由 Python 生成器 `generator.py` 在 **CMake 配置期**现写：`GenerateSMxx` 构造候选 → `Manifest.filter` 筛选 → `emit` 生成 `.cu` + `manifest.cmake` → 回写 `include` 进 CMake 编译成 `cutlass_library`。
- 运行时由进程级单例 `Singleton::get()`（持 `Manifest` + `OperationTable`）提供内核查询，生成的 `initialize_all(manifest)` 在加载时完成注册。
- 控制编译耗时四大手段：**过滤编译（`CUTLASS_LIBRARY_KERNELS`）**、**实例化级别（四位数码 / `max`）**、**Unity Build**、以及**默认即最小子集**；SM90/SM100 下 `CUTLASS_LIBRARY_KERNELS` 必须非空。
- `--ignore-kernels` 仅在 `--kernels` 非空时生效（向后兼容），`--exclude-kernels` 恒生效（可连 filter-file 一起排除）——这是历史包袱与新选项并存的典型设计。

## 7. 下一步学习建议

- **向上**：若关心 Python 侧的等价能力，可学 u3-l9/u3-l10 的 **CuTe DSL**——它用 Python 直接描述内核，配合 `autotune` 也能做选型，是实例库「预生成 + C++ 编译」之外的另一条产出路径。
- **向深**：想理解「profiler 选出的最优配置」为何最优，回到 u2-l8（CollectiveBuilder 自动推断 tile/stage）、u2-l9（Hopper warp-specialized）与 u3-l1（异步流水线级数如何影响 occupancy）。
- **横向**：实例库不只服务于 profiler——`tools/library/src/handle.cu` 暴露的 `cutlass::library::Handle` 是一个可在自有 C++ 程序里直接调用的运行时句柄，建议阅读它了解「不通过 profiler、直接用实例库」的方式。
- **动手**：尝试写一个自己的 `--kernel-filter-file`（一行一个正则），体会它与 `CUTLASS_LIBRARY_KERNELS` 通配符的差异（前者是**正则** `re.search`，后者是 `*` 子串），见 [manifest.py#L594-L611](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/python/cutlass_library/manifest.py#L594-L611)。
