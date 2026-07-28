# 调试工具：T.print、layout 可视化与日志

## 1. 本讲目标

本讲是 U9「扩展与内部机制」系列的第三篇，承接 [u2-l4（内存层级与显存分配）](./u2-l4-memory-hierarchy.md) 与 [u4-l3（内存布局推断 Layout/Fragment）](./u4-l3-layout-inference.md)，专门讲**调试**。

TileLang 是一个编译器：你写的是「计算规格」，真正运行的是机器生成的代码。当代码跑不对时，你不能像写普通 Python 那样直接 `print`——因为那行 `print` 要被编译进 GPU kernel、在成千上万个线程里执行。本讲教你怎么在这种环境下做调试。

学完后你应当能够：

1. 用 `T.print` 在 kernel 内部打印标量、buffer（global/shared/fragment/local）的值，并理解它为什么对 fragment 要做一次「先搬回 shared 再打印」的额外步骤。
2. 用 `plot_layout` 把一个 `T.Layout` / `T.Fragment` 可视化成「逻辑坐标 → 线程号 + 线程内槽位」的彩色网格图，直观理解 u4-l3 讲的布局推断结果。
3. 配置 TileLang 的日志：区分 Python 侧 `logging` 与 C++ 侧 TVM 的 `LOG/DLOG/VLOG`，以及 `TVM_LOG_LEVEL`、`TVM_LOG_DEBUG` 这两个名字相同却含义不同的开关。
4. 用 `tilelang/env.py` 的 `EnvVar` 描述符统一管理环境变量，知道哪些环境变量（`TILELANG_PRINT_ON_COMPILATION`、`TILELANG_CLEANUP_TEMP_FILES`、`TILELANG_PASS_DIFF` 等）是调试时最常用的旋钮。

---

## 2. 前置知识

进入源码前，先用三段话建立直觉。

