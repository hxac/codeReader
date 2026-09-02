# 模板参数分发：7 种 state 组合 × varlen 的实例化

## 1. 本讲目标

上一讲（u2-l2）我们读完了 `fwd` 入口的前半段：一连串 `TORCH_CHECK` 校验和三项布局预处理。校验通过之后、真正启动 kernel 之前，还有一道关键工序——**从 14 份预先编译好的 kernel 实现中，挑出与本次调用匹配的那一份**。

本讲结束时，你应该能够：

1. 说出 `launch_fwd` 模板的 5 个模板参数（`D`、`HasStateIn`、`HasStateOut`、`StateFP32`、`IsVarlen`）各自控制 kernel 的哪一段行为。
2. 手工推导：给定一次 `flash_kda.fwd` 调用的参数（给了哪些状态张量、什么 dtype、有没有 `cu_seqlens`），它会命中 `DISPATCH_STATE` 七个分支中的哪一个。
3. 解释为什么是 **7** 种 state 组合而不是 8 种，以及为什么总共实例化 **14** 份 `launch_fwd`。
4. 解释「显式实例化」（explicit instantiation）在 `fwd_launch.cu` 末尾解决了什么链接问题，为什么 `flash_kda.cpp` 不直接 `#include` kernel 代码。
5. 论证为什么这些选择必须放在**编译期**（模板 + `if constexpr`），而不是运行时 `if`——核心原因是 TMA 描述符的 **C++ 类型**本身随配置改变。

---

## 2. 前置知识

### 2.1 模板不是代码，是「生成代码的配方」

C++ 模板（function template / class template）本身不产生任何机器码。编译器只有看到「模板 + 一组具体模板实参」（比如 `launch_fwd<128, true, false, false, true>`）时，才会按配方**实例化**（instantiate）出一份真正的函数。同一份模板配上不同实参，生成的是完全独立的函数体，各自编译、各自优化。

### 2.2 `if constexpr`：编译期裁剪

```cpp
if constexpr (HasStateIn) {
    // 只有 HasStateIn == true 的实例化里，这段代码才被编译
} else {
    // HasStateIn == false 的实例化里，只编译这一段
}
```

与运行时 `if` 不同，`if constexpr` 的**另一侧分支根本不进入编译产物**——不是「运行时跳过」，而是「代码不存在」。这对 GPU kernel 极其重要：不存在的代码不占寄存器、不占共享内存、不需要 warp 发散处理。

### 2.3 声明与定义分离时的「显式实例化」

普通函数只要在一个 `.cpp` 里定义、在头文件里声明，链接器就能找到符号。模板不同：**编译器只会在「看得见模板定义」的编译单元里实例化**。如果 A.cpp 只 include 了模板声明就去调用 `launch_fwd<...>`，A.cpp 里不会生成函数体，链接时报 undefined symbol。

解决办法有两种：

- **header-only**：把定义放进 `.h`，让每个调用方都能实例化（多个编译单元生成同一实例时由链接器去重）。
- **显式实例化**：定义留在 `.cu`/`.cpp` 里，在其末尾用 `template void launch_fwd<128, true, true, false, true>(...);` 这样的语句**点名要求生成某几份实例**。调用方只 include 声明，链接到这些现成符号。

FlashKDA 选择了后者，原因见 4.3。

### 2.4 为什么 GPU kernel 偏爱编译期分支

三个在 CPU 代码里不突出、在 CUDA kernel 里致命的理由：

1. **类型不同**：TMA 描述符由 `make_tma_copy(算子, gmem 张量, smem 布局)` 生成，bf16 状态和 fp32 状态产出的描述符是**不同的 C++ 类型**。类型无法用运行时 `if` 切换，只能走模板。
2. **资源不同**：不同配置需要的共享内存布局、同步 barrier 数量不同。运行时分支要求所有路径的资源并存，可能直接超过 48KB/228KB 的 smem 上限。
3. **同步语句不能进发散分支**：`__syncthreads()` 若被运行时条件包裹，一旦部分线程走另一侧就是未定义行为。`if constexpr` 保证整块线程（乃至整个实例化）看到的代码一致，天然安全。

---

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| [csrc/fwd.h](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/fwd.h) | `launch_fwd` 的**模板声明**（仅 27 行） | 5 个模板参数与默认值 |
| [csrc/flash_kda.cpp](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp) | pybind 入口（host 编译单元） | 运行时布尔推导 + `DISPATCH_STATE` 七分支 |
| [csrc/smxx/fwd_launch.cu](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu) | `launch_fwd` 的**模板定义** + 两次 kernel 启动（device 编译单元） | 定义端签名、哑指针技巧、末尾 14 个显式实例化 |
| [csrc/smxx/fwd_kernel2.cuh](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh) | Kernel 2（递推）实现头 | 4 个布尔参数如何用 `if constexpr` 改变形为 |
| [csrc/smxx/fwd_kernel1.cuh](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh) | Kernel 1（准备）实现头 | `IsVarlen` 在 K1 侧的二分查找 |
| [tests/test_fwd.py](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test_fwd.py) | 正确性测试 | 实践任务的输入构造模板 |

