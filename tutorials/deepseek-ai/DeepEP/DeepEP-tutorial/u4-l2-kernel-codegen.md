# 内核代码生成：模板实例化的 .cu 注入技巧

## 1. 本讲目标

本讲是 JIT 单元的第二篇。在 [u4-l1](u4-l1-jit-overview.md) 里我们已经建立了 JIT 子系统的「森林」：知道 `Compiler::build` 负责把一段 `.cu` 源码编译成 cubin 再加载。但有一个关键问题被刻意留到了本讲：**这段 `.cu` 源码本身是从哪里来的？它又是怎么做到「只在运行时才知道 SM 数、rank 数、hidden 大小」，却仍然能把这些值变成 nvcc 的编译期常量的？**

学完本讲，你应当能够：

1. 看懂 `LaunchRuntime<Derived>` 这个 CRTP 基类如何把「生成代码」和「启动内核」标准化成两段式接口。
2. 读懂 `DispatchRuntime::generate_impl` 如何把一堆运行时整型参数，用 `fmt::format` 填进一段 C++ 模板的 `<>` 里，拼出一份只有十几行的 `.cu` 文件。
3. 理解 `reinterpret_cast<void*>(&func<...>)` 这个看似怪异的写法，是如何「逼」nvcc 把一个从未被调用的模板内核真正实例化、编译出来的。
4. 说清楚为什么 DeepEP 要费这么大劲把参数编译期常量化（寄存器分配、共享内存布局、循环展开）。

## 2. 前置知识

- **C++ 模板（template）**：`template <int N> void f()` 里 `N` 是编译期常量。只有当 `f<8>` 被「使用」时，编译器才会为 `N=8` 专门生成一份机器码；未被使用的特化不会被编译。
- **CRTP（Curiously Recurring Template Pattern）**：一个类把自己的派生类作为模板参数传给基类，即 `class Derived : public Base<Derived>`。基类就能在编译期调用派生类提供的静态方法，既实现了「框架复用」又没有虚函数的运行时开销。
- **`__global__` 内核与启动配置**：CUDA 内核用 `__global__` 标记，启动时需要 grid/block/共享内存等配置（见 u4-l4）；本讲只关心「内核的源码怎么生成」，不关心具体怎么启动。
- **`__launch_bounds__`**：告诉 nvcc 一个 block 最多多少线程、每个 SM 最多驻留多少个 block，从而让 nvcc 精确控制寄存器分配（少占寄存器 → 多占 SM 并发）。
- 本讲默认你已经读过 [u4-l1](u4-l1-jit-overview.md)，知道 `Compiler`、`KernelRuntime`、`include_parser` 各自的角色，以及「build 一段代码 → 取哈希 → 命中缓存或编译 → 加载 cubin」的主流程。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `csrc/jit/launch_runtime.hpp` | 定义 CRTP 基类 `LaunchRuntime<Derived>` 与 `LaunchArgs`，标准化 `generate` / `launch` 两段式接口。 |
| `csrc/kernels/elastic/dispatch.hpp` | dispatch 的启动器。`DispatchRuntime` 继承 `LaunchRuntime`，提供 `generate_impl`（生成 `.cu`）和 `launch_impl`（传参启动）；`launch_dispatch` 把运行时参数打包后调用 generate→build→launch。 |
| `deep_ep/include/deep_ep/impls/dispatch.cuh` | 真正的 GPU 内核 `dispatch_impl<...>` 模板，header-only，运行时才被 JIT 实例化。 |
| `csrc/jit/compiler.hpp` | `Compiler::build`：用 `name+signature+flags+code` 算哈希做缓存键，调用 nvcc 编译。 |
| `csrc/jit/kernel_runtime.hpp` | 用 `cuobjdump -symbols` 从 cubin 里提取「唯一的内核符号」并加载——这里会过滤掉本讲的壳函数 `__instantiate_kernel`。 |
| `csrc/jit/include_parser.hpp` | `get_hash_value`：把 `.cu` 里的 `#include <deep_ep/...>` 递归算哈希，纳入缓存签名。 |

调用方向（承接 u4-l1 的五层模型，本讲聚焦中间的「generate」一步）：

```
launch_dispatch(...)                 // csrc/kernels/elastic/dispatch.hpp
  ├─ 打包 DispatchRuntime::Args       // 运行时参数（SM数、rank数、hidden…）
  ├─ DispatchRuntime::generate(args)  // ← 本讲主角：拼出 .cu 源码字符串
  ├─ jit::compiler->build("dispatch", code)   // 哈希→缓存→nvcc→cubin
  └─ DispatchRuntime::launch(runtime, args)   // 构造启动配置 + launch_impl 传参
```

## 4. 核心概念与源码讲解

### 4.1 LaunchRuntime\<Derived\> 的 CRTP 框架与两段式设计

