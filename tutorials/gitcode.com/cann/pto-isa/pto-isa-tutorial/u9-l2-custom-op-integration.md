# 自定义算子封装与框架集成

## 1. 本讲目标

上一讲（u9-l1）我们学习了 Auto Mode：让编译器接管 Tile 缓冲摆放与同步插入。本讲把视角从「单条 kernel 怎么写」拉高到「一个自定义算子怎么交付」：

1. 掌握 **kernel 侧融合**：以 `kernels/custom/fused_add_relu_mul` 为例，理解把 Add + ReLU + Mul 三个逐元素算子折叠进一个 kernel 的写法、多核切分与双缓冲优化。
2. 掌握 **host 侧封装**：读懂 `main.cpp` 中模板化的功能测试 / 性能测试骨架，理解 kernel 动态库与 host 可执行程序的「双目标」构建方式，以及 `LaunchXxx` 启动桩的来源。
3. 掌握 **框架集成**：理解 `demos/baseline/add` 中 `op_extension` 扩展机制——如何用 `TORCH_LIBRARY_FRAGMENT` 声明 schema、用 `PrivateUse1` 派发键注册实现、用 `EXEC_KERNEL_CMD` 启动 kernel，最终让 Python 侧能调用 `torch.ops.npu.my_add(...)`。

学完本讲，你应该能把任意一个 PTO kernel 封装成「可在测试程序中运行、可注册进 PyTorch」的完整算子。

## 2. 前置知识

本讲假设你已读过以下内容（前序讲义）：

- **u1-l4 第一个算子 Add**：kernel 标准骨架「GlobalTensor 视图 → TLOAD → 事件同步 → 计算 → TSTORE」，以及 host 侧 `EXEC_KERNEL_CMD` 的初次亮相。
- **u6-l1 多核编程**：SPMD 模型——所有核执行同一份 kernel，靠 `get_block_idx()` / `get_block_num()` 区分身份，按输出归属切分数据。
- **u6-l2 流水线并行**：double buffer（乒乓缓冲）三要素——双份缓冲、0/1 翻转槽位、按槽配对的事件。
- **u9-l1 Auto Mode**：`__PTO_AUTO__` 宏下编译器自动插入缓冲分配与同步；本讲的示例是 **Manual 模式**，正好构成对照。

再补充三个本讲新概念，用通俗语言解释：

| 术语 | 通俗解释 |
|------|----------|
| **算子融合（operator fusion）** | 把多个小算子合并成一个大 kernel。不融合时中间结果要写回全局内存（GM）再读出来；融合后中间结果留在片上，省掉往返。 |
| **host 侧 / device 侧** | host 侧指运行在 CPU 上的程序（负责分配内存、启动 kernel、比对结果）；device 侧指运行在 AI Core 上的 kernel 本体。两侧代码分开编译、通过启动桩缝合。 |
| **算子 schema 与派发键** | PyTorch 自定义算子需要一份「函数签名声明」（schema，如 `my_add(Tensor x, Tensor y) -> Tensor`）和一个「注册到哪个后端」的派发键。PTO demo 用 `PrivateUse1` 这个派发键挂到 NPU 后端（`torch_npu` 接管该键）。 |
| **op_extension** | `demos/baseline/add` 中的一个目录名，也是构建产出的动态库名（`libop_extension.so`）：把 host 侧注册代码编成 so，打成 Python 包，`import op_extension` 时自动 `torch.ops.load_library` 加载注册。 |

## 3. 本讲源码地图

| 文件 | 作用 | 所属模块 |
|------|------|----------|
| [kernels/custom/fused_add_relu_mul/fused_add_relu_mul_kernel.cpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/custom/fused_add_relu_mul/fused_add_relu_mul_kernel.cpp) | device 侧 kernel：3 个版本（基础 / 双缓冲 / 大 Tile） | kernel 侧融合 |
| [kernels/custom/fused_add_relu_mul/main.cpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/custom/fused_add_relu_mul/main.cpp) | host 侧测试程序：功能测试 + 性能测试 | host 侧封装 |
| [kernels/custom/fused_add_relu_mul/CMakeLists.txt](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/custom/fused_add_relu_mul/CMakeLists.txt) | 双目标构建：kernel 动态库 + host 可执行 | host 侧封装 |
| [kernels/custom/fused_add_relu_mul/run.sh](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/custom/fused_add_relu_mul/run.sh) | 一键构建运行脚本（sim / npu 两种模式） | host 侧封装 |
| [demos/baseline/add/csrc/host/my_add.cpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/csrc/host/my_add.cpp) | PyTorch 算子注册：schema + 派发 + 启动 | 框架集成 |
| [demos/baseline/add/csrc/host/utils.h](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/csrc/host/utils.h) | `EXEC_KERNEL_CMD` 宏定义 | 框架集成 |
| [demos/baseline/add/CMakeLists.txt](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/CMakeLists.txt) | `ascendc_library` + `op_extension` 动态库构建 | 框架集成 |
| [demos/baseline/add/setup.py](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/setup.py) | 把 op_extension 打成 Python wheel | 框架集成 |
| [demos/baseline/add/op_extension/_load.py](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/op_extension/_load.py) | `torch.ops.load_library` 加载 so | 框架集成 |
| [demos/baseline/add/test/test.py](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/test/test.py) | Python 侧调用与精度校验 | 框架集成 |
| [docs/coding/framework-integration.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/framework-integration.md) | 框架集成方法论（PyTorch/TF/ONNX Runtime 模式） | 框架集成 |
| [tests/cpu/st/testcase/tadddeqrelu/tadddeqrelu_kernel.cpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadddeqrelu/tadddeqrelu_kernel.cpp) | CPU 仿真下验证融合算子的参考样板 | 代码实践 |