两个编译单元的关系（承接 u1-l4 的单翻译单元策略）：

```text
flash_kda.cpp  (host 单元)          fwd_launch.cu  (device 单元)
  include "fwd.h" ──────────────────► include "fwd.h"
  调用 launch_fwd<128, HI, HO,        include "fwd_kernel1.cuh"
                 FP32, VL>()          include "fwd_kernel2.cuh"
       │                              定义 launch_fwd 模板
       │  链接期解析符号               末尾显式实例化 14 份
       └──────────────────────────────────► 14 个具体符号
```

---

## 4. 核心概念与源码讲解

### 4.1 launch_fwd 模板签名

#### 4.1.1 概念说明

`launch_fwd` 是 Python 层与 CUDA kernel 之间的**最后一层 C++ 函数**：它负责构造 TMA 描述符、切分 workspace、启动 K1/K2 两个 kernel。它的行为由 5 个模板参数决定，其中 4 个是布尔开关：

| 模板参数 | 类型 | 控制什么 |
| --- | --- | --- |
| `D` | `int` | head 维度。当前硬编码为 128（校验链里 `TORCH_CHECK(D == 128)`，见 u2-l2） |
| `HasStateIn` | `bool` | K2 开头是否加载 `initial_state`（三条路径：bf16 直载 / fp32 转换载 / 清零） |
| `HasStateOut` | `bool` | K2 结尾是否把最终状态写出（两条路径：bf16 直存 / fp32 转存） |
| `StateFP32` | `bool` | 状态张量在**全局内存**中的精度（bf16 或 fp32），决定 TMA 描述符类型与转换缓冲 |
| `IsVarlen` | `bool` | 是否变长模式（有 `cu_seqlens`），决定 K1/K2 的 tile 寻址方式，还控制是否启动第三个辅助 kernel |

注意语义分工：`HasStateIn`/`HasStateOut` 说的是「**有没有**这次 IO」，`StateFP32` 说的是「IO 的**精度**」。二者正交组合，再乘上 `IsVarlen`，就是全部实例。

#### 4.1.2 核心流程

一次 `flash_kda.fwd` 调用中，模板实参的确定过程：

```text
Python 侧传参                    C++ 侧推导                    模板实参
─────────────────────────────────────────────────────────────────────
initial_state 是否为 None   →  has_state_in               →  HasStateIn
final_state   是否为 None   →  has_state_out              →  HasStateOut
状态张量 dtype 是否 fp32    →  state_fp32                 →  StateFP32
cu_seqlens   是否为 None    →  is_varlen                  →  IsVarlen
D == 128（校验保证）                                        →  D
```

关键点：**模板实参全部来自「参数的存在性」和「dtype」这类零成本可判定的元信息**，不需要看任何张量的数值。这也是为什么分发能放在 host 侧一行 `if` 链里完成。

#### 4.1.3 源码精读