#### 4.1.1 概念说明

DeepEP 有十几个不同的内核（dispatch、hybrid_dispatch、combine、combine_reduce_epilogue、dispatch_copy_epilogue、barrier、engram、pp_send_recv、agrs…）。如果每个内核都自己写一遍「生成代码 → 编译 → 构造启动配置 → 传参启动」的样板，代码会大量重复。

`LaunchRuntime<Derived>` 就是为消除这种重复而设计的**框架基类**。它把流程拆成两段：

- **generate（生成段）**：把运行时参数变成一段 `.cu` 源码字符串。这一段是「每个内核都不一样」的，所以交给派生类 `Derived::generate_impl` 实现。
- **launch（启动段）**：拿到已编译的 `KernelRuntime`，根据 `LaunchArgs` 构造启动配置，再把具体参数传给内核。其中「构造启动配置」是通用的（基类做），「具体传哪些参数」又是每个内核不同的，于是基类算好配置后回调 `Derived::launch_impl` 来传参。

CRTP 的好处：基类 `LaunchRuntime<Derived>` 在编译期就知道派生类的真实类型，可以直接 `Derived::generate_impl(args)`、`Derived::launch_impl(...)`，没有任何虚函数开销——这对一个会被高频调用的启动器很重要。

#### 4.1.2 核心流程

```
用户调用 launch_dispatch(...)
   │  把运行时参数填进 DispatchRuntime::Args
   ▼
DispatchRuntime::generate(args)         [基类方法]
   │  调用 Derived::generate_impl(args) → 得到 .cu 字符串
   │  算 include 哈希并拼到代码开头（供缓存失效用）
   ▼
jit::compiler->build("dispatch", code)  [u4-l1/u4-l3]
   │  算总哈希 → 查缓存 / 编译 / 加载 cubin → KernelRuntime
   ▼
DispatchRuntime::launch(runtime, args)  [基类方法]
   │  从 args.launch_args 取 grid/block/smem/cluster
   │  构造 LaunchConfigHandle
   └─ Derived::launch_impl(kernel, config, args)   [派生类：传参 + cudaLaunchKernelExC]
```

#### 4.1.3 源码精读