## 4. 核心概念与源码讲解

### 4.1 kernel 侧融合：三个算子折叠进一个 kernel

#### 4.1.1 概念说明

`fused_add_relu_mul` 计算的是：

\[ \text{out} = \text{ReLU}(x + \text{bias}) \times \text{scale} \]

如果不融合，需要三个独立 kernel：Add 写一次 GM、ReLU 读+写一次 GM、Mul 再读+写一次 GM，合计 3 读 3 写共 6 次 GM 访问和 3 次 kernel 启动。融合成一个 kernel 后只需 1 读 1 写共 2 次 GM 访问、1 次启动，中间结果全程留在片上 Tile 里。对于这种算术极其简单（每元素 2 次浮点运算）的算子，运行时间几乎完全由 GM 带宽决定，所以 GM 流量降为 1/3 意味着性能有 2-3 倍量级的提升空间——这正是[示例 README](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/custom/fused_add_relu_mul/README_zh.md#L27-L32) 中「预期性能提升 2-3×」的来源。

融合的本质：**用算术强度换内存流量**。每元素 GM 字节数从 \( 6 \times 4\text{B} \) 降到 \( 2 \times 4\text{B} \)，而计算量不变。

#### 4.1.2 核心流程

kernel 的整体执行流程（基础版）：

```text
__global__ __aicore__ FusedAddReLUMulKernel(out, x, bias, scale, totalLength)
  ├─ CalculateBlockRange：按 block_idx 均分 totalLength，得到本核的 [start, end)
  └─ for i in [start, end) 步进 TILE_SIZE:
       ├─ TLOAD(tile_x, GlobalTensor(x + i))     # GM → 片上
       ├─ TADDS(tile_result, tile_x, bias)        # 步骤1：加标量
       ├─ TRELU(tile_result, tile_result)         # 步骤2：激活
       ├─ TMULS(tile_result, tile_result, scale)  # 步骤3：乘标量
       └─ TSTORE(GlobalTensor(out + i), tile_result)  # 片上 → GM
```

双缓冲版在其上叠加：预加载第 0 块 → 循环内「先发起下一块的 TLOAD（异步）、再 WAIT 当前块、计算、写回」，让相邻两轮的搬运与计算重叠。

#### 4.1.3 源码精读

**融合计算核心**——三行指令就是三个「虚拟算子」：

[fused_add_relu_mul_kernel.cpp:57-68](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/custom/fused_add_relu_mul/fused_add_relu_mul_kernel.cpp#L57-L68) 把融合体抽成模板函数，供三个 kernel 版本复用。`TADDS`/`TMULS` 是「tile × 标量」变体（u4-l1 讲过的 TBinSOp 族），标量经寄存器广播；`TRELU` 原地计算 \(\max(0, x)\)。注意后两步都是 `tile_result` 既当源又当目的——片上 Tile 的原地更新正是融合省内存的关键。

**多核切分**：

[fused_add_relu_mul_kernel.cpp:23-46](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/custom/fused_add_relu_mul/fused_add_relu_mul_kernel.cpp#L23-L46) 是 u6-l1 学过的「按输出归属切分」最简形态：`elements_per_block = ceil(totalLength / block_num)`，本核负责 `[start, end)`，各核输出互不重叠、零跨核同步。返回 `start < totalLength` 用于跳过数据量不足以填满所有核时的空转核。

**Tile 配置模板化**：

[fused_add_relu_mul_kernel.cpp:73-88](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/custom/fused_add_relu_mul/fused_add_relu_mul_kernel.cpp#L73-L88) 定义了两档 Tile 配置：`StandardTileConfig`（16×256 = 4096 元素 = 16 KB，适配 A2/A3）与 `LargeTileConfig`（32×512 = 16384 元素 = 64 KB，适配 L1 更大的 A5）。把形状决策收进 trait 结构体，kernel 实现就能用同一份模板面向不同档位硬件——这是「同一份 kernel 跨代复用」的微缩示范。

[fused_add_relu_mul_kernel.cpp:93-111](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/custom/fused_add_relu_mul/fused_add_relu_mul_kernel.cpp#L93-L111) 的 `KernelContext` 与 `INIT_KERNEL_CONTEXT` 宏把「切分 + 类型别名」包成一行，减少三个版本间的重复。

**基础版主循环**：

[fused_add_relu_mul_kernel.cpp:116-129](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/custom/fused_add_relu_mul/fused_add_relu_mul_kernel.cpp#L116-L129) 就是 4.1.2 伪代码的原样落地。注意 `GlobalTensor(x + i)` 每轮用指针平移构造新视图（u2-l1：构造是 O(1) 零成本），Tile 的动态掩码自动保护尾块不足 TILE_SIZE 时的越界（u2-l2）。

**双缓冲版**：

[fused_add_relu_mul_kernel.cpp:134-164](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/custom/fused_add_relu_mul/fused_add_relu_mul_kernel.cpp#L134-L164) 是 u6-l2 乒乓模式的最小完整样例：`tile_x[2]` 双缓冲 + `load_event[2]` 事件数组；循环外先预加载第 0 块（L145），循环内先发起下一块 TLOAD（L155-L158，拿到 `load_event[next]`），再 `WAIT(load_event[curr])` 等当前块、计算、写回。相比 gemm_performance 的四级流水，这里只有 MTE2 → Vector 两段，是最适合初学者模仿的 double buffer 骨架。

**三个入口 kernel**：

[fused_add_relu_mul_kernel.cpp:187-191](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/custom/fused_add_relu_mul/fused_add_relu_mul_kernel.cpp#L187-L191)、[L203-L207](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/custom/fused_add_relu_mul/fused_add_relu_mul_kernel.cpp#L203-L207)、[L217-L221](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/custom/fused_add_relu_mul/fused_add_relu_mul_kernel.cpp#L217-L221) 用 `__global__ __aicore__` 标记三个入口，分别把统一实现实例化为「基础 / 双缓冲 / 大 Tile」三个版本。`__global__` + `__aicore__`（或 `AICORE`）是设备侧函数标注，host 侧不能直接调用，必须经启动桩（见 4.2）。

#### 4.1.4 代码实践

**实践目标**：不运行代码，仅通过「改指令」理解融合体的可替换性。

**操作步骤**：

1. 打开 [fused_add_relu_mul_kernel.cpp:57-68](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/custom/fused_add_relu_mul/fused_add_relu_mul_kernel.cpp#L57-L68)。
2. 把 `TRELU(tile_result, tile_result);` 这一行注释掉。
3. 手工推导此时 kernel 计算的公式（纸面推演即可）。

**需要观察的现象**：无需运行——对照 `main.cpp` 中的 golden 计算（见 4.2.3），确认你推导的公式与 golden 是否还一致。

**预期结果**：注释 ReLU 后 kernel 变为 \(\text{out} = (x + \text{bias}) \times \text{scale}\)，与 golden 公式 \(\text{ReLU}(...) \times \text{scale}\) 不一致，测试会 FAIL。这说明：**融合体里每个指令步骤都必须与 host 侧 golden 一一对应**，融合是「kernel 指令序列 + golden 公式」成对修改的操作。

#### 4.1.5 小练习与答案

**练习 1**：本算子每元素只做 2 次浮点运算却读写 8 字节 GM，为什么融合还能提速 2-3 倍？

**答案**：因为该算子是访存受限（Memory Bound）的：不融合时 GM 流量是 6×4B/元素（3 读 3 写），融合后是 2×4B/元素，流量降为 1/3；计算量不变且本就微不足道，所以墙钟时间近似正比于 GM 流量，理论加速比接近 3 倍（上限受 kernel 启动开销与尾块影响）。

**练习 2**：`FusedAddReLUMulOptimizedKernelImpl` 中（[L144-L146](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/custom/fused_add_relu_mul/fused_add_relu_mul_kernel.cpp#L144-L146)）为什么预加载要放在循环外、且要判断 `ctx.start < ctx.end`？

**答案**：循环内的 TLOAD 只对「下一块」发起（`tile_idx + 1 < num_tiles` 才执行），第 0 块没有「上一轮」可以捎带，必须在进循环前单独发起；而如果本核分到的区间为空（`start >= end`，核数多于数据块时出现），循环一次都不会执行，此时若无条件预加载会对越界地址发起 TLOAD，所以要用 `if` 保护。

**练习 3**：基础版 [L122-L128](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/custom/fused_add_relu_mul/fused_add_relu_mul_kernel.cpp#L122-L128) 中 TLOAD 与 TADDS 之间没有写任何 `set_flag/wait_flag`，数据依赖靠什么保证？

**答案**：这里依赖的是 API 的隐式同步约定——PTO 指令 API 内部统一为「TSYNC 折叠等待已有事件 → 执行 → 返回 RecordEvent」（u2-l3），`TADDS` 等计算指令接收 TLOAD 返回的事件作为依赖即可保证顺序；本示例未显式传递事件，真机上的严格时序应像双缓冲版那样用 `Event`/`WAIT` 显式表达，CPU 仿真下单线程按序执行所以功能总是正确（这也再次印证「CPU 验功能、真机验时序」的纪律）。

### 4.2 host 侧封装：测试骨架与双目标构建

#### 4.2.1 概念说明

device 侧 kernel 无法被 CPU 直接调用（`__global__` 语法普通编译器也不认识），所以一个可交付的算子必须配一套 host 侧封装，它做四件事：

1. **内存编排**：host 内存（`aclrtMallocHost`）↔ device 内存（`aclrtMalloc`），输入下发、结果取回。
2. **启动 kernel**：通过启动桩把参数与 stream 交给运行时。
3. **正确性判据**：host 上算一份 golden（参考实现），与 device 结果比对。
4. **性能度量**：预热 + 多次迭代 + 计时，输出吞吐。

`fused_add_relu_mul/main.cpp` 把 3、4 做成函数模板，把「启动方式」作为可替换参数注入——这就是 host 侧封装的核心设计模式。

#### 4.2.2 核心流程

```text
main()
 ├─ TestKernel<LaunchFunc>("名字", LaunchXxx<float>, length, bias, scale)   × 5 组
 │    ├─ aclInit / aclrtSetDevice / aclrtCreateStream
 │    ├─ 分配 host + device 内存
 │    ├─ InitializeTestData（确定性造数）→ ComputeGolden（CPU 参考实现）
 │    ├─ H2D 拷贝 → launch_func(...) 启动 kernel → SyncStream → D2H 拷贝
 │    └─ CompareResults 逐元素比对容差 → PASSED / FAILED
 ├─ BenchmarkKernel<LaunchFunc>(...) × 3 个版本
 │    ├─ 预热 10 次 → 计时 100 次迭代
 │    └─ 输出平均时延与吞吐（按读+写 2×data_size 计）
 └─ 汇总 all_passed 决定退出码
```

#### 4.2.3 源码精读

**启动桩的声明**：

[main.cpp:21-30](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/custom/fused_add_relu_mul/main.cpp#L21-L30) 只**声明**了三个 `LaunchXxx` 模板函数（`uint8_t*` 裸指针 + `void* stream`），整个仓库里找不到它们的手写定义。证据链在 CMakeLists：[CMakeLists.txt:74-103](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/custom/fused_add_relu_mul/CMakeLists.txt#L74-L103) 把 `fused_add_relu_mul_kernel.cpp` 编成 **SHARED 动态库**（L76-L81）并链接进 host 可执行（L97-L98），而 kernel 编译选项里带着 [CMakeLists.txt:55](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/custom/fused_add_relu_mul/CMakeLists.txt#L55) 的 `--cce-pto-enable`——即这些 `Launch` 桩由 Bisheng 工具链编译 kernel 时自动生成并从动态库导出，host 侧只管声明（具体生成规则以工具链文档为准，标注：**待本地验证**）。

对照另一种旧式风格——手写启动桩：[topk_kernel.cpp:369-384](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/topk/topk_kernel.cpp#L369-L384) 在 kernel 文件里手写 `launchTopk`，用 `<<<blockDim, nullptr, stream>>>` 三尖括号语法启动模板实例，并显式 `template void launchTopk<float>(...)` 实例化；[gemm_performance_kernel.cpp:250-274](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_performance/gemm_performance_kernel.cpp#L250-L274) 同款，且用 `#ifndef __COSTMODEL` 包住以便 Perf-Sim 直接调用 kernel 体。两种风格的选择：形状参数需要在 host 侧定档的（topk/gemm）选手写；签名即全部参数的（本例）交给工具链生成即可。

**golden 与比对**：

[main.cpp:35-47](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/custom/fused_add_relu_mul/main.cpp#L35-L47) 的 `ComputeGolden` 就是 4.1 练习中提到的「与 kernel 指令一一对应的参考公式」：加、ReLU、乘三步。**修改融合体时必须同步修改这里**。[main.cpp:52-81](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/custom/fused_add_relu_mul/main.cpp#L52-L81) 的 `CompareResults` 统计最大误差与超差个数（容差 1e-5），打印前 10 个坏点便于定位。

**测试骨架模板**：

[main.cpp:142-204](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/custom/fused_add_relu_mul/main.cpp#L142-L204) 的 `TestKernel` 把 launch 函数当模板参数 `LaunchFunc` 注入（L185 `launch_func(...)` 一处调用，五个测试复用全套内存编排）；[main.cpp:209-258](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/custom/fused_add_relu_mul/main.cpp#L209-L258) 的 `BenchmarkKernel` 预热 10 次 + 迭代 100 次（L228-L238），吞吐按读加写 `2.0 * data_size` 计（L245）。[main.cpp:276-301](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/custom/fused_add_relu_mul/main.cpp#L276-L301) 用三档规模（1K / 1M / 16M）测正确性、三个 kernel 版本测性能。

**双目标构建与 sim/npu 切换**：

[CMakeLists.txt:74-103](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/custom/fused_add_relu_mul/CMakeLists.txt#L74-L103) 的 `pto_custom_kernel` 函数把整个算子封装成两个目标：`<NAME>_kernel` SHARED（用 `-xcce` 的 CCE 选项编译，[L77](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/custom/fused_add_relu_mul/CMakeLists.txt#L77) 还指定了 `--cce-aicore-arch=dav-c220-cube` 即 A2/A3 Cube 核架构）与 `<NAME>` 可执行（用 `-xc++` 普通 C++ 编译）。[L99-L100](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/custom/fused_add_relu_mul/CMakeLists.txt#L99-L100) 按 `RUN_MODE` 生成器表达式切换链接 `runtime_camodel`（仿真器）或 `runtime`（真机）。[run.sh:36-62](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/custom/fused_add_relu_mul/run.sh#L36-L62) 校验 `RUN_MODE` 只能是 `sim|npu` 后执行 `cmake -DRUN_MODE=... -DSOC_VERSION=...` → `make -j16` → 运行。注意：这里的 `sim` 指 **CAMS 仿真器**（需要 CANN 工具链），与本仓库无硬件依赖的 `__CPU_SIM` 后端是两条不同路径（u1-l3）。

#### 4.2.4 代码实践

**实践目标**：验证「main.cpp 的 launch 注入点」——只改一处即可换测另一个 kernel 版本。

**操作步骤**（有 CANN 环境时）：

1. `source /usr/local/Ascend/ascend-toolkit/set_env.sh`。
2. `cd kernels/custom/fused_add_relu_mul && ./run.sh -r sim -v Ascend910B1`（无 CANN 环境则跳到第 3 步做阅读版）。
3. 阅读版：在 [main.cpp:276-301](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/custom/fused_add_relu_mul/main.cpp#L276-L301) 中数一数：五个功能测试分别注入了哪个 `LaunchFunc`？如果新增第四个 kernel 版本，main.cpp 需要改几处？

**需要观察的现象**（真机/仿真器下）：功能测试全部 `PASSED`；性能测试中双缓冲版的吞吐应高于基础版。

**预期结果**：五个功能测试中三个基础（小/中/大数据）、一个双缓冲、一个大 Tile；新增版本只需「声明一个 Launch 模板 + 加一行 `TestKernel`/`BenchmarkKernel`」，印证 launch-注入模式的可扩展性。无硬件环境时此实践为源码阅读型，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`main.cpp` 为什么用 `uint8_t*`（字节指针）而不是 `float*` 传地址给 `LaunchXxx`？

**答案**：为了类型解耦——启动桩是按字节搬运的通用入口，dtype 信息由模板参数 `T`（如 `LaunchFusedAddReLUMul<float>`）在实例化时确定，kernel 侧再 `reinterpret_cast` 回 `__gm__ float*`（参见 [gemm_performance_kernel.cpp:241-247](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_performance/gemm_performance_kernel.cpp#L241-L247) 的同款做法）。这样同一份 host 测试代码可同时驱动 fp16/fp32 两套实例。

**练习 2**：`BenchmarkKernel` 计算吞吐为什么用 `2.0 * data_size` 而不是 `data_size`？

**答案**：该算子每元素从 GM 读一次、写一次，有效 GM 流量是输入加输出共 2 份数据；按 2 倍体积折算得到的 GB/s 才是「有效内存带宽利用率」，可与硬件 HBM 带宽直接对比判断是否已打满（u6-l3 的 Bound 判定思路）。

**练习 3**：`InitializeTestData`（[main.cpp:86-94](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/custom/fused_add_relu_mul/main.cpp#L86-L94)）用 `i % 1000` 的确定性公式而不是 `rand()`，好处是什么？

**答案**：结果可复现——失败用例每次出现在同一位置，便于回归与排查；同时避开 `rand()` 的线程安全与分布不可控问题。测试数据范围 [-2, 2] 也刻意跨过 ReLU 的零点，保证正负分支都被覆盖。

### 4.3 框架集成：op_extension 与 PyTorch 算子注册

#### 4.3.1 概念说明

前两节的算子只能通过独立可执行程序调用。要让 PyTorch 用户像调内置算子一样调用，需要走「框架集成」。[docs/coding/framework-integration.md:23-28](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/framework-integration.md#L23-L28) 给出三种方式对比：

| 方式 | 优点 | 缺点 | 适用 |
|------|------|------|------|
| Python Extension | 开发快、易调试 | Python/C++ 边界开销 | 原型验证 |
| C++ Extension | 高性能、类型安全 | 构建注册更复杂 | 生产、性能敏感 |
| 框架插件/自定义后端 | 贴近部署路径 | 维护成本高 | 稳定产品集成 |

仓库的 `demos/baseline/add` 走的是 C++ Extension 路线，落成 **op_extension** 包：host 注册代码编成 `libop_extension.so` → setuptools 打成 wheel → `pip install` → Python 里 `import op_extension` 自动加载，之后 `torch.ops.npu.my_add(x, y)` 即可调用。文档特别声明（[framework-integration.md:3](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/framework-integration.md#L3)）这些片段是「实现模式」而非可直接落地的代码——具体 API 名随 torch_npu 版本变化。

#### 4.3.2 核心流程

```text
┌─ 构建期 ─────────────────────────────────────────────┐
│ CMakeLists.txt                                        │
│  ├─ ascendc_library(no_workspace_kernel STATIC,       │
│  │        csrc/kernel/add_custom.cpp)   ← device 侧   │
│  └─ add_library(op_extension SHARED,                 │
│           csrc/host/*.cpp)             ← host 注册侧  │
│ setup.py（NpuExtension）→ 驱动 cmake → 打 wheel       │
└──────────────────────────────────────────────────────┘
┌─ 运行期 ─────────────────────────────────────────────┐
│ import op_extension                                   │
│  └─ _load.py: torch.ops.load_library(libop_extension.so) │
│ torch.ops.npu.my_add(x_npu, y_npu)                    │
│  └─ my_add.cpp: EXEC_KERNEL_CMD → ACLRT_LAUNCH_KERNEL │
└──────────────────────────────────────────────────────┘
```

my_add.cpp 的注册三步（与 [framework-integration.md:29-56](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/framework-integration.md#L29-L56) 的工作流一一对应）：

1. **定 schema**：`m.def("my_add(Tensor x, Tensor y) -> Tensor")`。
2. **写实现**：`run_add_custom` 做输入检查、`empty_like` 分配输出、取数据指针、`EXEC_KERNEL_CMD` 启动。
3. **注册派发**：`TORCH_LIBRARY_IMPL(npu, PrivateUse1, m)` 把实现挂到 NPU 后端的 `PrivateUse1` 派发键。

#### 4.3.3 源码精读

**host 侧算子实现**：

[my_add.cpp:16-29](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/csrc/host/my_add.cpp#L16-L29) 是标准的 host 实现函数：`at::empty_like(x)` 分配输出、循环累乘 `sizes()` 算总元素数、以 `blockDim = 20`（向量核数）调 [L27](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/csrc/host/my_add.cpp#L27) 的 `EXEC_KERNEL_CMD(add_custom, blockDim, x, y, z, totalLength)` 启动。注意 tensor 直接作为参数传入——宏内部会做类型转换。

**schema 声明与派发注册**：

[my_add.cpp:33-38](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/csrc/host/my_add.cpp#L33-L38) 在 `TORCH_LIBRARY_FRAGMENT(npu, m)` 命名空间里声明 schema；[my_add.cpp:41-46](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/csrc/host/my_add.cpp#L41-L46) 用 `TORCH_LIBRARY_IMPL(npu, PrivateUse1, m)` + `TORCH_FN` 注册实现函数。schema 与实现分开声明，意味着同一 schema 未来可再挂 Autograd 等其它派发键（[framework-integration.md:219-222](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/framework-integration.md#L219-L222) 的示例）。

**EXEC_KERNEL_CMD 宏**：

[utils.h:49-62](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/csrc/host/utils.h#L49-L62) 展开这个「启动宏」：`ConvertTypes` 把 tensor 抽成裸指针（[L22](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/csrc/host/utils.h#L22) 的 `ConvertType` 从 tensor storage 取 `data()`）、`getCurrentNPUStream` 取当前流、lambda 里 `std::apply` 展开参数后调 `ACLRT_LAUNCH_KERNEL(kernel_name)(blockdim, acl_stream, params...)`（来自 CCE 工具链头 `aclrtlaunch_add_custom.h`，[my_add.cpp:12](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/csrc/host/my_add.cpp#L12) 引入），最终经 `OpCommand::RunOpApi` 提交。**关键认知：torch 的 stream 语义在这里被接住**——算子自动落在当前 NPU 流上，与框架的异步执行模型兼容。

**构建与打包**：

[demos/baseline/add/CMakeLists.txt:56-63](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/CMakeLists.txt#L56-L63) 用 CANN 的 `ascendc_library` 把 `add_custom.cpp` 编成静态库（include 路径指向 PTO 头文件目录，即 `PTO_LIB_PATH`）；[L65-L76](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/CMakeLists.txt#L65-L76) 把 `csrc/host/*.cpp` 编成名为 `op_extension` 的 SHARED 库并链接 torch_npu/ascendcl。[setup.py:125-133](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/setup.py#L125-L133) 用 `torch_npu.utils.cpp_extension.NpuExtension` 定义空扩展占位、自定义 `build_clib` 命令（[L84-111](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/setup.py#L84-L111)）驱动 cmake 并把产物拷进 `op_extension/lib/`，打成名为 `op_extension` 的 wheel。

**运行期加载与调用**：

[op_extension/__init__.py:13-15](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/op_extension/__init__.py#L13-L15) 在包被 import 时自动执行 `_load.py` 的 [_load_opextension_so（L20-L23）](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/op_extension/_load.py#L20-L23)——`torch.ops.load_library` 加载 so，schema 注册随之生效。[test.py:26-37](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/test/test.py#L26-L37) 是最终用户视角：CPU 造 fp16 张量 → `.npu()` 搬上设备 → [L32](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/test/test.py#L32) `torch.ops.npu.my_add(x_npu, y_npu)` → 与 CPU 参考结果 `assertRtolEqual`。[run.sh:20-25](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/run.sh#L20-L25) 一条龙：`setup.py bdist_wheel` → `pip install` → `python3 test/test.py`。

**最佳实践**：

[framework-integration.md:674-687](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/framework-integration.md#L674-L687) 的 DO/DON'T 值得抄录：接口保持简单、提供 shape/type 推断、完整单测；不要在算子内部分配大临时内存、不要假设输入连续（先 `contiguous()`）、不要忽略空张量等边角。文档 6.1 节（[L543-L551](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/framework-integration.md#L543-L551)）还点明：`torch.relu(x + y)` 这类 JIT 融合正是自定义融合算子要替换的目标——与 4.1 的融合收益呼应。

#### 4.3.4 代码实践

**实践目标**：走通 op_extension 的「构建 → 安装 → 调用」闭环（有 NPU + torch_npu 环境时），或完成纯源码链路追踪（无环境时）。

**操作步骤**：

1. 有环境路线：`cd demos/baseline/add && ./run.sh`（脚本完成 set_env、打 wheel、安装、跑测试）。
2. 无环境路线（源码追踪）：从 `test.py:32` 的 `torch.ops.npu.my_add` 出发，逆向画出调用链：`torch.ops` → `PrivateUse1` 派发 → `run_add_custom` → `EXEC_KERNEL_CMD` → `ACLRT_LAUNCH_KERNEL(add_custom)` → kernel 侧 `add_custom.cpp`。在纸上写出每一跳所在的文件与行号。

**需要观察的现象**：有环境时测试输出 `assertRtolEqual` 通过；无环境时你应得到一张五级调用链图。

**预期结果**：调用链五跳分别为：`test/test.py:32`（Python 入口）→ `csrc/host/my_add.cpp:44`（派发注册）→ `my_add.cpp:27`（启动宏）→ `csrc/host/utils.h:56`（ACLRT_LAUNCH_KERNEL 展开）→ `csrc/kernel/add_custom.cpp`（device 侧）。无环境时结论为源码阅读型，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`TORCH_LIBRARY_FRAGMENT` 与 `TORCH_LIBRARY_IMPL` 为什么分成两段写？

**答案**：`FRAGMENT` 只声明 schema（可多文件多次_FRAGMENT地拼同一个命名空间），`IMPL` 把具体函数绑定到某个派发键。分开后，同一份 schema 可以按需挂多个派发键——如同时注册 `PrivateUse1`（NPU 前向）与 `Autograd`（反向），训练/推理路径互不干扰（见 [framework-integration.md:213-223](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/framework-integration.md#L213-L223)）。

**练习 2**：`blockDim = 20` 这个数从哪来？换成 `48` 会怎样？

**答案**：20 是该 demo 目标芯片（910B 级）的向量核（AIV）数目，与 kernel 内 `get_block_num()` 的返回值对应，决定数据切成多少份。改成 48 后若硬件核数不足，启动会失败或行为未定义；核数超过 20 时多余核分到空区间，由 `CalculateBlockRange` 类似的边界检查跳过（在 add kernel 中即 start >= totalLength 直接返回）。**核数取值必须与目标 SoC 和 kernel meta 声明的核型一致**（u6-l1）。

**练习 3**：为什么 `_load.py` 里用 `torch.ops.load_library` 而不是 Python 的 `ctypes.CDLL`？

**答案**：`ctypes` 只把 so 装进进程，不会触发 PyTorch 的算子注册机制；`torch.ops.load_library` 加载时会执行 so 里的 `TORCH_LIBRARY_*` 静态注册代码，把 schema 与实现登记进 PyTorch 派发表，之后 `torch.ops.npu.my_add` 这个调用点才能解析。这是「注册生效」与「 merely加载」的本质差别。

## 5. 综合实践

**任务**：实现 `fused_mul_add` 算子——\( \text{out} = x \times \text{scale} + \text{bias} \)（先乘后加），补齐 kernel 侧与 host 侧封装，并在 CPU 仿真下验证。这是本讲三个模块的综合：融合写法（4.1）+ host 封装（4.2）+ 可选的框架注册（4.3）。

### 路线 A：CPU 仿真验证（无硬件，推荐先做）

利用仓库的 `__CPU_SIM` 后端与 ST 用例设施（u1-l3、u10-l1 预告）：

1. **建目录**：新建 `tests/cpu/st/testcase/fused_mul_add/`，包含四件套 `main.cpp` / `fused_mul_add_kernel.cpp` / `gen_data.py` / `CMakeLists.txt`。
2. **写 kernel**（示例代码，参照 [tadddeqrelu_kernel.cpp:16-56](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadddeqrelu/tadddeqrelu_kernel.cpp#L16-L56) 的结构）：

   ```cpp
   // 示例代码：fused_mul_add kernel 体（CPU ST 风格）
   template <int row, int validRow, int col, int validCol>
   PTO_INTERNAL void runFusedMulAdd(__gm__ float* out, __gm__ float* x, float scale, float bias)
   {
       using DynShape = pto::Shape<1, 1, 1, -1, -1>;
       using DynStride = pto::Stride<1, 1, -1, -1, 1>;
       using SrcGlobal = GlobalTensor<float, DynShape, DynStride>;
       SrcGlobal srcGlobal(x, DynShape(validRow, validCol), DynStride(validRow, validCol));
       using TileT = Tile<TileType::Vec, float, validRow, col, BLayout::RowMajor, -1, -1>;
       TileT srcTile(validRow, validCol);
       TileT dstTile(validRow, validCol);
       TASSIGN(srcTile, 0x0);
       TASSIGN(dstTile, 0x8000);
       TLOAD(srcTile, srcGlobal);
       TMULS(dstTile, srcTile, scale);   // 先乘
       TADDS(dstTile, dstTile, bias);    // 后加
       TSTORE(srcGlobal /*换成 out 的视图*/, dstTile);
   }
   ```

   再仿照 [tadddeqrelu_kernel.cpp:58-62](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadddeqrelu/tadddeqrelu_kernel.cpp#L58-L62) 写 `extern "C" __global__ AICORE void launchFusedMulAddCase1(...)` 实例化一组形状，并照 [L118-L136](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadddeqrelu/tadddeqrelu_kernel.cpp#L118-L136) 写 dispatch 模板。注意 out 需要单独的 `DstGlobal` 视图，示例中为简洁省略。
3. **写 main.cpp**：照抄 [tadddeqrelu/main.cpp:36-94](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadddeqrelu/main.cpp#L36-L94) 的「读 input*.bin → H2D → dispatch → D2H → 写 output.bin → 与 golden.bin 比对」骨架，写一两个 `TEST_F` 用例。
4. **写 gen_data.py**：仿照 [tadddeqrelu/gen_data.py:37-60](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadddeqrelu/gen_data.py#L37-L60)，golden 公式为 `golden = x * scale + bias`（**注意与 kernel 指令顺序一致：先乘后加**）。
5. **注册**：`CMakeLists.txt` 写一行 `pto_cpu_sim_st(fused_mul_add)`（同 [tadddeqrelu/CMakeLists.txt:11](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tadddeqrelu/CMakeLists.txt#L11)），并把目录名加进 [tests/cpu/st/testcase/CMakeLists.txt:46-170](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/CMakeLists.txt#L46-L170) 的用例清单（清单有序，按字母序插入）。
6. **运行**：`python3 tests/run_cpu.py -t fused_mul_add`（`-t` 单跑一个用例，见 [run_cpu.py:455](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/run_cpu.py#L455)）。脚本会自动先跑 gen_data.py 生成 golden 再编译执行 gtest。
7. **观察**：gtest 输出 `[  PASSED  ]`。若失败，检查 golden 公式与指令顺序是否一致、`TASSIGN` 偏移是否重叠（本例 0x0 与 0x8000 各占 32 KiB 内不重叠，尺寸大时需调整）。

### 路线 B：完整工程目录（有 CANN 环境时）

1. `cp -r kernels/custom/fused_add_relu_mul kernels/custom/fused_mul_add`，按 [kernels/custom/README_zh.md:23-29](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/custom/README_zh.md#L23-L29) 的五步走。
2. kernel 侧把 `PerformFusedComputation` 改成 `TMULS` → `TADDS` 两步；`main.cpp` 的 `ComputeGolden` 同步改为 `x[i] * scale + bias`。
3. `CMakeLists.txt` 末行 `pto_custom_kernel(fused_mul_add)` 改名，文件名同步改。
4. `./run.sh -r sim -v Ascend910B1` 验证。

### 路线 C：框架集成（可选，需 torch_npu + NPU）

仿照 [my_add.cpp:33-46](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/csrc/host/my_add.cpp#L33-L46) 注册 `fused_mul_add(Tensor x, Scalar scale, Scalar bias) -> Tensor`（标量参数写法见 [framework-integration.md:77](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/framework-integration.md#L77)），host 实现里先取 `data_ptr<float>()` 再 `EXEC_KERNEL_CMD`。Python 侧用 `torch.ops.npu.fused_mul_add(x_npu, 2.0, 1.0)` 与 `(x * 2.0 + 1.0)` 比对。

**预期结果**：路线 A 在仅装了 GCC≥13/Clang≥15 与 numpy 的机器上即可通过（u1-l3 的 CPU 路径门槛）；三条路线的 golden 全部是 \( x \times \text{scale} + \text{bias} \)。若你的运行结果与此不符，优先排查「先乘后加」与「先加后乘」的顺序笔误——\( (x+b) \times s \neq x \times s + b \)（除非 \( b \times (s-1) = 0 \)）。本综合实践未在本环境实际运行（路线 B/C 需硬件），**待本地验证**。

## 6. 本讲小结

- **kernel 侧融合**：把 Add+ReLU+Mul 三算子折叠为一个 kernel，GM 访问 6 次 → 2 次，是访存受限算子提性能的第一手段；融合体的每个指令步骤必须与 host 侧 golden 公式一一对应、成对修改。
- **封装复用三板斧**：融合体抽成模板函数（`PerformFusedComputation`）、Tile 配置抽成 trait（`StandardTileConfig`/`LargeTileConfig`）、host 测试抽成「launch 函数注入」的函数模板（`TestKernel`/`BenchmarkKernel`），使「新增一个版本」只改一行。
- **双缓冲最小骨架**：预加载第 0 块 → 循环内「发下一块 TLOAD → WAIT 当前块 → 计算 → 写回」，是四级流水（gemm_performance）之前的最佳练习标本。
- **双目标构建**：算子交付物 = kernel 动态库（CCE 编译，`--cce-pto-enable` 自动导出 `LaunchXxx` 启动桩）+ host 可执行（普通 C++），`RUN_MODE=sim|npu` 切换 `runtime_camodel`/`runtime`；仓库另有手写 `<<<>>>` 启动桩的旧式风格（topk、gemm_performance）。
- **框架集成**：op_extension 机制 = CMake 编 host 注册侧为 `libop_extension.so` + setuptools 打 wheel + `import` 时 `torch.ops.load_library`；注册三步为 schema 声明（`TORCH_LIBRARY_FRAGMENT(npu,...)`）、实现函数（`EXEC_KERNEL_CMD` 启动）、派发绑定（`TORCH_LIBRARY_IMPL(npu, PrivateUse1,...)`）。
- **验证路径分层**：无硬件走 `__CPU_SIM` 的 ST 用例（`python3 tests/run_cpu.py -t xxx`），有 CANN 走 CAMS 仿真器（`./run.sh -r sim`），有真机走 `--run-mode npu` + torch_npu 集成测试。

## 7. 下一步学习建议

- **u9-l3 构建体系与打包**：本讲的 `pto_custom_kernel` 函数、`ascendc_library`、setup.py 打 wheel 都只是局部切面，下一讲系统讲解顶层 CMakeLists、`build.sh` 与 wheel 打包的全貌。
- **u10-l1 测试体系**：综合实践路线 A 用到的 ST 四件套、`pto_cpu_sim_st` 注册与 `run_st.py`/`run_cpu.py` 的过滤机制，将在该讲展开。
- **延伸阅读**：[docs/coding/framework-integration.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/framework-integration.md) 的 TensorFlow/ONNX Runtime 章节（模式与 PyTorch 路线同构）；[demos/baseline/gemm_basic](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/gemm_basic/README_zh.md) 提供了同一套 op_extension 机制的第二个完整样本，可对照检验你是否真正掌握。
- 若想继续深化融合算子性能，回到 u6-l3 的 Bound 判定方法，对 `fused_mul_add` 做吞吐测算，验证它是否如预期打满内存带宽。