先看声明端 [csrc/fwd.h:6-27](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/fwd.h#L6-L27)：模板声明带默认实参（`HasStateIn = true`、`HasStateOut = true`、`StateFP32 = false`、`IsVarlen = true`），函数体参数全是裸指针与标量——两个状态指针是 `void const*`/`void*`，**精度信息不在指针类型里，只存在于模板参数中**：

```cpp
template <int D, bool HasStateIn = true, bool HasStateOut = true,
          bool StateFP32 = false, bool IsVarlen = true>
void launch_fwd(
    cutlass::bfloat16_t const* q_ptr,
    ...
    void const* initial_state_ptr,   // 精度由 StateFP32 表达
    ...
    void* final_state_ptr,
    ...);
```

再看定义端 [csrc/smxx/fwd_launch.cu:6-27](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L6-L27)：定义端写的是 `template <int D, bool HasStateIn, bool HasStateOut, bool StateFP32, bool IsVarlen>`，**不带默认值**。C++ 规定默认模板实参在声明与定义中只能出现一次——本项目选择放在头文件里，方便少数内部调用省写参数，而显式实例化时永远全部显式给出。

模板参数在定义体内最直接的消费点是两次 kernel 启动。Kernel 1 只接收 `IsVarlen`（[csrc/smxx/fwd_launch.cu:153-160](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L153-L160)）——因为 K1 只做块内准备、完全不触碰状态：

```cpp
auto kernel1 = _flash_kda_fwd_prepare<
    decltype(tma_load_q), decltype(tma_load_k), ...,
    CHUNK, D, kK1Threads, IsVarlen>;      // 只有 IsVarlen 一个布尔
```

Kernel 2 则接收全部 4 个布尔（[csrc/smxx/fwd_launch.cu:190-199](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L190-L199)），因为状态的加载与写出都发生在 K2。

还有一个容易被忽略的细节：`IsVarlen` 不仅影响 device 代码，还控制 **host 侧是否启动第三个辅助 kernel**。[csrc/smxx/fwd_launch.cu:164-167](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L164-L167) 中，只有 varlen 实例化才会启动 32 线程的 `_flash_kda_build_tile_prefix`（u2-l6 会精读它）：

```cpp
if constexpr (IsVarlen) {
    _flash_kda_build_tile_prefix<<<1, 32, 0, stream>>>(
        cu_seqlens_ptr, N, CHUNK, ws_tile_prefix);
}
```

最后看哑指针技巧。[csrc/smxx/fwd_launch.cu:131-136](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L131-L136)：构造 TMA 描述符需要合法的全局内存地址，但 `HasStateIn = false` 时根本没有初始状态张量，于是拿 `out_ptr` 充当地址构造一个**永远不会被使用**的哑描述符：

```cpp
auto state_ptr_load = HasStateIn
    ? static_cast<BF16 const*>(initial_state_ptr)
    : reinterpret_cast<BF16 const*>(out_ptr);  // dummy, never used
```

这个「never used」的保证来自 kernel 内的 `if constexpr`（见 4.2.3）：使用该描述符的代码路径在 `HasStateIn = false` 的实例化里已被整体裁掉，哑指针只是让**描述符构造本身**（在 host 侧无条件执行）不崩溃。

#### 4.1.4 代码实践

**实践目标**：确认「模板实参完全由参数元信息决定」这一论断，并观察哑指针策略下 stateless 调用不会出错。

**操作步骤**：

1. 打开 [csrc/smxx/fwd_launch.cu:119-144](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L119-L144) 的 `make_state_tma` lambda，对照 [csrc/smxx/fwd_kernel2.cuh:241-317](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L241-L317) 的三个 `if constexpr` 分支，画一张「模板组合 → 描述符类型 → kernel 内路径」对照表。
2. 运行 `tests/test_fwd.py` 中的 `test_fwd`（它给了 bf16 的 `initial_state` 与 `final_state`，即 `(true, true, false)` 组合），确认通过。

**需要观察的现象**：`make_state_tma` 在 host 侧对每种实例化都构造了 load 和 store 两个描述符——即使 `HasStateIn = false` 也会构造 load 描述符（用哑指针）。

**预期结果**：stateless 模式下程序正常运行，无非法地址访问。因为哑描述符在 kernel 内无任何使用点。

**本实践依赖 GPU 环境，运行结果待本地验证。**

#### 4.1.5 小练习与答案

**练习 1**：`initial_state_ptr` 为什么声明成 `void const*` 而不是 `cutlass::bfloat16_t const*`？

**答案**：状态张量有两种合法 dtype（bf16 与 fp32），指针类型无法同时表达两者。精度信息改由模板参数 `StateFP32` 携带，定义端在 [csrc/smxx/fwd_launch.cu:120-127](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L120-L127) 里 `static_cast<float const*>` 或 [131-133](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L131-L133) 里 `static_cast<BF16 const*>` 还原。这是「类型信息上移到模板参数」的典型手法。

**练习 2**：如果把 `fwd_launch.cu:6` 定义端的模板参数也写上默认值（`bool HasStateIn = true`），会发生什么？

**答案**：编译错误。C++ 标准禁止默认模板实参在声明和定义中重复出现（同一默认值也不行）。默认实参只能给一次，本项目给在了 `fwd.h` 的声明端。

**练习 3**：为什么 Kernel 1 不需要 `HasStateIn`/`HasStateOut`/`StateFP32`？

**答案**：双 kernel 分工（u1-l4）中 K1 只做块内准备（L2 归一化、门控 cumsum、decay 家族、L/Mqk/INV 构造），状态递推全部在 K2。状态 IO 只发生在 K2 的开头与结尾，所以这 3 个参数对 K1 无意义——不传它们还能减少 K1 的实例化数量（K1 只有 2 份：varlen 与 batched）。

---

### 4.2 DISPATCH_STATE 七分支

#### 4.2.1 概念说明

`DISPATCH_STATE` 是 `flash_kda.cpp` 里的一个预处理器宏，它把 3 个运行时布尔（`has_state_in`、`has_state_out`、`state_fp32`）翻译成对 `launch_fwd` 的正确模板实参。它要覆盖的组合数是：

\[ 4 \text{ 种 (in, out) 存在性组合} \times 2 \text{ 种 dtype} - 1 \text{ 种不可能组合} = 7 \]

那个「不可能的组合」是 `(无 in, 无 out, fp32)`：`state_fp32` 只能从**实际传入**的状态张量的 dtype 推导出来，两个状态都没给时它永远是 `false`。所以是 7 而不是 8。

#### 4.2.2 核心流程

运行时布尔从哪来？三段逻辑：

1. **存在性**：[csrc/flash_kda.cpp:57-59](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L57-L59) 直接由 `std::optional::has_value()` 得到 `has_state_in`/`has_state_out`，`state_fp32` 初始化为 `false`。
2. **精度推导**：[csrc/flash_kda.cpp:61-74](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L61-L74) 校验存在的状态张量 dtype 必须是 bf16 或 fp32；**任一**状态张量是 fp32 就把 `state_fp32` 置 `true`。
3. **一致性**：[csrc/flash_kda.cpp:76-79](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L76-L79) 若两个状态都给，dtype 必须一致（否则报错），因此 `state_fp32` 不会有歧义。

varlen 判定在 [csrc/flash_kda.cpp:145-160](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L145-L160)：`cu_seqlens` 存在即 varlen（且要求 `B == 1`、int64），`N` 取 `cu_seqlens.numel() - 1`；否则 `N = B`。

随后分发分两层展开：外层 [csrc/flash_kda.cpp:209-213](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L209-L213) 用运行时 `is_varlen` 选 `DISPATCH_STATE(true)` 或 `DISPATCH_STATE(false)`，内层再按 state 组合走七分支。完整映射表：

| # | has_state_in | has_state_out | state_fp32 | 典型调用场景 |
| --- | --- | --- | --- | --- |
| 1 | false | false | false | 纯 stateless 前向（只关心 out） |
| 2 | true | true | false | 训练 / 推理双向传递，bf16 状态 |
| 3 | true | true | true | 训练 / 推理双向传递，fp32 状态 |
| 4 | false | true | true | 只收集最终状态，fp32 精度 |
| 5 | false | true | false | 只收集最终状态，bf16 |
| 6 | true | false | true | 只注入初始状态，fp32（如接力解码但状态由外部管理） |
| 7 | true | false | false | 只注入初始状态，bf16 |

#### 4.2.3 源码精读

分发宏本体在 [csrc/flash_kda.cpp:184-216](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L184-L216)。`LAUNCH` 宏（L184-190）把 4 个布尔填进模板实参列表，`D` 硬编码为 128，并把前面准备好的所有指针、`total_tiles`、stream 一并转发：

```cpp
#define LAUNCH(HI, HO, FP32, VL) \
    launch_fwd<128, HI, HO, FP32, VL>( \
        q_ptr, k_ptr, v_ptr, g_ptr, beta_t_ptr, \
        initial_state_raw, scale_f, final_state_raw, out_ptr, \
        workspace_ptr, total_tiles, ...)
```

`DISPATCH_STATE` 宏（L192-207）是一条 `if / else if` 链，分支条件就是上表 7 行的逐字翻译：

```cpp
#define DISPATCH_STATE(VL) \
    if (!has_state_in && !has_state_out) { \
        LAUNCH(false, false, false, VL); \        // 组合 1
    } else if (has_state_in && has_state_out && state_fp32) { \
        LAUNCH(true, true, true, VL); \           // 组合 3
    } else if (has_state_in && has_state_out && !state_fp32) { \
        LAUNCH(true, true, false, VL); \          // 组合 2
    } else if (!has_state_in && has_state_out && state_fp32) { \
        LAUNCH(false, true, true, VL); \          // 组合 4
    } else if (!has_state_in && has_state_out && !state_fp32) { \
        LAUNCH(false, true, false, VL); \         // 组合 5
    } else if (has_state_in && !has_state_out && state_fp32) { \
        LAUNCH(true, false, true, VL); \          // 组合 6
    } else { \
        LAUNCH(true, false, false, VL); \         // 组合 7（兜底 else）
    }
```

两个值得注意的写法细节：

- **7 个条件互斥且完备**，所以分支顺序理论上可以任意重排，结果不变；最后一个分支因此可以省略条件直接写 `else`。作者把最复杂的 `(true, false, false)` 留作 `else` 兜底——前提正是前 6 个条件已把其余组合全部排除。这也意味着**改动任何一个条件都要重新核对完备性**，否则错误组合会被 `else` 静默吞掉。
- 宏用完立刻 [csrc/flash_kda.cpp:215-216](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L215-L216) `#undef` 清理，避免宏名泄漏到其他翻译单元（`DISPATCH_STATE(VL)` 这种带参宏如果与 CUTLASS 头文件中的名字撞车会产生极难定位的编译错误）。

再看 kernel 侧，模板参数是如何用 `if constexpr` 改变形为的。K2 的模板参数列表见 [csrc/smxx/fwd_kernel2.cuh:128-131](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L128-L131)。**输入侧三条互斥路径**：

- bf16 直载（[csrc/smxx/fwd_kernel2.cuh:241-266](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L241-L266)）：`HasStateIn && !StateFP32` 时 LOAD warp 用 TMA 把 bf16 状态直接搬进常驻的 `state_acc` 共享内存，全块等一个事务 barrier。
- fp32 转换载（[csrc/smxx/fwd_kernel2.cuh:267-304](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L267-L304)）：`HasStateIn && StateFP32` 时先 TMA 载入 fp32 到 `state_fp32_buf`，再由全体线程做 `smem_cvt_fp32_to_bf16` 布局转换——多了两步同步和一次转换。
- 清零（[csrc/smxx/fwd_kernel2.cuh:305-317](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L305-L317)）：无输入状态时全线程把 `state_acc` 清零，连 TMA 都不用。

**输出侧两条路径**：bf16 直存（[csrc/smxx/fwd_kernel2.cuh:786-801](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L786-L801)）与 fp32 转存（[csrc/smxx/fwd_kernel2.cuh:804-834](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L804-L834)，先 `smem_cvt_bf16_to_fp32` 再由 STORE warp 发 TMA）。

`IsVarlen` 则改变**寻址算法**。K2 侧（[csrc/smxx/fwd_kernel2.cuh:221-234](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L221-L234)）：varlen 时每个块从 `cu_seqlens` 读出本序列的 `[bos, eos)` 并线性扫描累出 `tile_base`；batched 时退化为整除映射 `bos = seq_idx * T_seq`。K1 侧（[csrc/smxx/fwd_kernel1.cuh:175-195](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L175-L195)）则是「二分查找 `tile_prefix`」对「整除映射」的切换（u2-l6 精读）。

#### 4.2.4 代码实践

**实践目标**：验证「运行时 if 链无法替代这套机制」中的类型障碍——用 `nm` 观察 14 份实例化确实是 14 个不同符号。

**操作步骤**：

1. 找到已安装的扩展模块：`python -c "import flash_kda_C, inspect; print(flash_kda_C.__file__)"`。
2. 对该 `.so` 执行：`nm -C <so路径> | grep launch_fwd | head -20`（若符号被 strip 则改用构建目录下的 `fwd_launch.o`：`nm build/temp.*/csrc/smxx/fwd_launch.o | grep launch_fwd | wc -l`）。

**需要观察的现象**：输出中应出现多行形如 `launch_fwd<128, true, true, false, true>` 的符号（`-C` 会还原可读的模板名），计数为 14。

**预期结果**：14 个 `launch_fwd` 实例符号——7 种 state 组合 × varlen/batched。这正是 4.3 要讲的显式实例化的直接产物。

**待本地验证**（符号是否可见取决于构建配置；`.o` 文件路径随 Python/CUDA 版本变化）。

#### 4.2.5 小练习与答案

**练习 1**：调用 `flash_kda.fwd` 时只传了 fp32 的 `final_state`（不传 `initial_state`）和 `cu_seqlens`，会命中哪个分支？模板实参是什么？

**答案**：`has_state_in = false`、`has_state_out = true`、`state_fp32 = true`（由 [csrc/flash_kda.cpp:68-74](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L68-L74) 从 final_state 的 fp32 dtype 推出）、`is_varlen = true`。命中组合 4 分支，展开为 `LAUNCH(false, true, true, true)`，即 `launch_fwd<128, false, true, true, true>`。

**练习 2**：如果传了 bf16 的 `initial_state` 和 fp32 的 `final_state`，会发生什么？

**答案**：被 [csrc/flash_kda.cpp:76-79](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L76-L79) 的 `TORCH_CHECK` 拦下，抛出 "initial_state and final_state must have the same dtype"。注意在校验顺序上，dtype 合法性检查（L64/L71）先于一致性检查（L77），所以「bf16 + fp32」会走到一致性报错，而「int32 状态」会更早报 dtype 非法——这条「先失败先报」链在 u2-l2 已详细分析。

**练习 3**：为什么 `DISPATCH_STATE` 的分支里，`stateless`（组合 1）不检查 `state_fp32`？

**答案**：因为 `(false, false, true)` 是不可能的组合——`state_fp32` 只能被 L61-74 中「实际存在的状态张量」置位，两个状态都没有时它在 L59 初始化为 `false` 后不会再变。所以第一个分支直接写 `LAUNCH(false, false, false, VL)` 即可，无需多余条件。

---

### 4.3 显式实例化与链接

#### 4.3.1 概念说明

显式实例化解决的问题是：**`flash_kda.cpp`（调用方）与 `fwd_launch.cu`（定义方）是两个独立的编译单元，调用方看不到模板定义，链接器必须有现成的符号可指**。

`fwd_launch.cu` 末尾的实例化表把「这套库支持哪 14 种调用形态」**固化成了链接期契约**：只要 `flash_kda.cpp`（或未来任何 host 代码）调用的组合在表内，链接成功；调到表外的组合，直接链接失败——错误在编译/链接期暴露，而不是运行时。

#### 4.3.2 核心流程

构建期发生的事：

```text
nvcc 编译 flash_kda.cpp
  ├─ include "fwd.h"（只依赖 cutlass/bfloat16.h，不含 kernel 代码）
  └─ 产出：对 14 个 launch_fwd<...> 符号的重定位引用（无定义）

nvcc 编译 fwd_launch.cu
  ├─ include 两个 kernel 实现头（拖入全部 CUTLASS/CuTe 头链）
  ├─ 隐式实例化：无（模板定义本身不产码）
  └─ 显式实例化 14 份 → 产出 14 个具体函数符号（每份含两个 kernel 的启动代码）

链接 → flash_kda.cpp 的调用点逐个绑定到 14 个符号上
```

实例化数量：7 种 state 组合 × 2 种 varlen = 14，即 \[ (4 \times 2 - 1) \times 2 = 14 \]。

#### 4.3.3 源码精读

实例化表在 [csrc/smxx/fwd_launch.cu:219-238](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L219-L238)，两层宏嵌套：

```cpp
// 显式实例化的语法：template + 函数签名 + 全部模板实参
#define INSTANTIATE_LAUNCH_FWD(D, HI, HO, FP32, VL) \
    template void launch_fwd<D, HI, HO, FP32, VL>( \
        cutlass::bfloat16_t const*, ...完整参数类型列表..., cudaStream_t);

#define INSTANTIATE_STATE_VARIANTS(VL) \
    INSTANTIATE_LAUNCH_FWD(128, true,  true,  false, VL) \
    INSTANTIATE_LAUNCH_FWD(128, true,  true,  true,  VL) \
    INSTANTIATE_LAUNCH_FWD(128, false, false, false, VL) \
    INSTANTIATE_LAUNCH_FWD(128, false, true,  false, VL) \
    INSTANTIATE_LAUNCH_FWD(128, true,  false, false, VL) \
    INSTANTIATE_LAUNCH_FWD(128, false, true,  true,  VL) \
    INSTANTIATE_LAUNCH_FWD(128, true,  false, true,  VL)

INSTANTIATE_STATE_VARIANTS(true)   // varlen：7 份
INSTANTIATE_STATE_VARIANTS(false)  // non-varlen：7 份
```

注意 `INSTANTIATE_LAUNCH_FWD` 里必须**逐字重复完整的函数参数类型列表**——显式实例化要求编译器能无歧义地指认函数，不能依赖默认参数或省略。

这张表的 7 行与 `DISPATCH_STATE` 的 7 个分支**一一对应**。这是本讲最重要的一条对应关系：host 侧 `if` 链的每一个分支，都在实例化表里有且仅有一行。两边任何一边增删组合（例如未来支持 D=64，或新增「只出不进 + fp32」之外的形态）都必须同步修改另一边，否则要么链接错误（表少了），要么死代码（表多了，没人调用）。

为什么不用 header-only 方案（把定义搬进 `fwd.h`）？三个理由：

1. **隔离重依赖**：`fwd_launch.cu` include 了 `fwd_kernel1.cuh`/`fwd_kernel2.cuh`，进而拖入整个 CUTLASS/CuTe 头链与两个 kernel 共约 1400 行设备代码。header-only 会让 `flash_kda.cpp` 也背上这一切，任何 kernel 头文件的改动都会触发两个编译单元全量重编。
2. **控制实例化数量**：显式实例化把实例数量钉死在 14。header-only 下每个调用方编译单元都可能产生自己的隐式实例，虽然链接器最终去重，但编译期的开销与不可控性更高。
3. **编译器选择**：`flash_kda.cpp` 是纯 host 代码，当前方案下它几乎不依赖 CUDA 设备编译路径；这与 u1-l3 讲过的「host 侧 `flash_kda.cpp` + 唯一 `.cu` 文件 `fwd_launch.cu`」的双编译单元构建结构互为表里。

最后把「编译期 vs 运行时」的论证收拢成一张表，这也是本讲的学习目标之三：

| 维度 | 运行时 `if` | 编译期模板 + `if constexpr`（本项目） |
| --- | --- | --- |
| TMA 描述符类型 | 无法切换（bf16/fp32 是不同 C++ 类型） | 每种实例化持有自己的描述符类型 |
| 状态 IO 路径 | 三条路径代码并存，寄存器/smem 都要预留 | 未选中的路径不存在（4.2.3 的三分支） |
| `__syncthreads` 安全 | 分支内同步有死锁风险 | 整块线程看到同一份代码，天然安全 |
| smem 布局 | 无法用 union 复用（[csrc/smxx/fwd_kernel2.cuh:99-107](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L99-L107) 中 fp32 转换缓冲与流水线缓冲的 union 复用依赖「路径编译期互斥」的保证） | union 复用合法且有据可依 |
| 哑指针安全性 | 「never used」无编译期保证 | 使用点被 `if constexpr` 裁掉，可证明安全 |
| 代价 | — | 二进制体积 ×14、编译时间增加 |

#### 4.3.4 代码实践

**实践目标**：亲手制造一次「表外组合」的链接错误，理解实例化表是链接期契约。

**操作步骤**：

1. 在 [csrc/smxx/fwd_launch.cu:237-238](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L237-L238) 处临时注释掉 `INSTANTIATE_STATE_VARIANTS(false)` 这一行（即删掉全部 7 个 batched 实例）。
2. 重新构建：`pip install --no-build-isolation .`（或直接重跑 u1-l3 的安装流程）。
3. 观察链接器输出。
4. **改完后务必还原**（本讲义禁止修改源码，此实验应在本地副本进行）。

**需要观察的现象**：链接阶段报 undefined reference / unresolved external symbol，符号名里包含 `launch_fwd<128, true, true, false, false>` 之类的模板实参——因为 `DISPATCH_STATE(false)` 展开的 7 处调用都找不到定义。

**预期结果**：链接失败，错误信息逐条列出 7 个缺失的 batched 符号。这从反面证明了：`flash_kda.cpp` 的调用之所以能链接，完全依赖实例化表逐行供给符号。

**待本地验证**（需要完整构建环境与 GPU 机器；不同链接器报错措辞不同）。

#### 4.3.5 小练习与答案

**练习 1**：如果把实例化表删掉一行 `INSTANTIATE_LAUNCH_FWD(128, false, false, false, true)`（varlen 的 stateless 组合），构建和运行各会怎样？

**答案**：构建（链接）失败。`flash_kda.cpp` 中 `DISPATCH_STATE(true)` 的第一个分支 `LAUNCH(false, false, false, true)` 引用了 `launch_fwd<128, false, false, false, true>` 的符号，而表中已无供给。错误在链接期暴露、不会进入运行期——这正是显式实例化作为「白名单契约」的价值。

**练习 2**：`INSTANTIATE_LAUNCH_FWD` 宏为什么要写出全部 19 个参数类型，而不能写 `launch_fwd<D, HI, HO, FP32, VL>` 加省略号？

**答案**：显式实例化的语法要求给出**完整的函数签名**（`template 返回类型 函数名<实参>(参数类型列表);`），编译器据此无歧义地指认要实例化哪个函数。C++ 没有函数签名的「省略号」写法；且参数类型列表不能依赖默认实参推导（模板实参必须显式，函数参数类型必须完整）。

**练习 3**：实例化表里有 `D = 128`，那么 `D` 作为模板参数存在的意义是什么？既然只有一个合法值？

**答案**：目前它更像「为扩展预留的维度参数」：kernel 内所有布局（`K1Layouts<D, CHUNK>`、`K2Layouts<D, CHUNK>`，见 [csrc/smxx/fwd_launch.cu:33-35](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L33-L35)）都以 `D` 为编译期输入，若未来支持 D=64/256，只需在实例化表加行、并在校验链放开 `D == 128` 的检查（u3-l12 会分析这项扩展的真实工作量）。在当前版本里，`D` 的约束由 [csrc/flash_kda.cpp:110](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L110) 的 `TORCH_CHECK` 在运行时把关。

---

## 5. 综合实践

写一个 `dispatch_matrix.py`，把本讲三个模块串起来：**用 Python 枚举全部 14 种合法调用形态，逐个实际调用 `flash_kda.fwd`，并与 `DISPATCH_STATE` 的分支推导互相对拍**。

**实践目标**：

1. 证明 7 × 2 = 14 种组合全部可用（每个组合都能成功命中一份实例化）。
2. 在 Python 侧复刻 [csrc/flash_kda.cpp:57-79](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L57-L79) 与 [L145-160](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L145-L160) 的推导逻辑，打印每行调用「应该命中」的模板实参 `(HI, HO, FP32, VL)`，与 C++ 分支表（4.2.2 的表）逐行核对。

**参考实现骨架**（示例代码，输入构造方式参照 [tests/test_fwd.py:230-240](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test_fwd.py#L230-L240)）：

```python
# dispatch_matrix.py（示例代码）
import math
import torch
import torch.nn.functional as F
import flash_kda

H, D = 4, 128
LOWER_BOUND = -5.0
torch.manual_seed(0)

def make_inputs(B, T, N):
    q = F.normalize(torch.randn((B, T, H, D), dtype=torch.float32, device='cuda'),
                    p=2, dim=-1).to(torch.bfloat16)
    k = F.normalize(torch.randn((B, T, H, D), dtype=torch.float32, device='cuda'),
                    p=2, dim=-1).to(torch.bfloat16)
    v = torch.randn((B, T, H, D), dtype=torch.bfloat16, device='cuda')
    g = torch.randn((B, T, H, D), dtype=torch.bfloat16, device='cuda')
    beta = torch.randn((B, T, H), dtype=torch.bfloat16, device='cuda')
    A_log = torch.rand(H, dtype=torch.float32, device='cuda')
    dt_bias = torch.rand(H, D, dtype=torch.float32, device='cuda')
    return q, k, v, g, beta, A_log, dt_bias

def make_state(N, dtype):
    return torch.arange(N * H * D * D, dtype=torch.float32,
                        device='cuda').reshape(N, H, D, D).to(dtype)

# (name, has_in, has_out, state_fp32)：7 种合法 state 组合
COMBOS = [
    ("in=F,out=F,fp32=F", False, False, False),
    ("in=T,out=T,fp32=F", True,  True,  False),
    ("in=T,out=T,fp32=T", True,  True,  True),
    ("in=F,out=T,fp32=T", False, True,  True),
    ("in=F,out=T,fp32=F", False, True,  False),
    ("in=T,out=F,fp32=T", True,  False, True),
    ("in=T,out=F,fp32=F", True,  False, False),
]

MODES = {
    "varlen":  dict(B=1, seq_lens=[48, 80]),   # N=2，T_total=128
    "batched": dict(B=2, seq_lens=[64, 64]),   # N=2，T_total=128
}

def derive(hi, ho, fp32, is_varlen):
    """复刻 flash_kda.cpp L57-79 / L145-160 的推导，返回模板实参。"""
    return f"launch_fwd<128, {hi}, {ho}, {fp32}, {is_varlen}>"

rows = []
for mode, cfg in MODES.items():
    B, seq_lens = cfg["B"], cfg["seq_lens"]
    T_total = sum(seq_lens)
    q, k, v, g, beta, A_log, dt_bias = make_inputs(B, T_total, len(seq_lens))
    is_varlen = (mode == "varlen")
    cu_seqlens = None
    if is_varlen:
        cu_seqlens = torch.tensor([0] + list(torch.cumsum(
            torch.tensor(seq_lens), dim=0).tolist()), dtype=torch.long, device='cuda')
    N = len(seq_lens)
    for name, hi, ho, fp32 in COMBOS:
        dtype = torch.float32 if fp32 else torch.bfloat16
        initial_state = make_state(N, dtype) if hi else None
        final_state = torch.zeros(N, H, D, D, dtype=dtype, device='cuda') if ho else None
        out = torch.zeros(B, T_total, H, D, dtype=torch.bfloat16, device='cuda')
        try:
            flash_kda.fwd(q, k, v, g, beta, 1.0 / math.sqrt(D), out,
                          A_log=A_log, dt_bias=dt_bias, lower_bound=LOWER_BOUND,
                          initial_state=initial_state, final_state=final_state,
                          cu_seqlens=cu_seqlens)
            torch.cuda.synchronize()
            rows.append((mode, name, derive(hi, ho, fp32, is_varlen), "OK"))
        except Exception as e:
            rows.append((mode, name, derive(hi, ho, fp32, is_varlen), f"FAIL: {e}"))

print(f"{'mode':<8} {'state combo':<20} {'template instantiation':<44} {'result'}")
print("-" * 88)
for r in rows:
    print(f"{r[0]:<8} {r[1]:<20} {r[2]:<44} {r[3]}")
```

**操作步骤**：

1. 在已安装 FlashKDA 的机器上保存并运行 `python dispatch_matrix.py`。
2. 核对输出的 14 行：`result` 列应全为 `OK`。
3. 把 `template instantiation` 列与 4.2.2 的组合表、[csrc/smxx/fwd_launch.cu:228-235](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L228-L235) 的实例化表逐行对照——三者应完全一致。
4. 可选：故意构造第 8 种「非法组合」（例如 stateless 但希望 fp32），确认 Python 侧根本没有途径表达它（没有状态张量就没有 dtype 可言），从调用接口层面印证 7 而非 8 的原因。

**需要观察的现象**：14 行全 OK；varlen 列的 `cu_seqlens` 版本与 batched 列互不干扰；`initial_state` 与 `final_state` 的 dtype 一致性在脚本里天然满足（同一 `dtype` 变量生成两者），因此不会触发 u2-l2 讲过的 dtype 一致性报错。

**预期结果**：得到一张 7 行 state 组合 × 2 列模式的分发矩阵，每格都成功执行并打印出对应的模板实例名。

**待本地验证**：本环境无 GPU，以上输出为基于源码推导的预期，请读者在 SM90 机器上运行确认。

---

## 6. 本讲小结

- `launch_fwd` 的 4 个布尔模板参数（`HasStateIn`/`HasStateOut`/`StateFP32`/`IsVarlen`）全部由调用时的**参数元信息**（optional 是否有值、状态 dtype、cu_seqlens 是否存在）决定，与张量数值无关。
- state 组合是 4 × 2 − 1 = 7 种：「无 in 无 out 但 fp32」不可能存在，因为 `state_fp32` 只能由实际传入的状态张量置位；再乘 varlen/batched，共 7 × 2 = 14 份实例。
- `DISPATCH_STATE` 的 7 个 `else if` 条件互斥完备，与 `fwd_launch.cu` 末尾实例化表的 7 行一一对应——一边是运行期入口，一边是链接期契约，改动必须两侧同步。
- `IsVarlen` 不仅切换 K1/K2 的寻址算法（二分查找 vs 整除映射），还在 host 侧决定是否启动第三个辅助 kernel `_flash_kda_build_tile_prefix`。
- 必须用编译期模板而非运行时分支的根本原因：TMA 描述符的 C++ 类型随配置改变、`if constexpr` 可整段裁掉未选路径（含 `__syncthreads` 与 smem 布局差异）、哑指针技巧的安全性依赖「使用点已被裁掉」的编译期保证。
- 显式实例化把 CUTLASS 重依赖隔离在 `fwd_launch.cu` 单个编译单元内，`flash_kda.cpp` 只需 27 行的 `fwd.h` 声明即可完成调用与链接。

## 7. 下一步学习建议

本讲止步于 `launch_fwd` 的签名与分发。函数体内部「构造 TMA 描述符、切分 workspace、启动两个 kernel」的细节由下一讲接管：

- **u2-l4（CuTe 布局）**：`make_state_tma` 里出现的 `TMAStateSmemLayout`/`TMAFP32StateSmemLayout` 等布局类型从何而来，`make_tma_copy` 如何用它们生成描述符。
- **u2-l5（TMA 描述符与启动配置）**：`fwd_launch.cu` 主体的逐行精读，包括 workspace 六段切分与两个 grid 的设计。
- **u2-l6（Kernel 1 骨架）**：`IsVarlen=true` 分支里 `tile_prefix` 的构建与二分查找的完整实现。

若想先巩固本讲的 C++ 知识，建议阅读 cppreference 的 [explicit instantiation](https://en.cppreference.com/w/cpp/language/function_template#Explicit_instantiation) 与 [if constexpr](https://en.cppreference.com/w/cpp/language/if#Consteval_if) 章节，再回头重读 `fwd_launch.cu` 的 219-238 行。
