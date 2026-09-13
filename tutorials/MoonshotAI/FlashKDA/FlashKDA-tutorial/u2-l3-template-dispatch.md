# u2-l3 模板参数分发：7 种 state 组合 × varlen 的实例化

## 1. 本讲目标

学完本讲，你应该能够：

1. 读懂 [csrc/fwd.h](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/fwd.h) 中 `launch_fwd` 的模板签名，说清 `D`、`HasStateIn`、`HasStateOut`、`StateFP32`、`IsVarlen` 五个模板参数各自控制 kernel 的哪一段行为。
2. 读懂 [csrc/flash_kda.cpp](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp) 中 `LAUNCH` / `DISPATCH_STATE` 两个宏的七分支分发逻辑，理解 `state_fp32` 是如何从两个可选状态张量的 dtype 推导出来的，以及为什么恰好是 7 个分支而不是 8 个。
3. 解释 [csrc/smxx/fwd_launch.cu](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu) 末尾那张显式实例化表（explicit instantiation）的作用：没有它，链接器会报什么错。
4. 从性能角度论证：为什么这些配置用编译期模板布尔而不是运行时 `if`。

本讲承接 u2-l2（校验链与预处理）。u2-l2 讲的是「进入分发之前发生什么」，本讲讲的是「校验通过之后，调用如何被路由到 14 份编译出的代码中的一份」。

## 2. 前置知识

### 2.1 C++ 函数模板与编译期布尔

C++ 的函数模板（function template）像一张「生成函数的配方」：

```cpp
template <bool IsVarlen>
void launch_fwd(/* 参数 */) {
    if constexpr (IsVarlen) {
        // 只有 IsVarlen == true 时才编译这段代码
    } else {
        // 只有 IsVarlen == false 时才编译这段代码
    }
}
```

`if constexpr` 是 C++17 引入的编译期分支：条件是编译期常量，不满足的分支**根本不会被编译**，这在 CUDA 术语里叫死代码消除（dead code elimination）。这与运行时 `if (is_varlen)` 有本质区别——后者两个分支都会编译成机器码，执行时靠分支预测硬扛。

### 2.2 翻译单元、声明与定义、链接