CRTP 基类定义在 [csrc/jit/launch_runtime.hpp:29-46](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/launch_runtime.hpp#L29-L46)。`generate` 是个模板静态方法，核心就一行——把活儿转给派生类：

```cpp
template <typename Args>
static std::string generate(const Args& args) {
    auto code = Derived::generate_impl(args);   // ← 派生类负责真正生成代码
    ...
}
```

启动段在 [csrc/jit/launch_runtime.hpp:48-71](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/launch_runtime.hpp#L48-L71)，基类先把通用的启动配置算好，再把「传参」这一步回调给派生类：

```cpp
auto config = construct_launch_config(kernel, stream, launch_args.smem_size,
                                      grid_dim, block_dim, launch_args.cluster_dim,
                                      launch_args.cooperative, launch_args.pdl_enabled);
...
Derived::launch_impl(kernel, config, args);   // ← 派生类负责把参数喂给内核
```

派生类 `DispatchRuntime` 的声明在 [csrc/kernels/elastic/dispatch.hpp:14](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/elastic/dispatch.hpp#L14)，正是 CRTP 的标准写法：

```cpp
class DispatchRuntime final : public jit::LaunchRuntime<DispatchRuntime> { ... };
```

> 小贴士：注意 `Args` 在基类里是「模板参数」（表示参数结构体的类型），而在派生类 `DispatchRuntime` 里 `Args` 是一个**嵌套结构体**（`struct Args { ... }`）。基类的 `generate<Args>` 在被 `DispatchRuntime::generate(args)` 调用时，`Args` 会被推导为 `DispatchRuntime::Args`。这两个 `Args` 不是一回事，容易看混。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：确认 CRTP 「基类调派生类」的接线确实成立。
2. **操作步骤**：在 `csrc/kernels/elastic/dispatch.hpp` 里找到 `DispatchRuntime`（第 14 行）和它的两个静态方法 `generate_impl`（第 51 行）、`launch_impl`（第 91 行）。再打开 `csrc/jit/launch_runtime.hpp`，确认基类的 `generate` 调用了 `Derived::generate_impl`、基类的 `launch` 调用了 `Derived::launch_impl`。
3. **观察**：派生类里**没有** `generate` / `launch` 这两个名字，它们完全继承自基类；派生类只提供 `*_impl` 两个钩子。
4. **预期结果**：你能画出「基类 generate/launch → 派生类 generate_impl/launch_impl」的回调关系，并理解为何这套机制不需要 `virtual`。

#### 4.1.5 小练习与答案

- **练习 1**：如果把 `DispatchRuntime` 改成不用 CRTP、而是给基类加 `virtual std::string generate_impl(...)`，会有什么坏处？
  - **答案**：引入虚函数分派开销（虽然每次 dispatch 只调一次，影响不大），更关键的是 `generate_impl` 需要访问派生类特有的 `Args` 结构，虚函数无法表达「每个派生类返回不同的参数类型」，CRTP 正是为了在编译期把参数类型也参数化。
- **练习 2**：基类 `generate` 里有一句注释「we require that `generate_impl`'s includes never change」，结合 `include_parser` 想想为什么？
  - **答案**：include 哈希只算一次并缓存（`static std::string include_hash`）。如果同一内核的 `generate_impl` 有时 include `dispatch.cuh`、有时 include 别的头文件，哈希就会算错，导致缓存键不稳定。所以约定：同一内核生成的代码，`#include` 列表必须固定，能变的只有模板参数。

---

### 4.2 generate_impl：把运行时参数填充成 .cu 代码（直接模式 vs hybrid）

#### 4.2.1 概念说明

`generate_impl` 是本讲真正的「代码生成器」。它的输入是 `DispatchRuntime::Args`（一堆运行时才能确定的整数：SM 数、rank 数、hidden 字节数、专家数、top-k 数…），输出是一段**纯字符串**——一份合法的、可被 nvcc 编译的 `.cu` 源码。

这份源码长得非常简单，本质上只有三句话：include 一个头文件、声明命名空间、写一个取函数地址的小函数。复杂的部分全藏在「模板参数列表」里：`generate_impl` 用 `fmt::format` 把那些运行时整数，逐个填进 `dispatch_impl<...>` 的尖括号，构造出一个**精确到具体配置的模板特化名**。

dispatch 有两种工作模式（承接 u3-l1 的拓扑域）：单节点直接模式（`num_scaleout_ranks == 1`，只用 NVLink）和多节点 hybrid 模式（`num_scaleout_ranks > 1`，scaleout+scaleup 两级）。它们对应的内核模板不同，所以 `generate_impl` 第一步就是按模式分流。

#### 4.2.2 核心流程

```
generate_impl(args):
  if args.num_scaleout_ranks == 1:          # 直接模式
      header_name = "dispatch"
      func_name  = "dispatch_impl<" + 15 个参数 + ">"
  else:                                      # hybrid 模式
      header_name = "hybrid_dispatch"
      func_name  = "hybrid_dispatch_impl<" + 16 个参数 + ">"

  return 字符串:
      #include <deep_ep/impls/{header_name}.cuh>
      using namespace deep_ep::elastic;
      static void __instantiate_kernel() {
          auto ptr = reinterpret_cast<void*>(&{func_name});
      }
```

也就是说，运行时参数（如 `num_sms=132`、`num_experts=256`）经过 `fmt::format`，变成了源码里的字面量 `dispatch_impl<..., 132, ..., 256, ...>`——对 nvcc 而言，这些就是**编译期常量**。

#### 4.2.3 源码精读

模式分流在 [csrc/kernels/elastic/dispatch.hpp:52-78](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/elastic/dispatch.hpp#L52-L78)：

```cpp
if (args.num_scaleout_ranks == 1) {
    header_name = "dispatch";
    func_name = fmt::format("dispatch_impl<{}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}>",
        args.is_scaleup_nvlink, args.do_cpu_sync, args.reuse_slot_indices,
        args.launch_args.grid_dim.first,        // ← 即 num_sms
        args.num_notify_warps, args.num_dispatch_warps,
        args.num_scaleup_ranks,                 // ← 直接模式下 num_ranks == num_scaleup_ranks
        args.num_hidden_bytes, args.num_sf_packs,
        args.num_max_tokens_per_rank,
        args.num_experts, args.num_topk, args.expert_alignment,
        args.num_qps, args.num_timeout_cycles);
} else {
    header_name = "hybrid_dispatch";
    func_name = fmt::format("hybrid_dispatch_impl<{}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}>", ...);
}
```

随后用一段原始字符串把 `header_name` 和 `func_name` 拼成最终 `.cu`，见 [csrc/kernels/elastic/dispatch.hpp:80-88](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/elastic/dispatch.hpp#L80-L88)：

```cpp
return fmt::format(R"(
#include <deep_ep/impls/{}.cuh>

using namespace deep_ep::elastic;

static void __instantiate_kernel() {{
    auto ptr = reinterpret_cast<void*>(&{});
}}
)", header_name, func_name);
```

把模板参数列表和真正的内核签名对照一下，就能看清「第几个运行时参数 → 哪个编译期常量」。直接模式的 15 个参数，正好对应 [deep_ep/include/deep_ep/impls/dispatch.cuh:17-30](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/dispatch.cuh#L17-L30) 的模板形参：

| `generate_impl` 填入的实参 | 模板形参（dispatch.cuh） | 说明 |
| --- | --- | --- |
| `is_scaleup_nvlink` | `bool kIsScaleupNVLink` | 节点内是否 NVLink 直连 |
| `do_cpu_sync` | `bool kDoCPUSync` | 是否 CPU 同步计数 |
| `reuse_slot_indices` | `bool kReuseSlotIndices` | cached 模式复用槽位 |
| `launch_args.grid_dim.first` | `int kNumSMs` | **SM 数 = grid.x** |
| `num_notify_warps` | `int kNumNotifyWarps` | 通知 warp 数 |
| `num_dispatch_warps` | `int kNumDispatchWarps` | 派发 warp 数 |
| `num_scaleup_ranks` | `int kNumRanks` | 直接模式下即总 rank 数 |
| `num_hidden_bytes` | `int kNumHiddenBytes` | 单 token hidden 字节数 |
| `num_sf_packs` | `int kNumSFPacks` | FP8 scale factor 包数 |
| `num_max_tokens_per_rank` | `int kNumMaxTokensPerRank` | 每 rank 最大 token 数 |
| `num_experts` | `int kNumExperts` | 全局专家数 |
| `num_topk` | `int kNumTopk` | top-k 路由数 |
| `expert_alignment` | `int kExpertAlignment` | 专家对齐粒度 |
| `num_qps` | `int kNumQPs` | RDMA 队列对数 |
| `num_timeout_cycles` | `int64_t kNumTimeoutCycles` | GPU 超时周期 |

> 注意一个细节：直接模式把 `num_scaleup_ranks` 当作 `kNumRanks` 传进去。这是因为在直接模式下 `num_scaleout_ranks == 1`，所以 `num_ranks = num_scaleout_ranks × num_scaleup_ranks = num_scaleup_ranks`（见 `launch_dispatch` 里 [csrc/kernels/elastic/dispatch.hpp:171](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/elastic/dispatch.hpp#L171)）。hybrid 模式则分别传 `num_scaleout_ranks` 和 `num_scaleup_ranks`，多了一个参数（16 个）。

#### 4.2.4 代码实践（本讲主实践）

1. **实践目标**：在 `generate_impl` 里定位直接模式与 hybrid 模式的分支，回答三问——`num_scaleout_ranks==1` 时实例化的是哪个头文件里的哪个模板？被编译期化的参数有哪些？为什么直接模式比 hybrid 少一个参数？
2. **操作步骤**：
   - 打开 [csrc/kernels/elastic/dispatch.hpp:51-89](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/elastic/dispatch.hpp#L51-L89)。
   - 找到 `if (args.num_scaleout_ranks == 1)` 分支：它把 `header_name` 设为 `"dispatch"`，于是源码里 `#include <deep_ep/impls/dispatch.cuh>`，实例化的是该头文件里的 [`dispatch_impl<...>`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/dispatch.cuh#L17-L32)。
   - 对照 [dispatch.cuh:17-30](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/dispatch.cuh#L17-L30) 的模板形参表，把上面那张表里 15 个参数逐个标注为「编译期常量」。
   - 把 `export EP_JIT_DEBUG=1` 后跑一次 `tests/elastic/test_ep.py`，终端会打印 `Generated kernel code:`，你能直接看到 `dispatch_impl<true, false, false, 132, ...>` 这样填好的真实字符串。
3. **需要观察的现象**：打印出的 `func_name` 里所有「尖括号内的数字」都是具体的字面量（如 `132`、`8`、`7168`），而不是变量名。
4. **预期结果**：你能口述「`num_scaleout_ranks==1` → `dispatch.cuh::dispatch_impl`，15 个模板参数全部编译期化；hybrid → `hybrid_dispatch.cuh::hybrid_dispatch_impl`，16 个参数（多出 `num_scaleout_ranks`，且没有 `is_scaleup_nvlink`）」。
5. **待本地验证**：具体的 `num_sms`、`num_dispatch_warps` 数值依赖你的 GPU 与配置，需在真实 8 卡环境跑一次才能看到。

#### 4.2.5 小练习与答案

- **练习 1**：为什么直接模式的 `dispatch_impl` 没有 `num_scaleout_ranks` 这个模板参数，而 hybrid 的 `hybrid_dispatch_impl` 有？
  - **答案**：直接模式 `num_scaleout_ranks==1` 是恒定事实，写进内核没意义，内核只需要知道总 rank 数 `kNumRanks`。hybrid 模式需要分别知道 scaleout（跨节点 RDMA）和 scaleup（节点内 NVLink）两级的 rank 数，才能做两级转发，所以两个维度都要编译期化。
- **练习 2**：如果我改了 `num_experts`（比如从 256 改成 512），会触发重新编译吗？
  - **答案**：会。`num_experts` 是模板实参，改了 → `func_name` 字符串变 → `code` 变 → `Compiler::build` 算出的哈希变（见 [compiler.hpp:112-113](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/compiler.hpp#L112-L113)）→ 缓存未命中 → 重新调 nvcc。这就是「不同配置 → 不同 cubin」的内容寻址缓存。

---

### 4.3 reinterpret_cast\<void\*>(&func\<...\>)：强制模板实例化的核心技巧

#### 4.3.1 概念说明

这是本讲最精妙的一笔，也是标题里「`.cu` 注入技巧」的灵魂。

回想 C++ 模板规则：**模板只有被「使用」才会被实例化**。而生成的 `.cu` 里，`dispatch_impl<...>` 从头到尾**没有被调用**——因为它是个 `__global__` 内核，正确调用要写 `<<<grid, block>>>` 并传一堆参数，太麻烦。可是如果什么都不做，nvcc 根本不会为这个特化生成任何代码，cubin 里就没有内核符号，后面 `KernelRuntime` 也就找不到东西可加载。

解决办法是**取它的地址**：

```cpp
auto ptr = reinterpret_cast<void*>(&dispatch_impl<...>);
```

这一行同时干了两件事：

1. **强制实例化**：`&dispatch_impl<...>` 是一次「取地址」操作，构成对模板特化的 ODR-use（显式使用），nvcc 必须为这个特化生成完整的内核代码。
2. **`reinterpret_cast<void*>`**：把内核指针转成通用 `void*` 存到一个局部变量，既不需要知道内核精确的指针类型，又构成一次编译器无法轻易消除的「使用」，保证内核代码不会被当成死代码优化掉。

而这个壳函数 `__instantiate_kernel` 本身是个 `static`、从不被调用的空壳——它存在的唯一目的，就是承载这一行取地址语句。真正被装进 cubin 的内核符号是 `dispatch_impl<...>`，`__instantiate_kernel` 在符号提取阶段会被故意过滤掉（见 4.3.3）。

#### 4.3.2 核心流程

```
生成的 .cu:
   #include <deep_ep/impls/dispatch.cuh>     # 带来模板定义
   static void __instantiate_kernel() {
       auto ptr = reinterpret_cast<void*>(&dispatch_impl<...>);  # 取地址 → 强制实例化
   }

nvcc 编译这份 .cu:
   - 看到 &dispatch_impl<...> → 必须实例化该特化 → 生成内核机器码 → 写入 cubin
   - __instantiate_kernel 本身是壳，但它的取地址行为已经达成目的

KernelRuntime 加载 cubin:
   - cuobjdump -symbols 列出所有符号
   - 过滤掉 __instantiate_kernel / vprintf / __internal / __assertfail
   - 只剩唯一的 dispatch_impl<...> 符号 → 加载它
```

#### 4.3.3 源码精读

壳函数模板在 [csrc/kernels/elastic/dispatch.hpp:85-87](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/elastic/dispatch.hpp#L85-L87)：

```cpp
static void __instantiate_kernel() {{
    auto ptr = reinterpret_cast<void*>(&{});
}}
```

其中 `{}` 会被填成上一步算好的 `func_name`（如 `dispatch_impl<true, false, false, 132, ...>`）。

而「过滤壳函数」的逻辑在 [csrc/jit/kernel_runtime.hpp:29-43](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/kernel_runtime.hpp#L29-L43)。`KernelRuntime` 用 `cuobjdump -symbols` 列出 cubin 里所有符号，然后只保留 `STT_FUNC` 且带 `STO_ENTRY`、且名字**不**含下列任一前缀的符号：

```cpp
const std::vector<std::string> illegal_names = {"vprintf", "__instantiate_kernel", "__internal", "__assertfail"};
```

注意 `__instantiate_kernel` 赫然在列——这正是为了把我们在 `.cu` 里写的壳函数排除掉。随后 [kernel_runtime.hpp:46-58](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/kernel_runtime.hpp#L46-L58) 断言「有且仅有一个」合格符号，并把它作为真正的内核加载：

```cpp
if (symbol_names.size() != 1) { ... "Corrupted JIT cache directory" ... }
kernel = load_kernel(cubin_path, symbol_names[0], &library);
```

这就形成了一个闭环：**壳函数负责「逼」出内核代码，加载器又负责把壳函数「挑掉」，只留下真内核。**

> 为什么用 `reinterpret_cast<void*>` 而不是直接 `auto ptr = &dispatch_impl<...>;`？因为 `__global__` 内核的地址类型是 nvcc 特有的 kernel function pointer（`cudaKernel_t` 之类），直接赋值需要精确类型，且不同 CUDA 版本/前后端（runtime API vs driver API，见 [handle.hpp:11](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/handle.hpp#L11) 的 `#if` 分支）类型不同。转成 `void*` 一了百了，既保住了「取地址 = 强制实例化」的语义，又避开类型依赖。

#### 4.3.4 代码实践（观察型）

1. **实践目标**：亲眼看到「壳函数被过滤、真内核被加载」的过程。
2. **操作步骤**：`export EP_JIT_DEBUG=1`，跑一次 dispatch 测试。清空缓存目录（`rm -rf ~/.deep_ep` 或你的 `EP_JIT_CACHE_DIR`）可以强制重新编译。
3. **观察**：
   - 终端会打印 `Loading CUBIN: .../kernel.dispatch.<hash>/kernel.cubin`。
   - 打开该目录，能看到 `kernel.cu`（生成的源码，里面有 `__instantiate_kernel` 和取地址语句）和 `kernel.cubin`（编译产物）。
   - 手动跑 `cuobjdump -symbols <那个 cubin>`，你会看到 `__instantiate_kernel` 和真正的 `dispatch_impl<...>`（mangled name）都在；加载器只取后者。
4. **预期结果**：验证 cubin 里确实只有「一个」被 `KernelRuntime` 认可的内核符号，其余都是被过滤的辅助符号。
5. **待本地验证**：符号的具体 mangled name 因编译器和参数而异，需本地执行 `cuobjdump` 才能看到。

#### 4.3.5 小练习与答案

- **练习 1**：如果删掉 `reinterpret_cast<void*>(&...)` 这一行，整个 `.cu` 里就不再出现 `dispatch_impl`，会发生什么？
  - **答案**：nvcc 不会实例化 `dispatch_impl<...>`，cubin 里没有任何内核符号。`KernelRuntime` 找到的合格符号数为 0，触发 `symbol_names.size() != 1` 断言，报 "Corrupted JIT cache directory"。
- **练习 2**：为什么壳函数起名叫 `__instantiate_kernel` 而不是随便一个名字？
  - **答案**：因为它要被 `kernel_runtime.hpp` 的 `illegal_names` 过滤掉。名字必须和过滤列表里的字符串完全一致（子串匹配）。如果换个名字，它就会被当成「真内核」候选，导致符号数 ≠ 1 而报错。这是一个跨文件的隐式约定。

---

### 4.4 编译期常量化的收益：寄存器、共享内存与循环展开

#### 4.4.1 概念说明

到目前为止你可能会问：费这么大劲把 `num_sms`、`num_experts`、`num_hidden_bytes` 等参数变成模板常量，到底换来了什么？为什么不在内核里写成普通函数参数（运行时传进去）？

答案是：**编译期常量能让 nvcc 做出在运行时参数下根本无法做的优化**。GPU 内核的性能极度依赖三件事：

1. **寄存器分配**：每个线程能用多少寄存器、每个 SM 能驻留多少个 block，直接决定占用率（occupancy）。这需要知道确切的线程数和共享内存用量。
2. **共享内存布局**：`extern __shared__` 怎么切分给 notify/dispatch 各段，需要确切的字节数。
3. **循环展开**：遍历 `num_ranks`、`num_experts` 等维度的循环，如果 trip count 是编译期常量，nvcc 可以完全展开，省掉分支与循环开销。

当这些量是模板的 `int kNumRanks`、`int kNumSMs` 时，它们是 `constexpr`，nvcc 在编译每一个特化时都能看到具体数值，从而把上面三件事做到极致。这就是 DeepEP 宁可「每次换配置都重新 JIT 一次」也要换来的性能——结合两级缓存（u4-l3），实际编译开销被摊到几乎可忽略。

#### 4.4.2 核心流程

```
运行时整数 (num_sms=132, num_experts=256, ...)
        │  generate_impl + fmt::format
        ▼
模板特化 dispatch_impl<..., 132, ..., 256, ...>
        │  nvcc 编译这个特化
        ▼
看到 constexpr kNumThreads / kNumRanks / kNumExperts ...
   ├─ __launch_bounds__(kNumThreads, 1) → 精确寄存器分配
   ├─ EP_STATIC_ASSERT(...)            → 编译期合法性检查
   ├─ constexpr 共享内存切分             → 静态布局
   └─ 循环 trip count 已知             → 完全展开
        ▼
高度优化、针对该配置量身定制的 cubin
```

#### 4.4.3 源码精读

**收益一：精确的 launch bounds。** 在 [dispatch.cuh:31](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/dispatch.cuh#L31)，内核带 `__launch_bounds__(kNumThreads, 1)`：

```cpp
template <..., int kNumNotifyWarps, int kNumDispatchWarps, ...,
          int kNumNotifyThreads = kNumNotifyWarps * 32,
          int kNumDispatchThreads = kNumDispatchWarps * 32,
          int kNumThreads = kNumNotifyThreads + kNumDispatchThreads, ...>
__global__ void __launch_bounds__(kNumThreads, 1)
dispatch_impl(...) { ... }
```

`kNumThreads` 完全由模板参数推导（编译期），所以 nvcc 知道每个 block 恰好多少线程、每个 SM 恰好驻留 1 个 block，从而把寄存器预算压到极致。配合 `Compiler` 给 nvcc 加的 `--ptxas-options=--register-usage-level=10`（[compiler.hpp:58-59](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/compiler.hpp#L58-L59)）——这是 ptxas 最激进的限寄存器档位——把寄存器占用压到最低，正好契合 DeepEP「尽量少占 SM、把算力让给用户计算」的设计目标（见 u3-l3 的 SM 建模）。

**收益二：编译期合法性检查。** [dispatch.cuh:46-48](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/dispatch.cuh#L46-L48)：

```cpp
constexpr int kNumExpertsPerRank = kNumExperts / kNumRanks;
EP_STATIC_ASSERT(kNumExperts % kNumRanks == 0, "Invalid number of experts or ranks");
EP_STATIC_ASSERT(kNumNotifyWarps % 4 == 0, "Invalid warpgroup size");
```

因为 `kNumExperts`、`kNumRanks` 是编译期常量，这些断言在 JIT 编译时就能求值。配置不合理（比如 256 专家 / 6 rank）会在 nvcc 编译阶段直接失败，而不是跑到 GPU 上才出错。

**收益三：编译期共享内存布局。** [dispatch.cuh:60-62](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/dispatch.cuh#L60-L62)：

```cpp
constexpr int kNumSmemBytesForNotify = kNumNotifyThreads > 0 ?
    math::constexpr_align(kNumRanks + kNumExperts, kNumNotifyThreads) * sizeof(int) : 0;
```

整段共享内存的大小、对齐都在编译期算定，运行时零开销。

> 补充：`num_sms` 不仅作为模板参数 `kNumSMs` 被编译期化，它同时也是 launch 时的 `grid_dim.x`（见 [dispatch.hpp:226](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/elastic/dispatch.hpp#L226) 的 `LaunchArgs(num_sms, ...)`）。也就是说「内核内部当常量用、内核外部当 grid 用」——同一个值，内外两种用途，这是 SM 数建模（u3-l3）与启动配置（u4-l4）的交汇点。

#### 4.4.4 代码实践（对比型）

1. **实践目标**：直观感受编译期常量对生成的机器码的影响。
2. **操作步骤**：
   - 用 `EP_JIT_DUMP_SASS=1`（或 `EP_JIT_DUMP_PTX=1`）跑一次 dispatch，在缓存目录里会多出 `kernel.sass`（或 `kernel.ptx`）。
   - 在 SASS/PTX 里搜索专家数、rank 数对应的循环：因为 `kNumExperts`、`kNumRanks` 是编译期常量，对应的循环通常已被展开成顺序指令，看不到循环回跳。
   - 对比：如果把同样的逻辑写成运行时参数（`int num_experts`），生成的代码里会出现基于寄存器的分支与循环。
3. **需要观察的现象**：编译期常量版本里，许多「循环」消失，变成一长串顺序的 load/store；`__launch_bounds__` 让寄存器数明显受限。
4. **预期结果**：能指出至少一处「因为常量化而被展开」的代码结构。
5. **待本地验证**：SASS 细节因配置而异，需本地 dump 后人工比对。

#### 4.4.5 小练习与答案

- **练习 1**：`--register-usage-level=10` 和 `__launch_bounds__(kNumThreads, 1)` 是什么关系？
  - **答案**：`__launch_bounds__` 告诉 nvcc「每个 SM 最多驻留 1 个 block、每 block 恰好 kNumThreads 线程」，nvcc 据此算出「为了让 1 个 block 放得下，每个线程最多能用多少寄存器」；`--register-usage-level=10` 则是 ptxas 层面进一步把这个上限压到最低档。两者配合，把寄存器占用压到极致，腾出 SM 给用户计算流（与 `prefer_overlap_with_compute` 呼应）。
- **练习 2**：为什么 `EP_STATIC_ASSERT(kNumExperts % kNumRanks == 0)` 比运行时 `assert` 好？
  - **答案**：它是编译期断言，配置非法时在 JIT 编译阶段（`nvcc`）就失败并打印消息，定位精确；运行时 assert 要等内核启动后才可能在 device 侧触发，调试困难得多。

## 5. 综合实践

把本讲四个模块串起来，完成一次「**追踪一次 dispatch 调用，从运行时参数到生成的 .cu 再到 cubin 符号**」的全链路阅读：

1. **起点**：在 [csrc/kernels/elastic/dispatch.hpp:199-229](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/elastic/dispatch.hpp#L199-L229) 的 `launch_dispatch` 里，找到 `DispatchRuntime::Args args = {...}` 这段聚合初始化，挑出 3 个被填进「模板参数」的字段（如 `num_experts`、`num_topk`、`launch_args.grid_dim.first`），追踪它们各自来自 `launch_dispatch` 的哪个入参。
2. **生成**：跟着 `DispatchRuntime::generate(args)`（[第 227 行](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/elastic/dispatch.hpp#L227)）进入 4.2/4.3 讲的 `generate_impl`，手写出「单节点、8 rank、hidden=7168、num_experts=256」配置下，`func_name` 字符串大概长什么样（不必精确到每个值）。
3. **编译**：跟着 `jit::compiler->build("dispatch", code)`（[第 228 行](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/elastic/dispatch.hpp#L228)）进入 [compiler.hpp:111-160](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/compiler.hpp#L111-L160)，确认 `code` 字符串参与了缓存哈希，并解释为什么改任一模板参数都会 miss 缓存。
4. **加载**：在 [kernel_runtime.hpp:29-58](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/kernel_runtime.hpp#L29-L58) 解释：为什么 `__instantiate_kernel` 这个壳函数不会干扰「唯一符号」的提取。
5. **真机验证（可选）**：`export EP_JIT_DEBUG=1 EP_JIT_DUMP_SASS=1` 跑 `tests/elastic/test_ep.py`，对照打印出的 `Generated kernel code` 与缓存目录里的 `kernel.cu` / `kernel.cubin` / `kernel.sass`，确认你手写的那段 `.cu` 和真实生成的一致。

> 如果没有 8 卡 Hopper 环境，第 1~4 步作为纯源码阅读任务即可完成；第 5 步标注为「待本地验证」。

## 6. 本讲小结

- `LaunchRuntime<Derived>` 用 CRTP 把「生成代码」和「启动内核」标准化为两段式：基类提供 `generate`/`launch`，派生类只实现 `generate_impl` 和 `launch_impl` 两个钩子，全程零虚函数开销。
- `generate_impl` 用 `fmt::format` 把运行时整数（SM 数、rank 数、hidden、专家数…）逐个填进 `dispatch_impl<...>` 的尖括号，拼出一份只有十几行、针对当前配置量身定制的 `.cu` 源码；直接模式（`num_scaleout_ranks==1`）实例化 `dispatch.cuh::dispatch_impl`（15 个模板参数），hybrid 模式实例化 `hybrid_dispatch.cuh::hybrid_dispatch_impl`（16 个参数）。
- `reinterpret_cast<void*>(&func<...>)` 是「逼」nvcc 实例化模板的核心技巧：取地址构成 ODR-use，强制编译器为该特化生成内核代码；壳函数 `__instantiate_kernel` 在符号提取时被 `illegal_names` 过滤掉，只留下真正的内核符号。
- 把参数编译期常量化换来三重收益：`__launch_bounds__(kNumThreads,1)` + `--register-usage-level=10` 精确压低寄存器占用、`EP_STATIC_ASSERT` 编译期校验配置、`constexpr` 共享内存布局与循环完全展开。
- 代价是「换配置即重编译」，但靠内容寻址的两级缓存（进程内 + 文件系统，见 u4-l3）把开销摊薄到几乎可忽略。
- 整条链路是跨文件协作的闭环：`dispatch.hpp` 生成壳 → `compiler.hpp` 编译 → `kernel_runtime.hpp` 过滤壳取真符号 → `handle.hpp` 加载启动。

## 7. 下一步学习建议

- 接着读 [u4-l3](u4-l3-jit-cache-and-load.md)：本讲只到「生成 `.cu`」，下一篇讲清楚 `Compiler::build` 的内容寻址缓存、临时目录原子 rename、fsync，以及 `KernelRuntime` 如何用 cuobjdump 提取唯一符号——和本讲 4.3 的过滤逻辑直接衔接。
- 再读 [u4-l4](u4-l4-launch-framework.md)：本讲的 `launch` 段把 `LaunchArgs`（grid/block/smem/cluster/cooperative/pdl）交给 `construct_launch_config`，下一篇会逐字段解释这些启动配置项的含义与 `DeviceRuntime` 的 GPU 能力探测。
- 想看「另一种内核的代码生成」作对比，可以读 [csrc/kernels/elastic/dispatch.hpp:260-276](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/elastic/dispatch.hpp#L260-L276) 的 `DispatchCopyEpilogueRuntime::generate_impl`，它用完全相同的 reinterpret_cast 技巧实例化 `dispatch_copy_epilogue_impl`，是巩固本讲技巧的好练习。
- 进入 U5 后，你会真正钻进 `deep_ep/include/deep_ep/impls/dispatch.cuh` 的内核内部，看这些编译期常量是如何驱动 notify/dispatch warp 协作的。