**第一，TileLang 的调试天然分两层。** 一份 TileLang 程序要经过「下译（lower）→ 生成源码 → 设备编译器编译 → 运行」几个阶段，对应的故障也分三类（见 [debug_tools_for_tilelang.md:22-28](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/docs/tutorials/debug_tools_for_tilelang.md#L22-L28)）：

- **生成问题（generation）**：下译过程就报错，根本没产出可执行文件；
- **正确性问题（correctness）**：能跑，但结果不对；
- **性能问题（performance）**：结果对但慢，需要 Nsight Compute / rocProf 等厂商剖析器。

本讲聚焦前两类。`T.print` 解决「正确性」，`plot_layout` 与日志/环境变量既帮「正确性」也帮「生成」。

**第二，`T.print` 的本质是「编译期展开成对设备端函数的调用」。** 你在 kernel body 里写 `T.print(buf)`，它并不是 Python 的 `print`，而是一个在**编译期**被展开的宏调用：展开后变成一行 `tirx.call_extern("handle", "debug_print_var", ...)`，最终由 codegen 印成对设备端模板函数 `debug_print_var`（定义在 `src/tl_templates/<backend>/debug.h`）的调用，运行时再由该函数用 `printf` 输出。所以 `T.print` 的输出是**设备运行时**打印的，带有 `BlockIdx / ThreadIdx` 前缀。

**第三，「布局」是 fragment 的核心抽象，可视化它就是可视化布局推断的结果。** u4-l3 讲过：`local.fragment` 寄存器 tile 的逻辑坐标与物理寄存器不一一对应，由 `LayoutInference` pass 自动把整块 tile 分发到各线程。`plot_layout` 把这种「逻辑坐标 → (线程号, 线程内槽位)」的映射画成彩色方格图，让你一眼看出「第几个元素归第几号线程」。这恰好是 `T.print` 对 fragment 要先搬回 shared 才能打印的根因——数据散落在各线程寄存器里，必须借助布局信息收集起来。

> 名词速查：**fragment**（`local.fragment` 作用域的寄存器 tile）、**Layout**（逻辑坐标→物理槽位的索引函数）、**macro**（编译期卫生宏，在 prim_func body 内被内联展开）、**codegen**（把 TIR 印成 C/CUDA 源码的代码生成器）、**PassContext**（pass 配置容器）。

> 全景提示：本讲详细展开「T.print / plot_layout / logging / env」四个最小模块，这是规格要求的核心。TileLang 还有一批同属调试工具箱的能力——**Pass Diff**（按 pass 逐条对比 IR 文本）、**Pass Visualizer**（结构树视图）、**AutoDD**（自动 delta 调试，把 200 行程序缩成 30 行最小复现）、**Visual Layout Inference**（编译期自动出布局图）、**postproc 回调**（拦截并改写生成的源码）。它们大多由 `env.py` 的环境变量驱动，我们会在 4.2 与 4.4 里点到，作为「想进一步深入」的入口。

---

## 3. 本讲源码地图

| 文件 | 角色 | 本讲用途 |
| --- | --- | --- |
| `tilelang/language/print_op.py` | `T.print` 及其底层宏（`print_var` / `print_*_buffer_with_condition` / `print_msg`） | 4.1 主角：打印标量与各类 buffer |
| `tilelang/tools/plot_layout.py` | 布局可视化工具 `plot_layout` / `plot_fragment_tv` | 4.2 主角：把 Layout/Fragment 画成彩色网格 |
| `docs/tutorials/logging.md` | TVM 日志体系说明 | 4.3：LOG/DLOG/VLOG/CHECK 的语义 |
| `tilelang/env.py` | 环境变量统一管理（`EnvVar` 描述符 + `Environment` 类） | 4.4 主角：所有调试旋钮的集中地 |

辅证文件（非主角，补全链路）：`src/tl_templates/cuda/debug.h` 与 `src/tl_templates/maca/debug.h`（设备端 `debug_print_*` 实现）、`src/cuda/codegen/codegen_cuda.cc`（codegen 引入 `debug.h`）、`tilelang/language/kernel.py`（`get_thread_bindings`）、`tilelang/language/utils.py`（`index_to_coordinates`）、`tilelang/language/tir/entry.py`（`@macro`）、`tilelang/engine/callback.py`（postproc 回调注册）、`tilelang/backend/pass_pipeline/pipeline_utils.py`（Visual Layout Inference 开关）、`examples/plot_layout/layout_transform.py` 与 `examples/visual_layout_inference/visual_layout_inference.py`（两个可运行示例）。

---

## 4. 核心概念与源码讲解

### 4.1 T.print：kernel 内运行时打印

#### 4.1.1 概念说明

`T.print` 是 TileLang 内建的调试原语，让你在 kernel 内部「窥探」中间值。它与 Python 的 `print` 同名却完全不同：

- Python `print` 在宿主机执行，编译时就被丢弃；
- `T.print` 被**编译进 kernel**，在**设备运行时**执行，输出带 `BlockIdx / ThreadIdx` 前缀，便于你定位是哪个线程块、哪个线程打印的。

它接受三类对象（见 [print_op.py:138-154](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/print_op.py#L138-L154)）：

- `tirx.Buffer`：按 scope 分四种打印策略（见 4.1.3）；
- `tirx.PrimExpr`：直接打印一个标量表达式；
- `None`：只打印一段消息字符串（`msg`）。

#### 4.1.2 核心流程

`T.print` 的执行链路横跨编译期与运行时，可分成「展开 → 印码 → 运行」三段：

```
kernel body 里写 T.print(buf, msg="...")
        │  (编译期：print 是普通函数，按 buf.scope() 分派到底层 @macro)
        ▼
print_shared_buffer_with_condition(cond, buf, elems, msg)   # 一个 @macro
        │  (宏内联展开)
        ▼
for i in serial(elems):                                     # 遍历每个元素
    tirx.call_extern("handle", "debug_print_buffer_value",  # 生成对外部函数的调用
                     msg, buf.name, i, buf[coords])
        │  (lowering/codegen 之后)
        ▼
生成的源码里出现 #include <tl_templates/cuda/debug.h>，
并调用其中的 __device__ void debug_print_buffer_value(...)
        │  (设备运行时)
        ▼
printf("msg='...' BlockIdx=(...), ThreadIdx=(...): buffer=..., index=..., value=...\n")
```

三个要点：

1. **分派按 scope**：global/local 不加线程条件（每个线程各打各的）；shared/fragment 加条件 `tx == main_lane and ty == 0 and tz == 0`，**只让一个线程打印**，否则成千上万个线程会把同一个 shared buffer 重复打印成千上万遍。
2. **fragment 要先搬回 shared**：fragment 的数据散落在各线程寄存器、且物理排布由布局决定，无法直接逐元素访问。因此对 `local.fragment`，先 `alloc_shared` 一个中转 buffer，再 `T.copy(buffer, smem)`（这次 copy 会按布局把数据收集成连续排列），最后从 shared 打印。
3. **main_lane 的含义**：`main_lane = warp_group_id * 128 + warp_id * 32`（[print_op.py:159-161](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/print_op.py#L159-L161)）。默认 `warp_group_id=0, warp_id=0`，即第 0 号 lane，等价于「第一个 warp 的第一个线程」。

#### 4.1.3 源码精读

**(a) 顶层 `print` 按 scope 分派**

[print_op.py:155-204](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/print_op.py#L155-L204) 是分派主体。注意 fragment 分支会算出元素总数、构造「只让一个线程打印」的条件，再调用宏：

```python
elif buffer.scope() == "local.fragment":
    elems = 1
    for dim in buffer.shape:
        elems *= dim
    # 只有第一个线程执行打印，避免重复
    condition = tx == main_lane and ty == 0 and tz == 0
    if not msg:
        msg = f"buffer<{buffer.name}, {buffer.dtype}>"
    print_fragment_buffer_with_condition(condition, buffer, elems, msg)
```

`tx, ty, tz = get_thread_bindings()` 取自当前 `KernelLaunchFrame`（u2-l2 讲过的线程绑定帧），见 [kernel.py:474-477](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/kernel.py#L474-L477)。

**(b) fragment 宏：先 copy 回 shared 再打印**

[print_op.py:76-94](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/print_op.py#L76-L94) 是理解「fragment 与布局耦合」的关键：

```python
@macro
def print_fragment_buffer_with_condition(condition, buffer, elems, msg=""):
    smem = alloc_shared(buffer.shape, buffer.dtype, "shared")
    copy(buffer, smem)                 # 借布局把 fragment 收集成连续 shared
    if condition:
        for i in serial(elems):
            coords = index_to_coordinates(i, buffer.shape)
            tirx.call_extern("handle", "debug_print_buffer_value",
                             msg, buffer.name, i, smem[coords])
```

这里的 `copy(buffer, smem)` 不是简单的逐元素搬运，而是依赖 u4-l3 的布局推断：fragment 的逻辑坐标经 `Layout.Forward` 映射到「线程号 + 槽位」，`T.copy` 据此把散落的数据汇聚成 shared 里连续的 tile。没有布局信息，fragment 根本无从打印。`index_to_coordinates`（[utils.py:29-48](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/utils.py#L29-L48)）把一维下标还原成多维坐标，用于索引 buffer。

**(c) 标量打印最简单**

[print_op.py:13-25](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/print_op.py#L13-L25)：打印一个表达式只是一行 `call_extern`：

```python
@macro
def print_var(var, msg=""):
    tirx.call_extern("handle", "debug_print_var", msg, var)
```

**(d) `@macro` 是怎么展开的**

这些底层函数都带 `@macro` 装饰（[entry.py:64-72](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/tir/entry.py#L64-L72)）。`@macro` 把函数注册成一个**卫生宏**：在 prim_func body 内「调用」它时，函数体被**内联展开**到调用处（默认 `hygienic=True`，宏体内的名字绑定到宏定义处而非调用处）。所以 `print_var(var, msg)` 在 kernel 里展开成其函数体那一行 `call_extern`。注意顶层 `T.print` 本身是普通函数而非宏，它只是在编译期「构造 TIR」时被调用、再委托给这些宏。

**(e) 设备端实现：debug.h**

codegen 在印出源码时会引入头文件（CUDA 见 [codegen_cuda.cc:676](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/codegen/codegen_cuda.cc#L676)，MACA 见 [codegen_maca.cc:365](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/codegen_maca.cc#L365)）。`debug.h` 提供了对应名字的设备函数：

[debug.h:101-109](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/tl_templates/cuda/debug.h#L101-L109) —— `debug_print_var` / `debug_print_buffer_value` 都是 `__device__` 模板函数，借助 `PrintTraits<T>` 按 dtype 选 `printf` 格式串：

```cpp
template <typename T> __device__ void debug_print_var(const char *msg, T var) {
  PrintTraits<T>::print_var(msg, var);
}
```

[debug.h:130-135](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/tl_templates/cuda/debug.h#L130-L135) —— 纯消息打印 `debug_print_msg` 输出形如：

```text
msg='hello world' BlockIdx=(0, 0, 0), ThreadIdx=(0, 0, 0)
```

这正是 `call_extern("handle", "debug_print_msg", msg)` 在运行时落地的样子。MACA 的 `src/tl_templates/maca/debug.h` 结构完全对称（见 [debug.h:96-104](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/tl_templates/maca/debug.h#L96-L104)），所以 `T.print` 在三个 GPU 后端上行为一致。

#### 4.1.4 代码实践

**实践目标**：在一个 GEMM kernel 里用 `T.print` 打印累加器 fragment 的部分值，观察输出格式，并验证「fragment 打印会引入额外的 shared 中转」。

**操作步骤**（示例代码，基于 `examples/gemm/example_gemm.py` 改写）：

```python
# 示例代码：在 GEMM 累加后插入 T.print
import tilelang
import tilelang.language as T

@tilelang.jit
def matmul_print(M, N, K, block_M, block_N, block_K,
                 dtype=T.float16, accum_dtype=T.float32):
    @T.prim_func
    def main(A: T.Tensor((M, K), dtype), B: T.Tensor((K, N), dtype),
             C: T.Tensor((M, N), dtype)):
        with T.Kernel(T.ceildiv(N, block_N), T.ceildiv(M, block_M),
                      threads=128) as (bx, by):
            A_shared = T.alloc_shared((block_M, block_K), dtype)
            B_shared = T.alloc_shared((block_K, block_N), dtype)
            C_local  = T.alloc_fragment((block_M, block_N), accum_dtype)
            T.clear(C_local)
            for k in T.Pipelined(T.ceildiv(K, block_K), num_stages=2):
                T.copy(A[by*block_M, k*block_K], A_shared)
                T.copy(B[k*block_K, bx*block_N], B_shared)
                T.gemm(A_shared, B_shared, C_local)
            # —— 调试打印：打印 fragment 的前若干个元素 ——
            T.print(C_local, msg="after gemm C_local")
            T.copy(C_local, C[by*block_M, bx*block_N])
    return main

kernel = matmul_print.compile(M=128, N=128, K=128,
                              block_M=32, block_N=32, block_K=32)
# 1) 看生成的设备源码里是否出现 debug_print_buffer_value 与 #include debug.h
print(kernel.get_kernel_source())
# 2) 实际运行（需 GPU），观察标准输出里的 BlockIdx/ThreadIdx/value 行
```

**需要观察的现象**：

1. `get_kernel_source()` 的输出里应能搜到 `debug_print_buffer_value` 调用，以及顶部 `#include <tl_templates/cuda/debug.h>`（CUDA）或 `tl_templates/maca/debug.h`（MACA）。
2. 运行时标准输出会出现形如 `msg='after gemm C_local' BlockIdx=(0, 0, 0), ThreadIdx=(0, 0, 0): buffer=C_local, index=0, dtype=float value=...` 的行；因为 fragment 只让第 0 号 lane 打印，不会出现海量重复。
3. 想确认「fragment 打印引入额外 shared」：把 `block_M=block_N=16` 设小，对比「有 `T.print`」与「无 `T.print`」两份生成源码，前者会多出一个 `__shared__` 中转 buffer 和一次 copy。

**预期结果**：能稳定看到带坐标前缀的数值行。若你把 `T.print(C_local)` 改成标量 `T.print(by, msg="block y")`，则只输出一行标量，源码里是 `debug_print_var`。

> 待本地验证：在没有 GPU 的机器上，仍可编译并 `get_kernel_source()` 查看生成的打印调用，但看不到运行时 `printf` 输出。

#### 4.1.5 小练习与答案

**练习 1**：为什么对 `shared` buffer 打印时要加 `tx == main_lane and ty == 0 and tz == 0` 条件，而对 `global` buffer 不加？

**参考答案**：shared buffer 是线程块内共享的，块内所有线程看到同一份数据，若不加条件，块内每个线程都会打印一遍相同内容，造成海量重复输出。global buffer 的访问通常带有线程索引偏移（每个线程访问不同元素），逐线程打印反映的是各自视角，故不加条件。

**练习 2**：把 `T.print` 加在一个 `T.Parallel` 循环里打印循环变量 `i`，会有什么现象？该如何避免刷屏？

**参考答案**：`T.Parallel` 让每次迭代由不同线程执行，打印 `i` 会在所有并行线程上触发，输出非常多且交错。避免刷屏的方法是用条件打印——例如 `if T.get_thread_bindings()[0] == 0: T.print(i)`，只让第一个线程打印（这正是 `print_var_with_condition` 这类带条件宏的用途）。

**练习 3**：`T.print` 的 fragment 分支为什么必须先 `T.copy(buffer, smem)`？

**参考答案**：fragment 的元素散落在各线程的寄存器里，物理排布由 u4-l3 的布局推断决定，逻辑下标无法直接定位到物理寄存器。`T.copy` 会按布局把 fragment 收集成 shared 里连续的 tile，之后才能用一维下标 + `index_to_coordinates` 逐元素读取并打印。

---

### 4.2 plot_layout：Layout/Fragment 布局可视化

#### 4.2.1 概念说明

`plot_layout` 是一个**纯 Python** 工具（依赖 matplotlib），与编译无关。你给它一个 `T.Layout` 或 `T.Fragment` 对象，它把「逻辑坐标 → 物理槽位（以及线程号）」的映射画成一张彩色方格图。它的价值是让 u4-l3 里那些抽象的索引函数（如 `_j // 16 * 64 + _i // 16 * 32 + ...`）变得**肉眼可读**。

它有两种工作模式，由传入对象类型决定：

- 传 `T.Fragment` → 调 `_plot_fragment_layout`：每个格子标注**线程号（T）+ 线程内槽位（L）**，颜色按线程号区分。这正是 fragment「如何切分给线程」的直观呈现。
- 传 `T.Layout` → 调 `_plot_layout_map`：每个格子标注映射后的**输出坐标**，可选 `view="input"`（网格是输入空间）或 `view="output"`（网格是输出空间）。

> 与「Visual Layout Inference」的区别：本节的 `plot_layout` 是你**手动构造或获取**一个布局对象再画图；而 Visual Layout Inference 是**编译期自动**捕获 `LayoutInference` pass 推断出的布局并出图（由 `TL_LAYOUT_VISUALIZATION_ENABLE` 开关控制，见 4.2.4）。两者画的是同类东西，区别在「布局从哪来」。

#### 4.2.2 核心流程

```
plot_layout(layout, name=..., formats=...)
        │
        ├── isinstance(layout, Fragment)  → _plot_fragment_layout
        │       1. 取 input_shape / num_threads / replicate_size
        │       2. 遍历每个逻辑坐标，map_forward_thread → 线程号
        │                       map_forward_index → 线程内槽位
        │       3. 画方格：颜色=线程号，文字="T<线程> L<槽位>"
        │
        └── isinstance(layout, T.Layout)  → _plot_layout_map
                1. 遍历每个输入坐标，map_forward_index → 输出坐标
                2. 按 view=input/output 摆放网格
                3. 画方格：颜色=来源位置，文字=坐标
        │
        ▼
_save_plot：按 formats（pdf/png/svg/all）写到 save_directory
```

核心是 Fragment 的两个查询方法（u4-l3 引入）：`map_forward_thread(index)` 返回某个逻辑坐标归哪个线程，`map_forward_index(index)` 返回它在该线程寄存器里的第几个槽位。`plot_layout` 不过是把这两个查询对每个格子都跑一遍、再把结果涂色写字。

#### 4.2.3 源码精读

**(a) 入口按类型分派**

[plot_layout.py:6-61](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/tools/plot_layout.py#L6-L61)：

```python
def plot_layout(layout, save_directory="./tmp", name="layout",
                colormap=None, verbose=False, formats="pdf",
                view="input", grid_shape=None):
    from tilelang.layout.fragment import Fragment
    if isinstance(layout, Fragment):
        _plot_fragment_layout(layout, save_directory, name,
                              colormap or "RdPu", verbose, formats)
    elif isinstance(layout, T.Layout):
        _plot_layout_map(layout, save_directory, name,
                         colormap or "Spectral", verbose, formats,
                         view=view, grid_shape=grid_shape)
    else:
        raise TypeError(...)
```

注意默认 `colormap` 随类型不同：Fragment 用 `RdPu`、Layout 用 `Spectral`；默认输出 `formats="pdf"`。

**(b) Fragment 视图：线程号 + 槽位**

[plot_layout.py:142-163](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/tools/plot_layout.py#L142-L163) 是 Fragment 可视化的核心，两段循环分别填「线程表」与「值表」：

```python
for i in range(replicate_size):
    for idx in itertools.product(*[range(dim) for dim in input_shape]):
        index = list(idx)
        if replicate_size > 1:
            index.insert(0, i)
        thread_id = layout.map_forward_thread(index)   # 逻辑坐标 → 线程号
        thread_map[idx].append(int(thread_id[0]))
        ...
        value_id = layout.map_forward_index(index)     # 逻辑坐标 → 线程内槽位
        value_map[idx].append(int(value_id[0]))
```

随后每个格子画一个矩形：`color = colors[thread_ids[0]]`（按线程上色），并标注 `T<线程号>` 与 `L<槽位>`（[plot_layout.py:198-225](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/tools/plot_layout.py#L198-L225)）。注意代码里 `warp_size = 32`（[plot_layout.py:173](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/tools/plot_layout.py#L173)），对 MACA 的 64 线程 warp 会触发「线程数少于 warp_size」的告警——这是工具的小局限，可视化本身仍可用。

**(c) 最小用法：构造一个转置 Layout 直接画**

[layout_transform.py:5-7](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/plot_layout/layout_transform.py#L5-L7) 是入门 `T.Layout` 构造与可视化的最简例子：

```python
transpose_layout = T.Layout([4, 4], lambda i, j: (j, i))
plot_layout(transpose_layout, name="transpose_4x4")
```

`T.Layout([4,4], lambda i,j: (j,i))` 定义了一个 4×4 的布局，把逻辑坐标 `(i,j)` 映射到 `(j,i)`（即转置）。`plot_layout` 会把这张映射画出来——`view="input"` 时网格是输入的 4×4，每个格子里写它映射到的输出下标。

**(d) 保存多种格式**

[plot_layout.py:80-104](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/tools/plot_layout.py#L80-L104) 的 `_save_plot` 支持把同一张图存成 pdf/png/svg，`formats` 接受 `"all"` 或逗号分隔字符串（如 `"png,svg"`）。

#### 4.2.4 代码实践

**实践目标**：可视化两类布局——(1) 一个手动构造的转置 `T.Layout`；(2) 了解如何拿到真实 GEMM 的 fragment 布局。

**操作步骤 1（手动 Layout，最快上手）**：

直接运行 `examples/plot_layout/layout_transform.py` 即可，或最小复现：

```python
# 示例代码
import tilelang.language as T
from tilelang.tools import plot_layout

# 转置布局：4x4 的 (i,j) -> (j,i)
lt = T.Layout([4, 4], lambda i, j: (j, i))
plot_layout(lt, name="transpose_4x4", formats="png", save_directory="./tmp")
```

**需要观察的现象**：`./tmp/transpose_4x4.png` 生成；图里 4×4 网格，第 `(i,j)` 格写着它映射到的输出坐标，对角线对称（因为转置）。

**操作步骤 2（真实 GEMM 的 fragment 布局）**：

要拿到真实 kernel 的 fragment 布局，最省事的是用编译期集成的 **Visual Layout Inference**，它会在 `LayoutInference` pass 后自动出图。开关是两个 pass 配置项（见 [pass_config.py:194-201](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/transform/pass_config.py#L194-L201)）：

```python
# 示例代码：开启编译期布局可视化（来自 examples/visual_layout_inference）
@tilelang.jit(
    pass_configs={
        tilelang.PassConfigKey.TL_LAYOUT_VISUALIZATION_ENABLE: True,
        tilelang.PassConfigKey.TL_LAYOUT_VISUALIZATION_FORMATS: "svg",
    }
)
def matmul(...):
    ...
    C_local = T.alloc_fragment((block_M, block_N), accum_dtype)
    ...
```

**需要观察的现象**：编译时控制台会打印类似下面的文本布局（见 [visual_layout_inference.py:49-54](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/visual_layout_inference/visual_layout_inference.py#L49-L54)）：

```text
C_local inferenced layout:
  Shape: [32, 32] -> [8]
  Thread: _j // 16 * 64 + _i // 16 * 32 + _i % 8 * 4 + _j % 8 // 2
  Index:  [_j % 16 // 8 * 4 + _i % 16 // 8 * 2 + _j % 2]
```

这段文本正是 `C_local` 这个 fragment 的 `forward_thread` / `forward_index` 表达式。它的开关判定在 [pipeline_utils.py:37-40](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/backend/pass_pipeline/pipeline_utils.py#L37-L40) 与 [pipeline_utils.py:83-88](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/backend/pass_pipeline/pipeline_utils.py#L83-L88) 的 `LayoutVisual`。若想进一步把这套表达式画成彩色图，可把 Visual Layout Inference 产出的 Fragment 对象喂给 `plot_layout`（`_plot_fragment_layout` 分支），即可得到「格子标 T/L」的图。

**预期结果**：步骤 1 得到一张转置网格图；步骤 2 得到 fragment 的线程映射表达式（及可选的图）。两者合起来，你就既会「手动画任意布局」，又会「抓取真实 kernel 的布局」。

> 待本地验证：`plot_layout` 需要安装 matplotlib；Visual Layout Inference 的 `svg/pdf/png` 输出还可能需要对应的 matplotlib 后端。

#### 4.2.5 小练习与答案

**练习 1**：`T.Layout([8,4], lambda i,j: (i%4*2 + i//4, j))`（interleave 布局，见 `layout_transform.py`）画出来大概是什么样？

**参考答案**：这是一个「把前半行与后半行交错」的重排。`view="input"` 时，输入的第 `i` 行会被涂成颜色 `i%4*2 + i//4`，于是行 0→0、行 1→2、行 4→1、行 5→3……相邻颜色在画面上交错出现，直观体现了「even rows from first half, odd rows from second half」。

**练习 2**：`_plot_fragment_layout` 里 `warp_size=32` 对 MACA（warp_size=64）意味着什么？

**参考答案**：该常量只影响「颜色循环」与「线程数少于 warp_size 时的告警」，并不影响 `map_forward_thread` 的正确性。对 MACA 的 64 线程 warp，可视化仍能正确标注每个格子的线程号，只是前 32 个线程的颜色按 hsv 循环、后 32 个可能重复配色，且会打印一条「线程数少于 warp_size」的告警。读图时以格子里的 `T<线程号>` 文字为准。

**练习 3**：`plot_layout` 和 Visual Layout Inference 各自适合什么场景？

**参考答案**：`plot_layout` 适合「你已有一个 Layout/Fragment 对象、想快速看它长什么样」，例如自学一个手写布局、或调试自定义发射器的 `InferLayout` 返回值。Visual Layout Inference 适合「你想看真实 GEMM 经过 `LayoutInference` pass 后 fragment 到底被推断成什么布局」，它集成在编译流水线里，无需你手动取对象，但需要开启 pass 配置开关。

---

### 4.3 logging：复用 TVM 的日志体系

#### 4.3.1 概念说明

TileLang 的日志分两侧：

- **Python 侧**：直接用 Python 标准库 `logging`。例如 `tilelang/env.py` 顶部就 `logger = logging.getLogger(__name__)`（[env.py:14](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/env.py#L14)）。你看到的大多数 `WARNING`（如「CUTLASS not found」「Loading tilelang libs from dev root」）都来自这里。
- **C++ 侧**：复用 TVM 的日志宏 `LOG / DLOG / VLOG / CHECK / ICHECK`（[logging.md:20-39](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/docs/tutorials/logging.md#L20-L39)）。`libtilelang.so` 里的检查、报错大多走 `ICHECK` 与 `LOG(FATAL)`。

本模块主要讲 C++ 侧，因为编译期/下译报错都从那里来。

#### 4.3.2 核心流程

```
C++ 代码里写 LOG(INFO) << "..." / DLOG(INFO) / ICHECK(cond)
        │
        ├── LOG / ICHECK  → 编进 Release 版，运行时按 TVM_LOG_LEVEL 过滤
        ├── DLOG / DCHECK → 仅 Debug 构建编入（受编译期宏 TVM_LOG_DEBUG 控制），
        │                   Release 版经死代码消除被删除
        └── VLOG(n)       → 基于 DLOG 实现，可按 verbose 级别 n 控制
        │
        ▼
运行时由环境变量控制输出：
  TVM_LOG_LEVEL  → 运行时日志级别（按文件指定，DEFAULT 兜底）
  TVM_LOG_DEBUG  → 运行时控制 DLOG 的显示级别（与编译期宏同名，易混淆）
```

#### 4.3.3 源码精读

**(a) 三类宏的语义差异**

[logging.md:26-31](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/docs/tutorials/logging.md#L26-L31) 给出关键区分：

- **`LOG`**：标准日志，保留在 Release 版，多数 C++ 报错用 `LOG(FATAL)`；
- **`DLOG`**：调试日志，**Release 版经死代码消除被删除**，只在 Debug 构建里存活。文档建议用 `DLOG` 而非 `LOG(DEBUG)`，因为后者会编进 release 运行时；
- **`VLOG`**：verbose 日志，可设级别（`VLOG(1..6)`），基于 `DLOG` 实现，TileLang 里用得少。

`CHECK` 家族（[logging.md:35-47](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/docs/tutorials/logging.md#L35-L47)）：`CHECK`（标准）、`DCHECK`（仅 debug）、`ICHECK`（**Internal Check，Release 版保留**，失败即 `LogFatal`）。TileLang 源码里大量正确性检查用 `ICHECK`。

**(b) 五个日志级别**

[logging.md:56-62](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/docs/tutorials/logging.md#L56-L62)：`DEBUG=0 / INFO=1 / WARNING=2 / ERROR=3 / FATAL=4`。设 `TVM_LOG_LEVEL=1` 即允许所有 `level <= 1`（DEBUG+INFO）的日志输出。

**(c) 两个同名却不同的 `TVM_LOG_DEBUG`**

这是最容易踩坑的地方（[logging.md:112-116](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/docs/tutorials/logging.md#L112-L116)）：

| 名称 | 性质 | 作用 |
| --- | --- | --- |
| `TVM_LOG_DEBUG`（**编译期宏**） | 由 CMake 在 Debug 构建时自动定义 | 决定 `DLOG` 内容**是否编入** `.so`；CMake 里 `target_compile_definitions(tilelang_objs PRIVATE "TVM_LOG_DEBUG")`（[logging.md:106-108](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/docs/tutorials/logging.md#L106-L108)） |
| `TVM_LOG_DEBUG`（**运行时环境变量**） | 启动进程时设置 | 运行时控制 DLOG 的**显示级别**，可按文件指定 |

要看到 `DLOG` 输出，必须**同时**满足：①用 Debug 模式构建（让宏编入）；②运行时 `TVM_LOG_DEBUG=1`（让级别放行）。两者缺一不可，名字相同极易混淆。

**(d) 按文件指定级别**

[logging.md:90-94](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/docs/tutorials/logging.md#L90-L94)：`TVM_LOG_DEBUG` 支持逗号分隔的 `<file_name>=<level>` 列表，特殊名 `DEFAULT` 兜底，`<level>=-1` 可禁用某文件；文件名是 `src/` 下相对的 `.cc` 名（`src/` 前缀可省）。

#### 4.3.4 代码实践

**实践目标**：在不动 C++ 源码的前提下，用环境变量把 TileLang 的 C++ 日志调出来。

**操作步骤**：

```bash
# 1) 让所有 INFO/DEBUG 级别的 C++ 日志输出（前提：用 Debug 模式构建过 tilelang）
TVM_LOG_LEVEL=1 python3 my_script.py

# 2) 只对某个 pass 文件开 DEBUG，其余静默
TVM_LOG_DEBUG="DEFAULT=-1,layout_inference.cc=1" python3 my_script.py

# 3) 用 Debug 模式重建（让 DLOG 真正编入 .so）
cmake .. -DCMAKE_BUILD_TYPE=Debug -DUSE_CUDA=ON
```

**需要观察的现象**：

- 步骤 1：若 `.so` 是 Debug 构建，你会看到大量平时被抑制的 `INFO` 行；若 `.so` 是 Release 构建，`DLOG` 行不会出现（已被消除），但 `LOG(INFO)`/`ICHECK` 仍会按级别输出。
- 步骤 2：只有 `layout_inference.cc` 里的 DEBUG 日志输出，其它文件被 `-1` 禁用——适合聚焦调试某个 pass。

**预期结果**：能按需放大/缩小 C++ 侧日志量。Python 侧日志则用标准 `logging` 控制，例如在脚本里 `logging.basicConfig(level=logging.WARNING)` 或单独调 tilelang logger 的级别。

> 待本地验证：能否看到 DLOG 输出，完全取决于 `.so` 是否以 Debug 模式构建；Release 安装包里 DLOG 不可见是预期行为。

#### 4.3.5 小练习与答案

**练习 1**：你在 Release 构建的 tilelang 上设了 `TVM_LOG_DEBUG=1`，却看不到某处 `DLOG(INFO)` 的输出，为什么？

**参考答案**：因为 `DLOG` 受编译期宏 `TVM_LOG_DEBUG` 控制——Release 构建不会定义该宏，`DLOG` 的内容经死代码消除已被删除，运行时环境变量无法让它「复活」。要看到 `DLOG`，必须用 `CMAKE_BUILD_TYPE=Debug` 重新构建。

**练习 2**：`ICHECK` 与 `DCHECK` 有何区别？TileLang 源码里正确性检查更该用哪个？

**参考答案**：`DCHECK` 只在 Debug 构建里生效，Release 版被删；`ICHECK` 是 Internal Check，**Release 版也保留**，失败即 `LOG(FATAL)`。对于「运行时必须成立、否则就是 bug」的正确性检查，应该用 `ICHECK`，保证用户在 Release 版上也能触发有意义的报错。`DCHECK` 只适合纯开发期的临时断言。

**练习 3**：Python 侧如何临时提高 tilelang 的日志级别以排查问题？

**参考答案**：tilelang 用标准 `logging`，可直接操作 logger：`import logging; logging.getLogger("tilelang").setLevel(logging.DEBUG)`，或在脚本入口 `logging.basicConfig(level=logging.DEBUG)`。这控制的是 Python 侧日志，与 C++ 侧 `TVM_LOG_LEVEL` 互不影响。

---

### 4.4 env：环境变量统一配置

#### 4.4.1 概念说明

TileLang 散落着大量环境变量（缓存路径、编译选项、调优参数、调试开关……）。如果到处写 `os.environ.get(...)`，会出现「键名和默认值散落多处、难发现、难测试」的问题。`tilelang/env.py` 用一个 **`EnvVar` 描述符** + 一个 **`Environment` 类**把所有环境变量集中管理：键名、默认值、读取、强制覆盖都收口在一处。

全局只有一个实例 `env = Environment()`（[env.py:485](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/env.py#L485)），整个代码库通过它读环境变量。

#### 4.4.2 核心流程

```
Environment 类把每个环境变量声明为类属性 = EnvVar("KEY", default)
        │
        ▼  读取 env.TILELANG_PRINT_ON_COMPILATION
EnvVar.__get__  →  self.get()
        │
        ├── 有 _forced_value（测试/调试时强制覆盖）→ 返回它（并记 warning）
        ├── KEY 在 os.environ 里           → 返回 os.environ[KEY]（动态读，改了立刻生效）
        └── 否则                            → 返回 default（default 可是 callable，惰性求值）
```

设计要点：

1. **动态读**：每次访问都现读 `os.environ`，所以脚本运行中 `os.environ[...]=...` 立刻生效，没有「import 时快照」的陈旧值问题。
2. **可强制覆盖**：给属性赋值 `env.X = "1"` 会存入 `_forced_value`，后续读取都用它（适合单测，不动真实环境）。
3. **默认值可惰性**：`default` 可以是 `Callable[[], str]`（如 `TILELANG_TMP_DIR` 默认基于 cache 目录拼接，[env.py:352](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/env.py#L352)）。

#### 4.4.3 源码精读

**(a) `EnvVar` 描述符**

[env.py:280-303](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/env.py#L280-L303) 是核心读写逻辑：

```python
def get(self):
    if self._forced_value is not None:
        return self._forced_value
    if self.key in os.environ:
        return os.environ[self.key]
    return self._get_default()

def __get__(self, instance, owner):
    return self.get()

def __set__(self, instance, value):
    self._forced_value = value
    # 想让覆盖全局生效，可取消下一行注释：
    # os.environ[self.key] = value
```

注意 `__set__` 默认**不**回写 `os.environ`——强制值只对 `env` 这个描述符实例生效，避免污染整个进程的环境（[env.py:300-303](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/env.py#L300-L303)）。

**(b) 调试相关的环境变量**

`Environment` 类（[env.py:326-387](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/env.py#L326-L387)）里，与调试最相关的是下面几个：

| 环境变量 | 默认 | 调试用途 |
| --- | --- | --- |
| `TILELANG_PRINT_ON_COMPILATION` | `"1"` | 编译时打印 kernel 名（[env.py:355](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/env.py#L355)） |
| `TILELANG_DISABLE_CACHE` | `"0"` | 关闭 kernel 缓存，单测/调试常用（[env.py:356-358](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/env.py#L356-L358)） |
| `TILELANG_CLEANUP_TEMP_FILES` | `"1"` | 设 `0` 可**保留临时编译文件**便于排查（[env.py:362-364](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/env.py#L362-L364)） |
| `TILELANG_PASS_DIFF` | `"0"` | Pass Diff：`terminal`/`html`/`both`，按 pass 逐条对比 IR（[env.py:370](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/env.py#L370)） |
| `TILELANG_PASS_DIFF_OUTPUT` | `tmp/pass_diff_output` | Pass Diff 的 HTML 报告目录（[env.py:371](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/env.py#L371)） |
| `TILELANG_JIT_DIAGNOSTICS` | `"0"` | 开启 JIT 阶段诊断（[env.py:366](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/env.py#L366)） |
| `TILELANG_COMPILE_TIMEOUT_SECONDS` | `""` | 给 nvcc 子进程设超时（[env.py:367](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/env.py#L367)） |

**(c) 布尔/解析辅助方法**

环境变量读出来都是字符串，`Environment` 提供了一批解析方法，把 `"1"/"true"` 之类归一化（[env.py:402-470](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/env.py#L402-L470)）。例如 `is_print_on_compilation_enabled()` 判定 `TILELANG_PRINT_ON_COMPILATION` 是否为真值，`get_pass_diff_mode()` 把 `TILELANG_PASS_DIFF` 映射成 `None / "terminal" / "html" / "both"`（[env.py:442-451](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/env.py#L442-L451)）。读代码时认准这些方法，比直接判字符串更稳。

**(d) Pass Diff：env 驱动的 IR 对比**

`TILELANG_PASS_DIFF` 是排查「生成问题」的利器（见 [debug_tools_for_tilelang.md:205-284](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/docs/tutorials/debug_tools_for_tilelang.md#L205-L284)）：开启后它 monkey-patch `Pass.__call__`，在每条 pass 执行前后捕获 IR、算 unified diff，按 `terminal`（彩色终端）或 `html`（带侧栏与折叠的可交互报告）输出。默认 `0` 完全零开销。

#### 4.4.4 代码实践

**实践目标**：用环境变量打开三个最常用的调试旋钮，观察它们对编译过程的影响。

**操作步骤**：

```bash
# 1) 保留临时编译文件 + 打印 kernel 名 + 开 Pass Diff 终端输出
TILELANG_CLEANUP_TEMP_FILES=0 \
TILELANG_PRINT_ON_COMPILATION=1 \
TILELANG_PASS_DIFF=terminal \
python3 examples/gemm/example_gemm.py
```

也可以在 Python 内强制覆盖（适合单测，不污染环境）：

```python
# 示例代码
from tilelang.env import env
env.TILELANG_DISABLE_CACHE = "1"          # 强制每次都真编译
print(env.TILELANG_PRINT_ON_COMPILATION)  # 动态读
```

**需要观察的现象**：

1. `TILELANG_CLEANUP_TEMP_FILES=0`：编译后 `~/.tilelang/cache/tmp/...` 下会保留生成的 `.cu`（或 MACA 的 `.c`）源码与编译中间产物，可手动打开查看。
2. `TILELANG_PASS_DIFF=terminal`：每条 pass 执行时会打印一段带 `+/-` 的彩色 diff，你能看到 `T.copy` 如何被 `LowerTileOp` 展开成底层 TIR、`LayoutInference` 如何给 fragment 加布局注解。
3. 强制覆盖后，同一进程内 `env.TILELANG_DISABLE_CACHE` 恒为 `"1"`，即便 `os.environ` 没设。

**预期结果**：你能在不改任何源码的前提下，让编译过程「自报家门」——留下临时文件、逐 pass 打印 IR 变化、按需关缓存。这是排查「为什么我的 kernel 编不出来/编出来不对」的第一手段。

> 待本地验证：Pass Diff 的 `html` 模式会生成带时间戳的报告文件（如 `pass_diff_20260611_205421.html`），路径由 `TILELANG_PASS_DIFF_OUTPUT` 控制。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `EnvVar.__get__` 每次都现读 `os.environ`，而不是在 `import` 时缓存一次？

**参考答案**：为了让运行时对 `os.environ` 的修改立刻生效。很多调试场景需要在脚本中途 `os.environ["TILELANG_PASS_DIFF"]="terminal"` 再触发编译；若 import 时快照，这种改动就无效了。现读带来的开销可忽略（字典查找），换来的是「随时可调」的灵活性。

**练习 2**：`env.TILELANG_DISABLE_CACHE = "1"`（赋值）与 `os.environ["TILELANG_DISABLE_CACHE"] = "1"` 有何不同？

**参考答案**：前者走 `EnvVar.__set__`，把值存进描述符的 `_forced_value`，**只**对该 `env` 实例的后续读取生效，且默认不回写 `os.environ`，适合单测隔离、不污染子进程。后者直接改进程环境，会被 `env` 的 `__get__` 现读到，同时也会传给 tilelang 启动的子进程（如 nvcc）。需要全局生效（含子进程）时用后者，需要进程内隔离覆盖时用前者。

**练习 3**：调试「kernel 编译失败」时，优先开哪几个环境变量？

**参考答案**：①`TILELANG_CLEANUP_TEMP_FILES=0` 保留出错的中间源码；②`TILELANG_PASS_DIFF=terminal` 看 IR 在哪条 pass 变得不对；③`TVM_LOG_LEVEL=1`（配合 Debug 构建）看 C++ 侧日志；④必要时 `TILELANG_DISABLE_CACHE=1` 确保每次都真编译、不被缓存掩盖问题。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次完整的「GEMM 调试会话」。

**任务**：写一个 GEMM kernel，用 `T.print` 验证累加器数值，用布局工具看清 fragment 切分，用日志/环境变量定位一个**人为注入的小 bug**。

**步骤**：

1. **基线 kernel**：复制 `examples/gemm/example_gemm.py`，确认能跑通、数值正确。
2. **加 `T.print`**：在 `T.gemm` 之后插入 `T.print(C_local, msg="C_local after gemm")`，运行后确认看到带 `BlockIdx/ThreadIdx` 的数值行；再用 `kernel.get_kernel_source()` 确认源码里出现 `debug_print_buffer_value` 与 `#include .../debug.h`。
3. **看布局**：给该 kernel 加 `pass_configs={TL_LAYOUT_VISUALIZATION_ENABLE: True, TL_LAYOUT_VISUALIZATION_FORMATS: "svg"}`，从编译输出抄下 `C_local` 的 `Thread`/`Index` 表达式；再独立运行 `examples/plot_layout/layout_transform.py` 熟悉 `plot_layout` 的出图。
4. **注入 bug 并排查**：故意把 `B_shared` 的形状写成 `(block_M, block_N)`（本应是 `(block_K, block_N)`）。然后：
   - 开 `TILELANG_CLEANUP_TEMP_FILES=0` 与 `TILELANG_PASS_DIFF=terminal`，观察报错发生在哪条 pass；
   - 若报错信息指向 TVM 内部，可用 `python -m tilelang.autodd buggy.py --err-msg "<错误片段>" -o minimized.py`（AutoDD，见 [debug_tools_for_tilelang.md:345-440](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/docs/tutorials/debug_tools_for_tilelang.md#L345-L440)）把它缩成最小复现。
5. **对照结论**：把上述四类工具（`T.print` 看运行时数值、布局工具看数据排布、env/Pass Diff 看编译期 IR、AutoDD 缩最小复现）写成一张「调试决策表」——什么症状用哪个工具。

**预期结果**：你不仅能修掉这个 bug，还建立起一套面向 TileLang 的调试套路：运行时问题用 `T.print`，布局困惑用 `plot_layout`/Visual Layout Inference，编译期问题用 Pass Diff + env 旋钮，复杂报错用 AutoDD。

---

## 6. 本讲小结

- **`T.print`** 是编译进 kernel 的运行时打印，按 buffer scope 分四种策略；fragment 因数据散落各线程寄存器，必须先 `T.copy` 回 shared 再打印，这正体现了 u4-l3 布局推断的存在意义。
- **`T.print` 的链路**是「Python 宏 → `tirx.call_extern` → codegen 印码引入 `debug.h` → 设备端 `printf`」，CUDA 与 MACA 的 `debug.h` 结构对称，行为一致。
- **`plot_layout`** 是纯 Python 可视化工具，把 `T.Layout`/`T.Fragment` 画成彩色网格：Fragment 标「线程号 + 线程内槽位」，Layout 标映射坐标；它和编译期集成的 Visual Layout Inference（`TL_LAYOUT_VISUALIZATION_ENABLE`）互补。
- **日志分两侧**：Python 用标准 `logging`，C++ 复用 TVM 的 `LOG/DLOG/VLOG/ICHECK`；注意两个同名的 `TVM_LOG_DEBUG`——编译期宏决定 `DLOG` 是否编入、运行时环境变量决定显示级别。
- **`env.py`** 用 `EnvVar` 描述符 + `Environment` 类集中管理所有环境变量，动态读取、可强制覆盖；调试最常用的是 `TILELANG_PRINT_ON_COMPILATION`、`TILELANG_CLEANUP_TEMP_FILES`、`TILELANG_DISABLE_CACHE`、`TILELANG_PASS_DIFF`。
- 调试工具箱还有 **Pass Diff / Pass Visualizer / AutoDD / postproc 回调**，分别解决「逐 pass IR 对比 / 结构树视图 / 最小复现缩放 / 拦截改写生成源码」。

---

## 7. 下一步学习建议

1. **Pass Diff 与 Pass Visualizer 深入**：本讲只点到 `TILELANG_PASS_DIFF`，建议结合 [debug_tools_for_tilelang.md:197-343](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/docs/tutorials/debug_tools_for_tilelang.md#L197-L343) 实跑一次 `html` 报告与 `pass_visualizer`，配合 u5-l2 的 transform 体系，理解每条 pass 到底改了什么。
2. **postproc 回调实战**：读 [callback.py:8-38](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/engine/callback.py#L8-L38)，注册一个 `tilelang_callback_maca_postproc`，在 MACA 源码里注入一行注释或 `printf`，体会「Python 注册、C++ 按名调用」的拦截点（u4-l1 讲过 postproc 的定位）。
3. **AutoDD 实跑**：拿 `examples/autodd/tilelang_buggy.py` 跑一次 `python -m tilelang.autodd`，看 200 行如何缩成 30 行，理解 PDD delta 调试。
4. **结合性能剖析**：本讲聚焦生成与正确性。正确性解决后，下一步用 `JITKernel.get_profiler().do_bench`（u8-l3）量延迟，再用厂商剖析器（Nsight Compute / rocProf）做硬件级分析。
5. **下一讲**：[u9-l4（测试与 examples 基础设施）](./u9-l4-testing-infra.md) 讲如何用 pytest + `examples/conftest.py` 为 kernel 写数值正确性测试——那是把「手动 `T.print` 验证」升级为「自动化回归校验」的正规军做法。