- **翻译单元（translation unit, TU）**：一个 `.cpp` / `.cu` 文件经过预处理（展开 `#include`）后交给编译器的完整内容。本项目的构建脚本 [setup.py:58-61](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/setup.py#L58-L61) 只有两个源文件：`csrc/flash_kda.cpp`（由 host 编译器编译，只做校验与分发）和 `csrc/smxx/fwd_launch.cu`（由 nvcc 编译，包含全部 CUDA 代码），即两个翻译单元。
- **声明 vs 定义**：`csrc/fwd.h` 里只有 `launch_fwd` 的**声明**（告诉编译器「有这个函数，长这样」）；真正的**定义**（函数体）在 `fwd_launch.cu` 里。
- **链接器**：`flash_kda.cpp` 调用 `launch_fwd<128, true, ...>` 时，编译器只看声明就放行；到链接阶段，链接器需要找到一份**实例化出来的函数体**（即模板按具体参数展开并编译出的机器码），找不到就报 undefined reference。

模板不会被「顺带」编译——除非编译器在某处看到了完整定义加具体参数。显式实例化（explicit instantiation）就是程序员主动写下的指令：

```cpp
template void launch_fwd<128, true, true, false, true>(/* 完整参数类型列表 */);
```

它告诉 nvcc：「请按这组参数把模板展开并生成机器码，放到符号表里供链接」。

### 2.3 pybind11 的 optional 参数

Python 侧的 `initial_state=None`、`final_state=None`、`cu_seqlens=None` 在 C++ 侧映射为 `std::optional<torch::Tensor>`（见 [csrc/flash_kda.cpp:40-42](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L40-L42)）。`has_value()` 把「用户传没传」变成一个运行时布尔，这个布尔随后被折算成模板参数——这就是本讲的全部故事：**运行时的可选性 → 编译期的代码特化**。

### 2.4 一个背景：TMA 描述符是「类型」，不只是「数值」

launch 层会在 host 侧为每块要搬的显存构造 TMA 描述符，然后按值传进 kernel（`CUTE_GRID_CONSTANT`，本质是 `__grid_constant__` 的常量内存传参）。bf16 状态和 fp32 状态对应的 smem 布局类型不同（`TMAStateSmemLayout` vs `TMAFP32StateSmemLayout`），所以**描述符本身是不同的 C++ 类型**，进而成为 kernel 模板的类型参数。这一点决定了「状态精度」很难退化成运行时布尔——后面 4.3 会再回到这里。

## 3. 本讲源码地图

| 文件 | 行数（约） | 本讲关注点 |
| --- | --- | --- |
| [csrc/fwd.h](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/fwd.h) | 27 行 | `launch_fwd` 的模板声明，五个模板参数及默认值 |
| [csrc/flash_kda.cpp](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp) | 233 行 | `has_state_in`/`has_state_out`/`state_fp32`/`is_varlen` 的推导，`LAUNCH`/`DISPATCH_STATE` 七分支宏，pybind 注册 |
| [csrc/smxx/fwd_launch.cu](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu) | 239 行 | 模板定义体、`make_state_tma` 的类型分叉、两个 kernel 的启动、末尾的显式实例化表 |
| [csrc/smxx/fwd_kernel1.cuh](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh) | 586 行 | 只接收 `IsVarlen` 一个布尔：tile 映射的二分查找 vs 整除 |
| [csrc/smxx/fwd_kernel2.cuh](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh) | 839 行 | 接收全部四个布尔：状态三进两出路径、varlen 寻址 |
| [setup.py](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/setup.py) | — | 确认只有两个编译单元，这是理解「为什么要显式实例化」的前提 |

## 4. 核心概念与源码讲解

### 4.1 模块一：`launch_fwd` 模板签名——五个参数各管什么

#### 4.1.1 概念说明

从 Python 进来的一次调用，最终要在 GPU 上跑出不同「形态」的代码，差异来自四个正交的配置维度：

- 有没有初始状态（`HasStateIn`）？
- 有没有最终状态输出（`HasStateOut`）？
- 状态用什么精度（`StateFP32`：false = bf16，true = fp32）？
- 序列长度是变长的（`cu_seqlens`，`IsVarlen`）还是等长 batch 的？

`launch_fwd` 把这四个问题编码成四个 `bool` 模板参数，外加一个 `int D`（head 维度，当前恒为 128）。它是 pybind 层与 kernel 层之间唯一的桥梁：**host 编译的 `flash_kda.cpp` 只认识这个函数签名，nvcc 编译的 `fwd_launch.cu` 负责在它内部启动两个真正的 `__global__` kernel**。

#### 4.1.2 核心流程

一次 `flash_kda.fwd` 调用穿过分发层的路径：

```text
Python fwd(...)
  └─ flash_kda.cpp::fwd
       ├─ 校验（u2-l2 已讲）
       ├─ 推导运行时布尔:
       │    has_state_in  = initial_state.has_value()
       │    has_state_out = final_state.has_value()
       │    state_fp32    = 任一存在的状态张量 dtype == fp32
       │    is_varlen     = cu_seqlens.has_value()
       └─ is_varlen ? DISPATCH_STATE(true) : DISPATCH_STATE(false)
            └─ 七分支 if-else 链 → LAUNCH(HI, HO, FP32, VL)
                 └─ launch_fwd<128, HI, HO, FP32, VL>(...)   ← 链接到 14 份实例之一
                  ├─ 构造全部 TMA 描述符（make_state_tma 按 StateFP32 分叉）
                  ├─ 启动 Kernel 1（模板参数：..., IsVarlen）
                  └─ 启动 Kernel 2（模板参数：..., HasStateIn, HasStateOut, StateFP32, IsVarlen）
```

#### 4.1.3 源码精读

**声明：一个只有 27 行的头文件。** 模板签名与默认实参：

[csrc/fwd.h:6-27](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/fwd.h#L6-L27)

```cpp
template <int D, bool HasStateIn = true, bool HasStateOut = true,
          bool StateFP32 = false, bool IsVarlen = true>
void launch_fwd(
    cutlass::bfloat16_t const* q_ptr,
    /* ...共 18 个参数... */
    cudaStream_t stream
);
```

注意两点：`D` 是 `int` 非类型参数，状态指针是 `void const*` / `void*`——因为 dtype 由 `StateFP32` 在定义体内决定如何 reinterpret。默认实参（`= true` 等）写在声明上，而 [csrc/smxx/fwd_launch.cu:6](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L6) 的定义处不再重复（C++ 规则：默认实参在同一 TU 内不得重复给出）。实际上调用方 `LAUNCH` 宏永远显式传满五个参数，这些默认值更像一份「典型用法」的文档。

**定义体开头：把模板参数交给布局与描述符。** [csrc/smxx/fwd_launch.cu:6-27](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L6-L27) 处定义与声明签名一致（去掉默认值），随后 `kInputStages = 3`、`kOutputStages = 2` 等常量在此写死——流水线深度不是本讲的模板旋钮，留给 u3-l2。

**状态描述符的编译期分叉。** 最能体现「模板参数改变生成代码」的段落是 `make_state_tma`：

[csrc/smxx/fwd_launch.cu:118-144](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L118-L144)

```cpp
auto make_state_tma = [&]() {
    if constexpr (StateFP32) {
        // fp32 状态：按 float 指针构造 load/store 描述符（FP32 smem 布局）
        ...
        return cute::make_tuple(tma_load, tma_store);
    } else {
        auto state_ptr_load = HasStateIn
            ? static_cast<BF16 const*>(initial_state_ptr)
            : reinterpret_cast<BF16 const*>(out_ptr);  // dummy, never used
        auto state_ptr_store = HasStateOut
            ? static_cast<BF16*>(final_state_ptr)
            : reinterpret_cast<BF16*>(out_ptr);        // dummy, never used
        ...
    }
};
auto [tma_load_initial_state, tma_store_final_state] = make_state_tma();
```

三个细节：

1. `StateFP32` 决定描述符的**类型**（fp32 布局 vs bf16 布局），两个分支返回的 tuple 类型不同，这就是 2.4 节说的「类型级差异」。
2. `HasStateIn == false` 时仍要构造一个 load 描述符（保持类型整齐），指针用 `out_ptr` 顶替——这个哑描述符**永远不会被真正使用**，因为 kernel 里对应的拷贝代码已被 `if constexpr` 删除。
3. 结构化绑定拿到的 `tma_load_initial_state` 的 decltype 会作为 kernel 模板参数传入 K2（[csrc/smxx/fwd_launch.cu:190-199](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L190-L199)），状态精度的差异就这样一路「类型化」地传导进 kernel。

**每个布尔在 kernel 内部点亮/删除的代码。** 四个布尔各自控制的行为，可用下表概括（引用处即 `if constexpr` 的分叉点）：

| 模板参数 | `true` 时的行为 | `false` 时的行为 | 分叉点 |
| --- | --- | --- | --- |
| `IsVarlen`（K1 与 K2 都接收） | 读 `tile_prefix` 前缀和 + 二分查找定位 tile；K2 逐段重算 `tile_base`；launch 前先跑 `_flash_kda_build_tile_prefix` 小 kernel | 等长序列：一次整除即得 `seq_idx`，乘法即得 `tile_base` | [fwd_kernel1.cuh:175-195](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L175-L195)、[fwd_launch.cu:164-167](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L164-L167) |
| `IsVarlen`（K2 侧） | `bos/eos` 查 `cu_seqlens`，`tile_base` 线性累加 | `bos = seq_idx * T_seq` 等纯算术 | [fwd_kernel2.cuh:221-234](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L221-L234) |
| `HasStateIn && !StateFP32` | LOAD warp 直接把 bf16 状态 TMA 载入常驻的 `state_acc` | — | [fwd_kernel2.cuh:241-266](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L241-L266) |
| `HasStateIn && StateFP32` | 先载入 fp32 缓冲，再全线程做 fp32→bf16 的 smem 布局转换 | — | [fwd_kernel2.cuh:267-304](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L267-L304) |
| `HasStateIn == false` | — | 全线程把 `state_acc` 清零 + 代理围栏（零状态起步） | [fwd_kernel2.cuh:305-317](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L305-L317) |
| `HasStateOut && !StateFP32` | STORE warp 直接把 `state_acc` 整块 TMA 写回 | — | [fwd_kernel2.cuh:786-801](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L786-L801) |
| `HasStateOut && StateFP32` | 全线程 bf16→fp32 转换后，STORE warp 发 fp32 TMA | — | [fwd_kernel2.cuh:804-835](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L804-L835) |

以 `IsVarlen` 在 K1 中的分叉为例直观看一下两种生成的代码：

[fwd_kernel1.cuh:175-195](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L175-L195)

```cpp
if constexpr (IsVarlen) {
    int lo = 0, hi = N;
    while (lo + 1 < hi) {                 // O(log N) 二分查找 tile_prefix
        int mid = (lo + hi) >> 1;
        if (tile_prefix[mid] <= global_tile_idx) lo = mid; else hi = mid;
    }
    seq_idx = lo; ...
} else {
    int T_seq = T_total / N;
    int tiles_per_seq = (T_seq + CHUNK - 1) / CHUNK;
    seq_idx = global_tile_idx / tiles_per_seq;   // 一次整除搞定
    ...
}
```

batched 模式下编译出的 kernel 里**根本不存在**二分循环——这正是模板分发的价值。状态三进两出路径的完整机制属于 u3-l6 的内容，本讲只需记住：`HasStateIn`/`HasStateOut`/`StateFP32` 三个布尔在 K2 内部选择的是**整段互斥的 TMA 载入/写出代码**，而不是某个开关值。

#### 4.1.4 代码实践

**实践目标**：验证「同一个 `flash_kda.fwd` Python 入口，底层路由到不同 kernel 实例」最直接的证据——不同模式跑出来的结果行为一致、但配置矩阵必须逐格构造才不触发 u2-l2 的校验报错。

**操作步骤**（示例代码，需已在 SM90 机器上安装 flash_kda）：

```python
# probe_isvarlen.py（示例代码）
import math, torch, torch.nn.functional as F
import flash_kda

D, H, LB = 128, 2, -5.0
def make_qkv(B, T):
    torch.manual_seed(0)
    q = F.normalize(torch.randn((B, T, H, D), device="cuda"), p=2, dim=-1).to(torch.bfloat16)
    k = F.normalize(torch.randn((B, T, H, D), device="cuda"), p=2, dim=-1).to(torch.bfloat16)
    v = torch.randn((B, T, H, D), dtype=torch.bfloat16, device="cuda")
    g = torch.randn((B, T, H, D), dtype=torch.bfloat16, device="cuda")
    beta = torch.randn((B, T, H), dtype=torch.bfloat16, device="cuda")
    A_log = torch.rand(H, dtype=torch.float32, device="cuda")
    dt_bias = torch.rand(H, D, dtype=torch.float32, device="cuda")
    return q, k, v, g, beta, A_log, dt_bias, 1.0 / math.sqrt(D)

# 同样 48 个 token：batched B=3,T=16 vs varlen [4,8,12]
for mode in ("batched", "varlen"):
    if mode == "batched":
        inp = make_qkv(3, 16); cu = None
    else:
        inp = make_qkv(1, 24); cu = torch.tensor([0, 4, 12, 24], dtype=torch.long, device="cuda")
    q, k, v, g, beta, A_log, dt_bias, scale = inp
    out = torch.zeros_like(q)
    flash_kda.fwd(q, k, v, g, beta, scale, out, A_log, dt_bias, LB, cu_seqlens=cu)
    torch.cuda.synchronize()
    print(mode, "out.mean =", out.float().mean().item())
```

**需要观察的现象**：两种模式都正常返回，`out` 均为有限值且量级相同（0.x）。

**预期结果**：batched 分支命中 `launch_fwd<128, false, false, false, false>`，varlen 分支命中 `launch_fwd<128, false, false, false, true>`；两者输出均值接近但不必相等（随机输入、切分方式不同）。若把 batched 模式的 `B` 改为 2 又同时传 `cu_seqlens`，会触发 u2-l2 讲过的 `B must be 1` 校验。待本地验证（本讲义写作环境无 GPU）。

#### 4.1.5 小练习与答案

**练习 1**：用户调用时传 `initial_state=None`、`final_state` 为 fp32 张量。这次调用命中哪组模板实参？kernel 里的状态路径是什么？

**答案**：`(HasStateIn=false, HasStateOut=true, StateFP32=true, IsVarlen=...)`，即七分支中的第 4 支。K2 开头走零初始化路径（全线程清零 `state_acc`），结尾走 fp32 写出路径（bf16→fp32 smem 转换 + TMA store）。它对应的真实场景是「解码/推理链第一步：冷启动、但要落盘精确状态」。

**练习 2**：`fwd.h` 里写了默认实参 `HasStateIn = true` 等，但 `LAUNCH` 宏永远显式传满。这些默认值有什么实际作用？

**答案**：对本项目的调用方式没有作用，纯文档意义（标出「典型配置」）。但 C++ 语法上它们只能出现在声明处（`fwd.h`），定义处（`fwd_launch.cu:6`）不能重复；而且有了默认值，`fwd.h` 的其他潜在使用者可以少写参数。这是一个「声明的默认值不参与显式实例化匹配」的好例子。

**练习 3**：`StateFP32` 的差异为什么不能像 `IsVarlen` 那样「既做模板参数、也可轻易改成运行时布尔」？

**答案**：`StateFP32` 改变的不是一段算法分支，而是 **TMA 描述符的 C++ 类型**（fp32 smem 布局 vs bf16 smem 布局，见 [fwd_launch.cu:120-142](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L120-L142)），描述符类型又作为 kernel 模板类型参数传入 K2。要运行时化就得同时携带两套描述符并在 kernel 内部分支，smem 布局类型也随之膨胀——开销和复杂度都远超收益。

### 4.2 模块二：`DISPATCH_STATE` 七分支与 `state_fp32` 的推导

#### 4.2.1 概念说明

校验通过后，`flash_kda.cpp` 手里有四个运行时布尔：`has_state_in`、`has_state_out`、`state_fp32`、`is_varlen`。要把它们折算成模板实参，需要一个运行时的 if-else 链。这段代码用两个预处理器宏写成：`LAUNCH` 负责「一次调用怎么写」，`DISPATCH_STATE` 负责「七个状态分支怎么排」。

分支数是 7 不是 8 的原因：`(has_state_in, has_state_out, state_fp32)` 理论上有 \(2^3 = 8\) 种组合，但**无状态调用（两者皆 None）时 `state_fp32` 恒为 false**——`state_fp32` 只在检查某个实际存在的状态张量 dtype 时才会被置 true。于是 `(false, false, true)` 这一组不可达，实际可实例化的是 \(8 - 1 = 7\) 种。

#### 4.2.2 核心流程

三个运行时布尔的推导关系：

```text
has_state_in  = initial_state.has_value()
has_state_out =  final_state.has_value()
state_fp32    = false                                  ← 初值
             ← 若 initial_state.dtype == fp32 则置 true
             ← 若  final_state.dtype == fp32 则置 true
（两者都存在时，校验链强制 dtype 相同，所以不会冲突）
```

七分支的判定顺序（先看「有没有」，再看「精度」）：

| # | 分支条件 | 模板实参 (HI, HO, FP32) | 语义 |
| --- | --- | --- | --- |
| 1 | `!in && !out` | `(false, false, false)` | 无状态前向 |
| 2 | `in && out && fp32` | `(true, true, true)` | fp32 状态直通 |
| 3 | `in && out && !fp32` | `(true, true, false)` | bf16 状态直通 |
| 4 | `!in && out && fp32` | `(false, true, true)` | 零起步、fp32 落盘 |
| 5 | `!in && out && !fp32` | `(false, true, false)` | 零起步、bf16 落盘 |
| 6 | `in && !out && fp32` | `(true, false, true)` | fp32 热启动、不落盘 |
| 7 | `else`（即 `in && !out && !fp32`） | `(true, false, false)` | bf16 热启动、不落盘 |

#### 4.2.3 源码精读

**第一步：从可选张量推导三个布尔。**

[csrc/flash_kda.cpp:57-79](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L57-L79)

```cpp
bool has_state_in = initial_state.has_value();
bool has_state_out = final_state.has_value();
bool state_fp32 = false;

if (has_state_in) {
    auto& is = initial_state.value();
    TORCH_CHECK(is.dtype() == torch::kBFloat16 || is.dtype() == torch::kFloat32, ...);
    if (is.dtype() == torch::kFloat32) state_fp32 = true;
}
if (has_state_out) { /* 同理，fp32 时置 true */ }
if (has_state_in && has_state_out) {
    TORCH_CHECK(initial_state->dtype() == final_state->dtype(), ...);
}
```

`state_fp32` 的语义是「**任一**存在的状态张量是 fp32 就算 fp32 模式」，而不是「两者各自独立」。最后一行的同 dtype 校验保证了这个「或」逻辑无歧义：不可能出现 initial 是 bf16、final 是 fp32 的分裂调用。这也解释了为什么没有 `StateFP32In`/`StateFP32Out` 两个独立布尔——上游约定输入输出状态精度必须一致，独立布尔会产生永远非法的组合。

**第二步：`is_varlen` 与 `N` 的确定。**

[csrc/flash_kda.cpp:145-160](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L145-L160)

```cpp
bool is_varlen = cu_seqlens.has_value();
if (is_varlen) {
    TORCH_CHECK(B == 1, "B must be 1 when cu_seqlens is provided");
    N_val = cu_seqlens_t.numel() - 1;      // 序列条数 = 段数
} else {
    N_val = B;                              // batched：每条 batch 一个序列
}
```

注意 varlen 是**最外层**的分发维度（先分 varlen、再分 state），与 state 分支完全正交，所以总实例化数 = \(7 \times 2 = 14\)。

**第三步：两个宏。**

[csrc/flash_kda.cpp:184-216](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L184-L216)

```cpp
#define LAUNCH(HI, HO, FP32, VL) \
    launch_fwd<128, HI, HO, FP32, VL>( \
        q_ptr, k_ptr, v_ptr, g_ptr, beta_t_ptr, \
        initial_state_raw, scale_f, final_state_raw, out_ptr, \
        workspace_ptr, total_tiles, \
        int(T_total), int(H), int(N_val), cu_seqlens_dev, \
        A_log_ptr, dt_bias_ptr, gate_scale, stream)

#define DISPATCH_STATE(VL) \
    if (!has_state_in && !has_state_out) { \
        LAUNCH(false, false, false, VL); \
    } else if (has_state_in && has_state_out && state_fp32) { \
        LAUNCH(true, true, true, VL); \
    } else if (has_state_in && has_state_out && !state_fp32) { \
        LAUNCH(true, true, false, VL); \
    } else if (!has_state_in && has_state_out && state_fp32) { \
        LAUNCH(false, true, true, VL); \
    } else if (!has_state_in && has_state_out && !state_fp32) { \
        LAUNCH(false, true, false, VL); \
    } else if (has_state_in && !has_state_out && state_fp32) { \
        LAUNCH(true, false, true, VL); \
    } else { \
        LAUNCH(true, false, false, VL); \
    }

if (is_varlen) {
    DISPATCH_STATE(true);
} else {
    DISPATCH_STATE(false);
}

#undef DISPATCH_STATE
#undef LAUNCH
```

精读要点：

1. **`LAUNCH` 把 `D` 写死为 128**，与 [flash_kda.cpp:110](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L110) 的 `TORCH_CHECK(D == 128)` 呼应——`D` 虽是模板参数，但当前只实例化了 128 一档；支持其它 head_dim 的影响面见 u3-l12。
2. 分支排列**从「双无」开始、以兜底 `else` 收尾**。最后一个 `else` 承接的正是唯一剩余组合 `(in=true, out=false, fp32=false)`，链首的「双无」分支**不检查 `state_fp32`**（直接传 `false`），依赖的正是 4.2.1 论证的不变量：无状态时 `state_fp32` 必为 false。若在此多写一个 `(false, false, true)` 分支，就会实例化一份永远进不去的代码。
3. 宏在函数体内定义、用完立刻 `#undef`（[flash_kda.cpp:215-216](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L215-L216)），避免 `LAUNCH` 这种通用名污染后续翻译单元。这是预处理宏的卫生习惯；用宏而非普通函数是因为要拼接**模板实参列表**（`launch_fwd<128, HI, HO, ...>` 中的 `HI` 必须逐字面展开）。
4. 分发完成后，**`total_tiles` 的取值在两种模式下含义不同**：[flash_kda.cpp:176-181](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L176-L181) 中 varlen 用上界 \(\lceil T_{total}/16 \rceil + N\)（每条序列至多多算一个尾 tile，K1 里靠 early return 吸收），batched 用精确值 \(N \cdot \lceil T_{seq}/16 \rceil\)。这解释了为什么 varlen 的 K1 grid 会略大于实际 tile 数。

#### 4.2.4 代码实践

**实践目标**：亲眼确认「七分支中每个分支都可被 Python 侧独立触发」，并观察不可达组合 `(false, false, fp32)` 的报错边界。

**操作步骤**（示例代码）：

```python
# probe_branches.py（示例代码）
import math, torch, torch.nn.functional as F
import flash_kda

D, H, T, LB = 128, 2, 64, -5.0
torch.manual_seed(0)
q = F.normalize(torch.randn((1, T, H, D), device="cuda"), p=2, dim=-1).to(torch.bfloat16)
k = F.normalize(torch.randn((1, T, H, D), device="cuda"), p=2, dim=-1).to(torch.bfloat16)
v = torch.randn((1, T, H, D), dtype=torch.bfloat16, device="cuda")
g = torch.randn((1, T, H, D), dtype=torch.bfloat16, device="cuda")
beta = torch.randn((1, T, H), dtype=torch.bfloat16, device="cuda")
A_log = torch.rand(H, dtype=torch.float32, device="cuda")
dt_bias = torch.rand(H, D, dtype=torch.float32, device="cuda")
scale = 1.0 / math.sqrt(D)

cases = {  # (has_in, has_out, dtype_name) -> 分支号
    (False, False, None):  1, (True, True, "fp32"): 2, (True, True, "bf16"): 3,
    (False, True, "fp32"): 4, (False, True, "bf16"): 5,
    (True, False, "fp32"): 6, (True, False, "bf16"): 7,
}
for (hi, ho, dn), no in cases.items():
    dt = {"bf16": torch.bfloat16, "fp32": torch.float32}[dn] if dn else None
    init = torch.randn(1, H, D, D, dtype=dt, device="cuda") if hi else None
    fin  = torch.zeros(1, H, D, D, dtype=dt, device="cuda") if ho else None
    out = torch.zeros_like(q)
    flash_kda.fwd(q, k, v, g, beta, scale, out, A_log, dt_bias, LB,
                  initial_state=init, final_state=fin)
    torch.cuda.synchronize()
    print(f"branch#{no} in={hi} out={ho} dtype={dn}: OK, out.mean={out.float().mean():.4f}")

# 非法组合：in=bf16 + out=fp32（dtype 不一致）
try:
    flash_kda.fwd(q, k, v, g, beta, scale, torch.zeros_like(q), A_log, dt_bias, LB,
                  initial_state=torch.randn(1, H, D, D, dtype=torch.bfloat16, device="cuda"),
                  final_state=torch.zeros(1, H, D, D, dtype=torch.float32, device="cuda"))
except RuntimeError as e:
    print("expected error:", str(e).splitlines()[0])
```

**需要观察的现象**：7 个分支依次打印 OK；最后一组抛出 `initial_state and final_state must have the same dtype`。

**预期结果**：7/7 通过；dtype 混用被校验链拦截——正因为有这道拦截，`state_fp32` 才能用单一布尔表达。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：把链首分支改成 `if (!has_state_in && !has_state_out && !state_fp32) LAUNCH(false,false,false,VL); else if (!has_state_in && !has_state_out) LAUNCH(false,false,true,VL); ...` 会发生什么？

**答案**：功能上仍正确（`state_fp32` 无状态时恒 false，新分支永不命中），但 `LAUNCH(false, false, true, VL)` 会**多实例化一份永远无法到达的 launch_fwd**（及其内部两个 kernel），白白增加编译时间和 .so 体积。实例化表（4.3）也得跟着加一行，否则反而链接失败——这是「运行时不可达 ≠ 编译期不存在」的直接例子。

**练习 2**：为什么 `IsVarlen` 在 `DISPATCH_STATE` 之外再包一层 if，而不是并进同一个 if-else 链（变成 14 个手写分支）？

**答案**：两个维度正交。分层写法用 \(7 + 2\) 个分支表达了 \(7 \times 2 = 14\) 种组合，宏展开一次、复用两次；揉平成 14 分支既难维护又容易漏分支。这也是「宏参数 `VL` 逐字面穿透到 `LAUNCH`」的价值所在。

**练习 3**：varlen 模式下 `total_tiles` 为什么是上界 \(\lceil T_{total}/16 \rceil + N\) 而不是精确值？多出来的 CTA 去哪了？

**答案**：因为序列边界在 device 内存里（`cu_seqlens`），host 侧不想为算精确 tile 数做一次 D2H 同步。上界中每条序列至多多算一个 tile（尾 tile 向上取整的误差 ≤ 1/序列），N 条序列累计至多多 N 个。多出来的 CTA 在 K1 里通过 [fwd_kernel1.cuh:198-199](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L198-L199) 的 `if (local_t >= t_tiles_this_seq) return;` 提前退出。workspace 尺寸同样按上界分配（u2-l2）。

### 4.3 模块三：显式实例化与链接——`fwd_launch.cu` 末尾的那张表

#### 4.3.1 概念说明

现在回答本讲最后一个问题：`flash_kda.cpp`（host 编译器编译）调用 `launch_fwd<128, ...>`，但函数体在 `fwd_launch.cu`（nvcc 编译）里，两个翻译单元如何对接？

答案是教科书式的「声明 + 显式实例化」模式：

- `flash_kda.cpp` include `fwd.h`，只见到声明，编译通过；
- `fwd_launch.cu` 里定义函数体，并在文件末尾**显式枚举**所有会被用到的模板实参组合，强制 nvcc 生成这 14 份机器码并导出符号；
- 链接器把 `flash_kda.cpp` 的调用点与这 14 个符号之一绑定。

如果删掉表里任何一行，编译照常成功，**链接阶段**才报 undefined reference。

#### 4.3.2 核心流程

实例化的组合数逐层放大：

\[ \underbrace{1}_{D=128} \times \underbrace{7}_{\text{state 组合}} \times \underbrace{2}_{\text{varlen/batched}} = 14 \text{ 份 } launch\_fwd \]

每份 `launch_fwd` 内部又实例化：

- 1 份 `_flash_kda_fwd_prepare`（K1 只依赖 `IsVarlen`，去重后实际 2 份）；
- 1 份 `_flash_kda_fwd_recurrence`（K2 依赖全部四个布尔，14 份互不相同）；
- varlen 专属的 `_flash_kda_build_tile_prefix`（非模板）。

编译成本估算：CUTLASS/CuTe 模板的单个 kernel 实例化相当重，14 份 `launch_fwd` × 4 个目标架构（`FLASH_KDA_CUDA_ARCHS=all` 时）= 56 组 K1/K2 编译，这就是 u1-l3 提到「全架构构建明显更慢」的根源。

#### 4.3.3 源码精读

**实例化宏。** [csrc/smxx/fwd_launch.cu:219-238](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L219-L238)

```cpp
// Explicit instantiations
#define INSTANTIATE_LAUNCH_FWD(D, HI, HO, FP32, VL) \
    template void launch_fwd<D, HI, HO, FP32, VL>( \
        cutlass::bfloat16_t const*, cutlass::bfloat16_t const*, \
        /* ...18 个参数的完整类型列表... */ \
        int64_t const*, float const*, float const*, float, cudaStream_t);

#define INSTANTIATE_STATE_VARIANTS(VL) \
    INSTANTIATE_LAUNCH_FWD(128, true,  true,  false, VL) \
    INSTANTIATE_LAUNCH_FWD(128, true,  true,  true,  VL) \
    INSTANTIATE_LAUNCH_FWD(128, false, false, false, VL) \
    INSTANTIATE_LAUNCH_FWD(128, false, true,  false, VL) \
    INSTANTIATE_LAUNCH_FWD(128, true,  false, false, VL) \
    INSTANTIATE_LAUNCH_FWD(128, false, true,  true,  VL) \
    INSTANTIATE_LAUNCH_FWD(128, true,  false, true,  VL)

INSTANTIATE_STATE_VARIANTS(true)   // varlen
INSTANTIATE_STATE_VARIANTS(false)  // non-varlen
```

精读要点：

1. **显式实例化必须写出完整参数类型列表**（不能省略成 `...`），因为它是对一个具体函数符号的显式声明，类型必须与 [fwd.h:7-27](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/fwd.h#L7-L27) 的声明逐参数精确匹配。
2. 表中的 7 行与 `DISPATCH_STATE` 的 7 分支**一一对应**（顺序不必一致，集合必须一致）：`(false,false,true)` 同样不在表中——运行时不可达的组合不实例化，两个文件靠这一不变量保持同步。改分发逻辑时必须同时改这张表，这是一个隐式耦合点（见练习 1）。
3. `INSTANTIATE_STATE_VARIANTS` 被调用两次（`VL=true/false`），把 7 × 2 = 14 份实例压成 7 行宏代码——与分发侧「先 state 后 varlen」的分层完全同构。

**为什么不把定义放进头文件让调用方自行实例化？** 三个理由：

- `flash_kda.cpp` 由 host 编译器以 `cxx` 选项编译（[setup.py:68-69](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/setup.py#L68-L69)），而 `launch_fwd` 的函数体充满 `__global__`、`CUTE_GRID_CONSTANT`、CuTe device 代码，**必须**由 nvcc 编译。把定义移进 `fwd.h` 会让 pybind TU 也变成 CUDA TU，构建脚本随之复杂化。
- 集中在一个 `.cu` 里实例化，14 份（× 每架构）编译只发生一次；若定义散进头文件被多个 TU include，要么重复编译浪费，要么得精心安排显式实例化避免 ODR 问题。
- 这也符合 u1-l3 讲过的「单翻译单元策略」：K1/K2 以 `.cuh` 实现头汇入唯一的 `fwd_launch.cu`，整个扩展的 CUDA 面积收敛在一个编译单元里。

**launch 层另一个（非模板的）开关。** [fwd_launch.cu:147](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L147) 与 [fwd_launch.cu:184](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L184) 的 `#if BLOCK_LEVEL_K1 >= 0` / `#if BLOCK_LEVEL_K2 >= 0`（默认值 1 定义于 [utils.cuh:30-35](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L30-L35)）是**预处理器级**的消融开关，可整体砍掉 K1 或 K2 的启动代码，与模板分发是两套机制（它们的用法留给 u3-l12 的消融实验）。

**那为什么必须编译期分发，运行时 `if` 不行吗？** 把本讲三处证据合起来：

1. **热路径零开销**：`IsVarlen` 的分支位于 K1/K2 每个 CTA 的入口（每 tile 执行一次），`HasStateOut` 的写出分支位于每个 block 的结尾。`if constexpr` 让每份实例只含它需要的指令；运行时布尔则在每个实例里都拖着两条路径的代码与分支。
2. **类型必须分叉**：`StateFP32` 改变 TMA 描述符与 smem 布局的**类型**（4.1.3 / 4.1.5 练习 3），这不是运行时值能表达的。
3. **资源占用精确**：不同实例的寄存器/smem 用量不同，`if constexpr` 让每份实例被 ptxas 独立调度（`--register-usage-level=10`，见 u1-l3），互不拖累。

代价则是本模块的主题：**每新增一个配置维度，实例化数翻倍，且分发宏与实例化表必须手工同步**。

#### 4.3.4 代码实践

**实践目标**：直观感受「删一行实例化 → 链接失败」的因果链，把本模块的论断变成亲手实验。

**操作步骤**：

1. 确认当前安装正常：`python -c "import flash_kda; print('ok')"`。
2. 编辑 `csrc/smxx/fwd_launch.cu`（实验后还原！），注释掉实例化表中的一行，例如：
   `INSTANTIATE_LAUNCH_FWD(128, false, true, true, VL)`（第 6 行，对应「零起步、fp32 落盘」）。
3. 重新安装：`pip install --no-build-isolation -e . 2>&1 | tail -40`（编译仍会成功，注意观察**链接阶段**的报错，形如 `undefined reference to 'void launch_fwd<128, false, true, true, false>(...)'`，由 `flash_kda.cpp.o` 的调用点触发；具体报错文本随平台而异，待本地验证）。
4. 还原该行，重装确认恢复。
5. 附加观察：`nvcc --ptxas-options=-v` 已在默认编译选项中（u1-l3），重装时在输出里搜索 `_flash_kda_fwd_recurrence`，可看到 14 份 K2 实例各自的寄存器/smem 报告行，对比 `(true,true,*)` 与 `(false,false,*)` 实例的资源差异。

**需要观察的现象**：步骤 3 出现链接错误（而非编译错误），且报错符号正是被注释掉的那组模板实参；步骤 5 中不同实例的 `Used N registers` 数字存在差异。

**预期结果**：链接错误的符号名里能读出 `<128, false, true, true, false>`（batched 侧）与/或 `<128, false, true, true, true>`（varlen 侧）两组——因为那一行宏在 `INSTANTIATE_STATE_VARIANTS(true/false)` 里各展开一次。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：如果要新增一种配置（例如独立的 `StateInFP32`/`StateOutFP32`，允许输入输出精度不同），分发侧和实例化侧各要改什么？工作量有多大？

**答案**：`fwd.h` 与 `fwd_launch.cu:6` 的模板签名各加一个 `bool`；`DISPATCH_STATE` 从 7 分支膨胀为最多 \(4 \times 4 - \text{不可达} = 13\) 分支左右（`(false,false,*,*)` 仍只算 1 个）；实例化表同步加到 13 行 × 2 varlen = 26 份 `launch_fwd`；K2 内部新增两条混合精度转换路径（fp32 入 → bf16 出等）。同时 u2-l2 的「同 dtype」校验要删除。可见一个看似小的语义放宽，在编译期分发架构下是全链路的改动——这正是该项目选择「输入输出精度必须一致」约定的原因之一。

**练习 2**：`launch_fwd` 的 14 份实例中，`_flash_kda_fwd_prepare`（K1）为什么去重后只有 2 份？`_flash_kda_fwd_recurrence`（K2）为什么 14 份互不相同？

**答案**：看模板参数列表即可：K1 的模板只接收 `IsVarlen` 一个布尔（[fwd_launch.cu:153-160](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L153-L160)），所以 14 份 `launch_fwd` 内引用的 K1 类型只有 `IsVarlen ∈ {true,false}` 两种；K2 接收全部四个布尔（[fwd_launch.cu:190-199](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L190-L199)），\(7 \times 2 = 14\) 个 (HI,HO,FP32,VL) 组合互异，故 14 份。链接器视角下 K1 符号被多份 `launch_fwd` 共享引用，不重复生成。

**练习 3**：pybind 注册处（[flash_kda.cpp:219-232](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L219-L232)）为什么只注册 `fwd` 和 `get_workspace_size` 两个函数，而不是按 14 种组合各注册一个？

**答案**：分发点收拢在 C++ 内部的 `DISPATCH_STATE`，Python 侧永远只调一个 `fwd`，可选参数天然由 pybind 的 `py::arg(...) = py::none()` 表达。若按组合拆成 14 个入口，Python 包装层（u1-l5）就得自己实现同样的分支逻辑，校验和默认值两处维护；收拢后「Python 看到的是 1 个函数，链接器看到的是 14 个符号」，各取所需。

## 5. 综合实践

写一个 `dispatch_matrix.py`（示例代码），把本讲三个模块串起来：枚举 7 种合法 state 配置 × 2 种序列模式，逐格调用 `flash_kda.fwd`，输出一张 7×2 分发矩阵表，并抽查其中两格与 `tests/torch_ref.py` 的 bit-exact 参考对拍。

```python
# dispatch_matrix.py（示例代码）
# 运行前提：SM90 机器，已 pip install --no-build-isolation -e .（见 u1-l3）
import math, sys, torch, torch.nn.functional as F
import flash_kda

D, H, LB, DEV = 128, 4, -5.0, "cuda"

def make_inputs(B, T):
    torch.manual_seed(42)   # 与 tests/test_fwd_full.py 对齐，便于对拍
    q = F.normalize(torch.randn((B, T, H, D), device=DEV), p=2, dim=-1).to(torch.bfloat16)
    k = F.normalize(torch.randn((B, T, H, D), device=DEV), p=2, dim=-1).to(torch.bfloat16)
    v = torch.randn((B, T, H, D), dtype=torch.bfloat16, device=DEV)
    g = torch.randn((B, T, H, D), dtype=torch.bfloat16, device=DEV)
    beta = torch.randn((B, T, H), dtype=torch.bfloat16, device=DEV)
    A_log = torch.rand(H, dtype=torch.float32, device=DEV)
    dt_bias = torch.rand(H, D, dtype=torch.float32, device=DEV)
    return q, k, v, g, beta, A_log, dt_bias, 1.0 / math.sqrt(D)

# 7 种合法 state 配置：与 DISPATCH_STATE 七分支一一对应
# (名称, has_in, has_out, dtype_name)
CONFIGS = [
    ("no_state        ", False, False, None),
    ("in+out   bf16   ", True,  True,  "bf16"),
    ("in+out   fp32   ", True,  True,  "fp32"),
    ("out_only bf16   ", False, True,  "bf16"),
    ("out_only fp32   ", False, True,  "fp32"),
    ("in_only  bf16   ", True,  False, "bf16"),
    ("in_only  fp32   ", True,  False, "fp32"),
]

def run_one(cfg, mode):
    _, hi, ho, dn = cfg
    if mode == "batched":
        B, T, cu = 3, 64, None
        N = B
        q, k, v, g, beta, A_log, dt_bias, scale = make_inputs(B, T)
    else:  # varlen
        seq_lens = [4, 8, 12]
        N = len(seq_lens)
        cu = torch.tensor([0] + list(torch.cumsum(torch.tensor(seq_lens), 0)),
                          dtype=torch.long, device=DEV)
        q, k, v, g, beta, A_log, dt_bias, scale = make_inputs(1, sum(seq_lens))
    dt = {"bf16": torch.bfloat16, "fp32": torch.float32}[dn] if dn else None
    init = torch.randn(N, H, D, D, dtype=dt, device=DEV).to(dt) if hi else None
    fin  = torch.zeros(N, H, D, D, dtype=dt, device=DEV) if ho else None
    out = torch.zeros_like(q)
    flash_kda.fwd(q, k, v, g, beta, scale, out, A_log, dt_bias, LB,
                  initial_state=init, final_state=fin, cu_seqlens=cu)
    torch.cuda.synchronize()
    return bool(torch.isfinite(out.float()).all()), out

if __name__ == "__main__":
    print(f"{'config':<18} | {'batched (VL=false)':<20} | varlen (VL=true)")
    print("-" * 64)
    for cfg in CONFIGS:
        cells = []
        for mode in ("batched", "varlen"):
            try:
                ok, _ = run_one(cfg, mode)
                cells.append("PASS" if ok else "FAIL(nonfinite)")
            except Exception as e:
                cells.append("ERR: " + str(e).splitlines()[0][:24])
        print(f"{cfg[0]:<18} | {cells[0]:<20} | {cells[1]}")

    # 可选加深：任选一格与 torch_ref 做 bit-exact 对拍（需能 import 到 tests 目录）
    # sys.path.insert(0, "tests"); from torch_ref import torch_ref
    # 复制 tests/test_fwd_full.py 中 test_fwd_fixed 的断言写法：torch.equal(out_kernel, out_ref)
```

**实践目标**：证明 14 个格子全部 PASS——即 `DISPATCH_STATE` × `IsVarlen` 的每个编译实例都能从 Python 侧唯一触达，且不存在「编译了却到不了」或「到了却没编译」的格子。

**操作步骤**：

1. 按 u1-l3 完成安装；把上面脚本保存到仓库任意位置（不要放进 `tests/`，避免被 pytest 收集）。
2. 运行 `python dispatch_matrix.py`。
3. 观察表格输出；如需数值级验证，取消末尾注释并参照 [tests/test_fwd_full.py:70-94](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test_fwd_full.py#L70-L94) 的 `torch.equal` 断言扩展脚本。

**需要观察的现象**：7 行 × 2 列全为 PASS；varlen 列与 batched 列使用相同的 state 配置但不同的 `cu_seqlens`/形状（batched 状态 `[3,H,D,D]`，varlen 状态 `[3,H,D,D]`——形状恰好一致但语义不同：前者每 batch 一份，后者每序列一份）。

**预期结果**：14/14 PASS。若某格 ERR 且报错来自 u2-l2 的校验链（如 dtype/shape），说明该格的输入构造与本讲的配置约定不符，应修脚本而非怀疑分发。待本地验证（写作环境无 GPU）。

## 6. 本讲小结

- `launch_fwd` 的五个模板参数中，`HasStateIn`/`HasStateOut`/`StateFP32` 描述状态 IO 的有无与精度，`IsVarlen` 描述序列组织方式；`D` 当前写死 128。
- `state_fp32` 由「任一存在的状态张量是否为 fp32」推导；由于无状态时它恒为 false，(HI, HO, FP32) 的 8 种组合中 `(false,false,true)` 不可达，故分发是 7 分支、实例化是 7 × 2 = 14 份。
- `DISPATCH_STATE` 是运行时 if-else 链 + 宏拼接模板实参；`LAUNCH` 宏把 `D=128`、指针包与 stream 打包成一次调用；`#undef` 收尾保证宏卫生。
- `StateFP32` 的差异是**类型级**的（TMA 描述符与 smem 布局不同），这也是状态精度必须编译期分发、不能运行时分支的根本原因之一。
- 显式实例化表（`INSTANTIATE_STATE_VARIANTS`）与分发宏是**隐式耦合**的一对：改一侧必须同步另一侧，否则编译成功、链接失败。
- 编译期分发的收益：热路径死代码消除、类型可分叉、每份实例独立调度寄存器；代价：实例数随配置维度指数增长、构建时间随架构数线性放大。

## 7. 下一步学习建议

- 下一讲 u2-l4「读懂 CuTe」将拆开 `K1Layouts`/`K2Layouts`，本讲反复出现的 `TMAStateSmemLayout`/`TMAFP32StateSmemLayout` 等类型届时会得到完整解释——建议先记住本讲的结论「类型不同」再去看「为何不同」。
- 若想先看模板布尔在 kernel 内的完整效果，可直接跳读 [csrc/smxx/fwd_kernel2.cuh:241-317](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L241-L317)（状态三进入口），但其布局细节依赖 u2-l4。
- 对「编译期分发 vs 运行时分支」想有更多体感的读者，可以对比 flash-linear-attention 中 Triton kernel 用 `tl.constexpr` 表达同类配置的方式（Triton 的 constexpr 同样触发特化编译，思想同源）。
- 本讲的实例化表是 u3-l12 消融实验的邻居：`BLOCK_LEVEL_K1/K2` 与 `TMA_DISABLE_ALL` 是另一套（预处理级）开关，做消融时注意区分两套机制。
