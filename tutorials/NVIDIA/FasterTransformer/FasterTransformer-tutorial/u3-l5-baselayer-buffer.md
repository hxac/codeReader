# BaseLayer 架构：buffer 生命周期管理

## 1. 本讲目标

本讲是「基础算子与层抽象」单元的收尾篇。前面几讲我们分别学了 kernel（u3-l1）、注意力层（u3-l2/u3-l3）、FFN 层（u3-l4），它们都属于「具体的层」。本讲要回答一个贯穿所有层的问题：

> **这些层在 forward 过程中需要的临时显存（workspace），是从哪里来的、谁来分配、谁来回收？**

学完本讲，你应当能够：

- 说清 `BaseLayer` 这个所有层的共同基类提供了什么「设备环境」与什么「接口契约」。
- 说清 `allocateBuffer()` / `freeBuffer()` 这对接口的 **幂等（idempotent）约定**，以及为什么 `freeBuffer` 必须可以安全地被反复调用。
- 把本讲和 u2-l2 的 `IAllocator::reMalloc` 串起来，解释 `REUSE / INCREASE / DECREASE` 三态决策在「同一个 layer 的 forward 被多次调用、batch 变大」时如何命中。
- 区分 FT 中的两套显存抽象：热路径的 `IAllocator`（每步 forward 复用 workspace）与冷路径的 `GPUBuf`（测试里用的 RAII 一次性 buffer）。
- 理解 FT 为什么坚持「buffer 复用」而不是「每步 malloc/free」。

> ⚠️ 一个重要更正：本讲规格里提到 `queryBuffer`，但**当前源码中并不存在 `queryBuffer` 这个方法**。`BaseLayer` 的真实接口只有 `allocateBuffer()` 与 `freeBuffer()` 两个纯虚函数。层的 workspace 是通过 `protected` 成员指针（如 `inter_buf_`）直接访问的，状态则用 `is_allocate_buffer_` 标志位记录。本讲会严格按真实源码讲解，不会编造 `queryBuffer`。

---

## 2. 前置知识

本讲需要你先掌握以下两讲（它们是 `depends_on`）：

- **u2-l2 GPU 显存管理：Allocator 与 memory_utils**。本讲大量依赖其中的 `IAllocator::reMalloc` 与 `ReallocType`（INCREASE / REUSE / DECREASE）三态语义。如果你还不清楚 `reMalloc` 如何用 `pointer_mapping_` 账本判断「地址是否已分配、当前大小是否够用」，请先回看 u2-l2。
- **u2-l5 权重容器与权重加载**。你需要知道「权重（Weight）」是冷路径、常驻显存的对象，用 `deviceMalloc` 一次性分配；而本讲讲的是「workspace」是热路径、每步 forward 用完即还的临时显存。两者必须区分开。

此外，u3-l3、u3-l4 讲过的注意力层、FFN 层是本讲最常用的「具体例子」，我们会反复引用 `FfnLayer`、`UnfusedAttentionLayer` 的源码。

几个通俗概念：

- **workspace（工作区）**：一个层在 forward 中间产生的、不需要跨步保留的临时张量。比如 FFN 里 GEMM1 的输出 `inter_buf_`、注意力里的 `qk_buf_`（QK^T 分数矩阵）。它们是「中间产物」，输出写回主输出张量后就废弃。
- **幂等（idempotent）**：同一个操作调用一次和调用多次效果相同。`freeBuffer()` 设计成幂等：已经释放过的 buffer 再 `freeBuffer()` 也不会出错。
- **RAII（Resource Acquisition Is Initialization）**：在构造函数里获取资源、在析构函数里释放资源，靠对象生命周期自动管理。本讲的 `GPUBuf` 就是典型 RAII。

---

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `src/fastertransformer/layers/BaseLayer.h` | 所有层（layer / model）的共同基类，定义「设备环境」成员与 `allocateBuffer/freeBuffer` 接口契约。 |
| `src/fastertransformer/utils/allocator.h` | `IAllocator` 抽象与 `reMalloc` 模板方法，`ReallocType` 三态枚举。workspace 复用的真正实现者。 |
| `src/fastertransformer/utils/gpu_buf.h` | `GPUBuf<T>`，测试场景使用的 RAII 显存封装（冷路径，不复用）。 |
| `src/fastertransformer/layers/FfnLayer.h` / `.cc` | 具体层的范例：演示 buffer 成员指针声明、`allocateBuffer(size_t...)` / `freeBuffer()` 实现、forward 中的调用时机。 |
| `src/fastertransformer/layers/attention_layers/UnfusedAttentionLayer.cc` | 第二个具体层范例，同样演示 `allocateBuffer(batch, seq)` 与幂等 `freeBuffer`。 |

---

## 4. 核心概念与源码讲解

### 4.1 BaseLayer：所有层的共同基类

#### 4.1.1 概念说明

FT 里有几十种层（各种注意力层、FFN 层、采样层、beam search 层……）和十几种模型。它们在 forward 时都需要三样「设备环境」：一条 CUDA stream、一个 cuBLAS wrapper、一个显存分配器。如果每个层都自己存一份这些环境、各自定义一套分配/释放接口，代码会非常混乱。

`BaseLayer` 就是把这些公共的东西抽到一个基类里：

- 把「设备环境」（stream、cuBLAS wrapper、allocator、设备属性）作为 `protected` 成员存起来，所有子类直接用。
- 定义一对纯虚函数 `allocateBuffer()` / `freeBuffer()`，强制每个子类自己实现「我要分配哪些 workspace、怎么释放」。
- 提供一个 `is_free_buffer_after_forward_` 开关，让上层决定「forward 之后是否立即把 workspace 还回去」。

#### 4.1.2 核心流程

一个 `BaseLayer`（及其子类）对象的生命周期大致是：

```
构造(layer)
   │  注入 stream / cublas_wrapper / allocator 等设备环境
   │  buffer 成员指针初始化为 nullptr
   ▼
forward(input, output)  ← 可能被调用很多次
   │  1. allocateBuffer(运行期尺寸)   // 按 batch/seq 现场申请 workspace
   │  2. 跑各种 GEMM / kernel，把中间结果写进 workspace
   │  3. 把最终结果写进 output_tensors
   │  4. if (is_free_buffer_after_forward_) freeBuffer();
   ▼
析构(~layer)
   │  freeBuffer();   // 兜底：确保 workspace 被释放
   ▼  销毁
```

注意第 1 步和第 4 步都依赖 `is_allocate_buffer_` 这个状态位和 `allocator_->reMalloc` 的复用能力，下面分别讲。

#### 4.1.3 源码精读

先看 `BaseLayer` 的构造函数与成员。它**不是模板类**，就是一个普通基类；所谓的「device 分发」是把设备环境通过构造函数注入，并存为 `protected` 成员，供子类共享：

[BaseLayer.h:29-40](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/BaseLayer.h#L29-L40) — 构造函数接收 stream、cublas_wrapper、allocator、`is_free_buffer_after_forward`、设备属性、sparse 标志，存到 protected 成员。

下面这段是本讲的核心契约——两个纯虚函数。**注意：只有 `allocateBuffer()` 和 `freeBuffer()` 两个，没有 `queryBuffer`**：

[BaseLayer.h:53-65](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/BaseLayer.h#L53-L65) — protected 区声明纯虚 `allocateBuffer()=0`、`freeBuffer()=0`，以及设备环境成员与两个状态位 `is_free_buffer_after_forward_`、`is_allocate_buffer_`。

这里有几个关键点要在源码里看懂：

1. `allocateBuffer()` / `freeBuffer()` 是 **pure virtual**（`= 0`），所以 `BaseLayer` 是抽象类，不能直接实例化；任何具体层都必须实现它们。
2. `is_allocate_buffer_` 默认 `false`，注释写着 `TODO (bhsueh) to be deprecated`（[BaseLayer.h:64](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/BaseLayer.h#L64)）。它目前仍是 `freeBuffer` 幂等性的依据（4.2 节详解）。
3. 「device 分发」在这里的含义是：**同一份设备环境（stream/cublas/allocator）被所有子类共享**，而数据类型分发则发生在更外层——具体层是 `Layer<T>` 模板（如 `FfnLayer<T>`），由外层按 `float / half / __nv_bfloat16` 实例化（这套「枚举→模板」dispatch 在 u1-l4 讲过）。`BaseLayer` 自身不感知 `T`。

#### 4.1.4 代码实践

**实践目标**：确认 `BaseLayer` 的真实接口，纠正「它有 `queryBuffer`」的误解。

**操作步骤**：

1. 打开 [src/fastertransformer/layers/BaseLayer.h](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/BaseLayer.h)，通读整个文件（只有约 50 行有效代码）。
2. 数一数它的 `public` 方法有哪些（构造、析构、`getStream`、`setStream`）。
3. 数一数它的 `protected` 纯虚函数有几个。

**需要观察的现象**：`public` 没有 `queryBuffer`；`protected` 只有 `allocateBuffer()` 和 `freeBuffer()` 两个纯虚函数；构造函数把六个参数原样存为成员。

**预期结果**：你会确认本讲开头的更正——`queryBuffer` 不存在。层的 workspace 是子类自己声明的 `protected` 成员指针（如 FfnLayer 的 `inter_buf_`），外层并不通过某个 `query` 方法去读，而是直接在 forward 内部使用。

**待本地验证**：如果你想彻底确认全仓库没有 `queryBuffer`，可以在仓库根目录执行 `grep -rn "queryBuffer" src/`，预期返回为空。

#### 4.1.5 小练习与答案

**练习 1**：`BaseLayer` 为什么要把 `stream_`、`cublas_wrapper_`、`allocator_` 放在 `protected` 而不是 `private`？

**参考答案**：因为具体子类（如 `FfnLayer`）在自己的 `allocateBuffer` / `forward` 里要直接用它们——`allocator_->reMalloc(...)` 申请 workspace、`cublas_wrapper_->Gemm(...)` 调矩阵乘、kernel 启动要传 `stream_`。放 `private` 子类就拿不到，每层就得自己再存一份，失去了「基类收拢设备环境」的意义。

**练习 2**：`BaseLayer` 是抽象类吗？为什么？

**参考答案**：是。因为它声明了纯虚函数 `virtual void allocateBuffer() = 0;` 和 `virtual void freeBuffer() = 0;`（[BaseLayer.h:54-55](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/BaseLayer.h#L54-L55)）。含有纯虚函数的类不能直接 `new BaseLayer(...)`，必须由子类实现这两个函数后才能实例化。

---

### 4.2 allocateBuffer / freeBuffer：workspace 的生命周期约定

#### 4.2.1 概念说明

`BaseLayer` 只规定了「要有 `allocateBuffer` 和 `freeBuffer`」，但具体申请哪些 buffer、怎么释放，由每个子类自己写。FT 全仓库的子类都遵守同一套约定：

1. **buffer 成员指针**：在子类 `protected`/`private` 区声明一组 `T* xxx_buf_ = nullptr;`，初始为 `nullptr`。这是 workspace 的「句柄」。
2. **带尺寸的重载 `allocateBuffer(size_t ...)`**：因为 workspace 大小依赖运行期的 batch/seq，无参的 `allocateBuffer()`（基类要求的那个）在子类里通常被实现成「直接报错」，强制调用方使用带尺寸的重载。
3. **`freeBuffer` 幂等**：用 `is_allocate_buffer_` 守卫，只有在「当前确实持有 buffer」时才真正释放，并把标志位置回 `false`。

这套约定让 workspace 的「申请—使用—释放」可以在 forward 内部闭环，也可以跨多次 forward 复用。

#### 4.2.2 核心流程

以 `FfnLayer` 为例，一次 forward 内 workspace 的状态流转：

```
forward(ffn_input, ffn_output)
  │
  ├─ allocateBuffer(token_num, moe_k, use_moe)
  │     │  对每个 workspace 指针：
  │     │    ptr = (T*) allocator_->reMalloc(ptr, 字节数, false)
  │     │  最后 is_allocate_buffer_ = true
  │     ▼
  ├─ Gemm1(ffn_input → inter_buf_)        // 写 workspace
  ├─ activation(inter_buf_)                // 原地改 workspace
  ├─ Gemm2(inter_buf_ → ffn_output)        // 读 workspace，写主输出
  │
  └─ if (is_free_buffer_after_forward_) freeBuffer();
        │  if (is_allocate_buffer_):
        │     allocator_->free(&inter_buf_); ...   // 各 buffer 逐一释放
        │     is_allocate_buffer_ = false
        ▼
```

关键在于 `reMalloc` 会把上一次同地址的分配「复用或扩容」，所以 `allocateBuffer` 即便被多次调用也不会泄漏，也不会无脑重复 malloc——这是 4.3 节的主题。

#### 4.2.3 源码精读

先看 `FfnLayer` 声明了哪些 workspace 成员指针。它们都初始化为 `nullptr`：

[FfnLayer.h:63-71](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/FfnLayer.h#L63-L71) — `inter_buf_`、`inter_buf_2_`（门控激活用）、`moe_gates_buf_`、`moe_fc_workspace_`、`mixed_gemm_workspace_`、`int8_gemm_workspace_` 等 workspace 指针。

注意它同时声明了**三个** `allocateBuffer` 重载（[FfnLayer.h:57-60](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/FfnLayer.h#L57-L60)）：无参的（满足基类契约）、`allocateBuffer(int moe_k, bool use_moe)`、以及真正干活的 `allocateBuffer(size_t token_num, int moe_k, bool use_moe)`。

无参那个被故意写成报错，强制你用带尺寸的版本：

[FfnLayer.cc:452-457](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/FfnLayer.cc#L452-L457) — `FfnLayer::allocateBuffer()` 直接 `FT_CHECK_WITH_INFO(false, "...deprecated. Use allocateBuffer(size_t token_num, ...) instead")`。

真正干活的重载（截取核心几行），注意每一行都是 `reMalloc(指针本身, 字节数, false)`——`reMalloc` 的返回值要写回指针（因为扩容会返回新地址）：

[FfnLayer.cc:459-503](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/FfnLayer.cc#L459-L503) — `allocateBuffer(size_t token_num, ...)`：对 `inter_buf_`、`inter_buf_2_`、各种 workspace 调 `reMalloc`，最后 `is_allocate_buffer_ = true`。

例如其中最关键的一行（非 MoE 分支，FP16/FP32 路径）：

[FfnLayer.cc:481-482](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/FfnLayer.cc#L481-L482) — `inter_buf_ = (T*)allocator_->reMalloc(inter_buf_, type_size * token_num * max_inter_size_, false);`

再看 `freeBuffer`——注意 `if (is_allocate_buffer_)` 守卫带来的幂等性：

[FfnLayer.cc:505-526](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/FfnLayer.cc#L505-L526) — `freeBuffer()`：仅当 `is_allocate_buffer_` 为真时才逐一 `allocator_->free(&xxx_buf_)`，最后置 `is_allocate_buffer_ = false`。

forward 的末尾，按开关决定是否释放（这里截取 MoE 早返回分支；标准分支同理）：

[FfnLayer.cc:164-167](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/FfnLayer.cc#L164-L167) — `if (is_free_buffer_after_forward_) freeBuffer();`。

析构函数兜底再 `freeBuffer` 一次，保证 workspace 一定被回收：

[FfnLayer.cc:444-450](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/FfnLayer.cc#L444-L450) — 析构里调 `freeBuffer()`，配合 `is_allocate_buffer_` 守卫，即使 `is_free_buffer_after_forward_=true` 的对象（buffer 早已释放）再次 `freeBuffer` 也安全。

`UnfusedAttentionLayer` 是第二个范例，套路完全一致。forward 开头按运行期 batch/seq 申请：

[UnfusedAttentionLayer.cc:39-43](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/UnfusedAttentionLayer.cc#L39-L43) — forward 从 `attention_mask` 读出 `request_batch_size`、`request_seq_len`，然后 `allocateBuffer(request_batch_size, request_seq_len)`。

它的带尺寸重载一次性申请 `q_buf_`、`k_buf_`、`v_buf_`、`qk_buf_`（分数矩阵）、`qkv_buf_` 等一堆 workspace：

[UnfusedAttentionLayer.cc:393-409](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/UnfusedAttentionLayer.cc#L393-L409) — `allocateBuffer(batch_size, seq_len)`：逐个 `reMalloc`，其中 `k_buf_2_`、`v_buf_2_` 是用 `q_buf_2_` 指针偏移切出来的（一段大 buffer 切三段，省一次 malloc）；末尾 `is_allocate_buffer_ = true`。

它的 `freeBuffer` 同样用 `if (is_allocate_buffer_)` 守卫（[UnfusedAttentionLayer.cc:413-426](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/UnfusedAttentionLayer.cc#L413-L426)），与 FfnLayer 如出一辙。

#### 4.2.4 代码实践

**实践目标**：亲手对照源码，确认 `freeBuffer` 的幂等性约定——它如何做到「重复调用也安全」。

**操作步骤**：

1. 打开 [FfnLayer.cc:505-526](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/FfnLayer.cc#L505-L526)，找到 `if (is_allocate_buffer_)` 这一行。
2. 想象一个 `is_free_buffer_after_forward_=true` 的 FfnLayer：forward 跑完 → 第 376 行 `freeBuffer()` 把 `is_allocate_buffer_` 置为 `false` → 之后某处（或析构）再调一次 `freeBuffer()`。
3. 再看 [FfnLayer.cc:444-450](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/FfnLayer.cc#L444-L450) 的析构函数，确认它无条件调 `freeBuffer()`。

**需要观察的现象**：第二次 `freeBuffer()` 时 `is_allocate_buffer_` 已是 `false`，整个 `if` 块被跳过，不会对已释放的指针再次 `free`。

**预期结果**：你会得出结论——`freeBuffer()` 的幂等性由**两个机制**共同保证：(1) `is_allocate_buffer_` 状态位守卫，避免重复释放与重复记账；(2) `allocator_->free()` 本身也对 `nullptr` 安全（[allocator.h:238](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/allocator.h#L238) 有 `if (*ptr != nullptr)` 守卫，并把 `*ptr=nullptr`）。这两层防护让「forward 里释放一次 + 析构里再释放一次」这种常见写法不会 double-free。

#### 4.2.5 小练习与答案

**练习 1**：为什么子类要把基类要求的无参 `allocateBuffer()` 写成直接报错（`FT_CHECK_WITH_INFO(false, ...)`）？

**参考答案**：因为 workspace 大小依赖运行期才知道的 `token_num`/`batch_size`/`seq_len`，无参版本根本无法知道该申请多大。把它写成断言失败，可以在编译期满足基类「必须实现纯虚函数」的契约，同时在运行期强制调用方使用带尺寸的重载 `allocateBuffer(size_t token_num, ...)`，避免误用。

**练习 2**：`UnfusedAttentionLayer::allocateBuffer` 里 `k_buf_2_ = q_buf_2_ + batch_size*seq_len*hidden_units_;`（[UnfusedAttentionLayer.cc:401-402](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/attention_layers/UnfusedAttentionLayer.cc#L401-L402)）为什么这样写？`freeBuffer` 里需要单独 free `k_buf_2_`、`v_buf_2_` 吗？

**参考答案**：这是「一次 malloc 切三段」的优化：只对 `q_buf_2_` 调一次 `reMalloc` 申请 `3 × batch × seq × hidden` 的大 buffer，再用指针偏移把 K、V 指过去，省掉两次 malloc。因此 `freeBuffer` 里**不能**再单独 free `k_buf_2_`/`v_buf_2_`——它们不是独立分配的，只 free `q_buf_2_` 一次即可，否则会 double-free（释放了偏移指针，再释放起始指针）。

---

### 4.3 allocator_->reMalloc：REUSE / INCREASE / DECREASE 三态决策（承接 u2-l2）

#### 4.3.1 概念说明

`allocateBuffer` 里反复出现的 `allocator_->reMalloc(ptr, bytes, false)` 是 workspace 复用的真正心脏。它的三态决策（u2-l2 详讲过）决定了「forward 第二次被调用、batch 变大时」会走哪条路：

- **REUSE**：账本里这个地址已存在，且已分配大小 == 新请求大小 → 直接返回原指针（可选 memset），**完全跳过 cudaMalloc**。
- **INCREASE**：账本里地址存在，但旧大小 < 新大小 → free 掉旧的，malloc 新的更大的。
- **DECREASE**：旧大小 > 新大小 → 在启用 CUDA 11.2 内存池时，回收多余显存；否则按 REUSE 处理。

本节把它和「同一个 layer 多次 forward」的场景串起来。

#### 4.3.2 核心流程

设想一个 `is_free_buffer_after_forward_=false` 的 FfnLayer（生产稳态常见配置，buffer 跨步保留），连续三次 forward，batch 分别为 8、32、32。关注 `inter_buf_` 的命运：

| 第几次 forward | 进入 allocateBuffer 时 `inter_buf_` | reMalloc 的判断 | 命中分支 | 实际开销 |
| --- | --- | --- | --- | --- |
| 1（batch=8） | `nullptr` | `isExist(nullptr)` = false | 走 else：`malloc` | 一次 cudaMallocAsync |
| 2（batch=32） | 上一步返回的指针 `0xAAA`（按 8 分配） | `isExist(0xAAA)`=true；旧大小 < 新大小 | **INCREASE** | free 旧 + malloc 新 |
| 3（batch=32） | `0xBBB`（按 32 分配） | 旧大小 == 新大小 | **REUSE** | 仅 memset，无 malloc |

稳态（第 3 次起，batch 不再变）命中 REUSE，正是 buffer 复用的核心收益。即便第 2 步 INCREASE 真的 free+malloc，配合 CUDA 11.2 异步内存池，free 是把指针还给池、malloc 是从池里取，开销也极小（详见 u2-l2）。

如果该 layer 是 `is_free_buffer_after_forward_=true`：每次 forward 末尾 `freeBuffer()` 把 `inter_buf_` 置回 `nullptr`，于是下一次 forward 总是走「`isExist(nullptr)`=false → malloc」分支——**这种配置下跨步不复用**，复用完全依赖内存池的快速回收/再发。这也是为什么稳态推理通常把该开关设为 `false`。

#### 4.3.3 源码精读

三态枚举定义：

[allocator.h:58-62](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/allocator.h#L58-L62) — `enum class ReallocType { INCREASE, REUSE, DECREASE };`

`reMalloc` 的核心决策逻辑（u2-l2 已精读，这里只点出与 batch 变大相关的分支）：

[allocator.h:74-107](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/allocator.h#L74-L107) — `reMalloc` 模板方法：先把 size 对齐到 32 字节，再查账本 `pointer_mapping_`，命中则按 `isReMalloc` 返回的三态分支处理。

具体看 INCREASE 分支（batch 变大的情形）：

[allocator.h:83-87](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/allocator.h#L83-L87) — INCREASE：free 旧指针、malloc 新尺寸。

以及 REUSE 分支（稳态命中的情形，零 malloc）：

[allocator.h:95-101](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/allocator.h#L95-L101) — REUSE：什么也不分配，按需 memset 后直接返回原指针。

三态的判定依据——账本里记录的旧大小 vs 新请求大小：

[allocator.h:133-145](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/allocator.h#L133-L145) — `isReMalloc`：旧大小 < 新 → INCREASE；== → REUSE；> → DECREASE。

把这条链串起来：`forward` → `allocateBuffer(token_num,...)` → `reMalloc(inter_buf_, bytes, false)` → `isReMalloc` 查账本 → 三态分支。batch 变大就触发 INCREASE，batch 稳定就触发 REUSE。

#### 4.3.4 代码实践

**实践目标**：跟踪「forward 第二次被调用、batch 变大」时 `reMalloc` 的决策路径，说清 REUSE 与 INCREASE 的区别。

**操作步骤**：

1. 假设有一个 `FfnLayer`，构造时 `is_free_buffer_after_forward=false`。
2. 在脑中（或纸面上）模拟连续三次 forward，batch = 8、32、32，只追踪 `inter_buf_` 一个指针。
3. 对照 [allocator.h:81-106](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/allocator.h#L81-L106)，每次都判断：(a) `inter_buf_` 当前是不是 `nullptr`？(b) 若不是，账本里它的大小是多少？(c) `isReMalloc` 返回哪个态？

**需要观察的现象**：

- 第 1 次：`inter_buf_==nullptr` → `isExist` 为 false → `malloc`。
- 第 2 次：`inter_buf_` 非空，旧大小（按 8 算）< 新大小（按 32 算）→ INCREASE → free+malloc，**返回值必须写回 `inter_buf_`**（地址变了）。
- 第 3 次：旧大小 == 新大小 → REUSE → 不 malloc。

**预期结果**：你能解释为什么 `allocateBuffer` 里每一行都必须写成 `ptr = (T*)allocator_->reMalloc(ptr, bytes, false);`——因为 INCREASE 会返回新地址，不写回就会用悬空的老地址。同时你能解释稳态下（batch 不再变）`reMalloc` 退化为「仅 memset」，这正是 FT 高吞吐的关键之一。

**待本地验证**：若想在运行时确认走的是哪个分支，可在 [allocator.h:83-101](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/allocator.h#L83-L101) 的三个 `FT_LOG_DEBUG` 处看日志——设置环境变量 `FT_LOG_LEVEL=DEBUG`（u1-l5 讲过）即可看到 "Reuse original buffer ..." / "ReMalloc the buffer ... since it is too small." 等输出。

#### 4.3.5 小练习与答案

**练习 1**：若把 `allocateBuffer` 里某一行误写成 `allocator_->reMalloc(inter_buf_, bytes, false);`（漏掉左值赋值），第二次 batch 变大时会发生什么？

**参考答案**：INCREASE 分支会 free 掉老指针并 malloc 新指针返回，但返回值被丢弃、`inter_buf_` 仍指向已被 free 的老地址（悬空指针）。后续 GEMM 往这个老地址写数据就是 use-after-free，且新分配的 buffer 永远不会在账本里被 `inter_buf_` 这个键关联，可能造成显存泄漏或随机崩溃。所以「返回值必须写回」是不可省的约定。

**练习 2**：什么情况下 batch 变大却**不**触发 INCREASE？

**参考答案**：当 `inter_buf_` 是 `nullptr` 时——`isExist(nullptr)` 为 false，直接走 else 分支的 `malloc`，根本不经过三态判定。这发生在 layer 刚构造完的第一次 forward，或 `is_free_buffer_after_forward_=true` 配置下每次 forward 之初（上一步 freeBuffer 把指针置回了 nullptr）。

---

### 4.4 GPUBuf：测试用的 RAII 显存封装（gpu_buf.h）

#### 4.4.1 概念说明

`gpu_buf.h` 里的 `GPUBuf<T>` 是一套**和 `BaseLayer`/`IAllocator` 完全不同**的显存抽象。它解决的是另一个场景的问题：**写单元测试时，需要一小块「申请—用—自动释放」的 GPU 内存，方便、不会泄漏即可，不在乎复用性能。**

它和 workspace 体系的关系是「互不通用」：

| 维度 | `IAllocator::reMalloc`（workspace 体系） | `GPUBuf<T>`（gpu_buf.h） |
| --- | --- | --- |
| 使用场景 | 生产 forward 的热路径 workspace | 单元测试、一次性数据搬运 |
| 复用 | 有账本，REUSE 命中跳过 malloc | 无复用，构造即 malloc、析构即 free |
| 底层调用 | `cudaMallocAsync`（走内存池） | `deviceMalloc`（冷路径薄封装，见 u2-l2） |
| 生命周期 | 由 layer 的 `is_allocate_buffer_` 手动管理 | RAII：构造分配、析构释放 |
| 数据搬运 | 不负责 | 内置 `set`/`to_host`/`to_host_vec` 等 |

#### 4.4.2 核心流程

`GPUBuf<T>` 是一个模板类，提供：

- 构造：`GPUBuf(size, random_init=true)` → `deviceMalloc(&ptr, size, random_init)` 申请 `size` 个 `T`（注意是元素个数，内部乘 `sizeof(T)`）。
- 拷贝构造：`GPUBuf(const GPUBuf<T2>&)` → 申请同尺寸、`set` 拷贝（类型不同时走 `invokeCudaCast` 做类型转换）。
- `set(const GPUBuf<T2>&)`：D2D 拷贝或类型转换。
- `set(const T* h_ptr)`：H2D 拷贝（从主机指针灌数据）。
- `to_host(T* h_ptr)` / `to_host_vec()`：D2H 拷贝回主机。
- `zero()`：`deviceMemSetZero`。
- 析构：`if (ptr != nullptr) cudaFree(ptr);` —— RAII 保证释放。

#### 4.4.3 源码精读

`GPUBuf` 整个类很短，关键是构造函数直接 `deviceMalloc`、析构函数直接 `cudaFree`：

[gpu_buf.h:30-42](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/gpu_buf.h#L30-L42) — 构造函数：`deviceMalloc(&ptr, size, random_init)` 申请；拷贝构造：申请同尺寸后 `set` 灌数据。

RAII 析构，且对 `nullptr` 安全：

[gpu_buf.h:77-81](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/gpu_buf.h#L77-L81) — `~GPUBuf()`：`if (ptr != nullptr) cudaFree(ptr);`。

类型转换分支（`T` 与 `T2` 不同时用 kernel 转换而非逐字节拷贝）：

[gpu_buf.h:44-53](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/gpu_buf.h#L44-L53) — `set(const GPUBuf<T2>&)`：同类型走 `cudaD2Dcpy`，异类型走 `invokeCudaCast`。

> 说明：`deviceMalloc`、`cudaD2Dcpy`、`cudaH2Dcpy`、`invokeCudaCast`、`deviceMemSetZero` 都来自 `memory_utils.h`（u2-l2 讲过），`gpu_buf.h` 通过 `#include "memory_utils.h"` 引入（[gpu_buf.h:21](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/gpu_buf.h#L21)）。这些都是冷路径的一次性封装，**不**经过 `IAllocator` 的复用账本。

#### 4.4.4 代码实践

**实践目标**：在仓库里找到 `GPUBuf` 的真实用法，确认它属于测试/示例路径，而非生产 forward 路径。

**操作步骤**：

1. 在仓库根目录执行 `grep -rn "GPUBuf" src/ examples/ tests/ | head -20`（这是查看用法的命令，**待本地执行**）。
2. 观察命中的文件集中在哪些目录。

**需要观察的现象**：命中基本集中在 `tests/` 下的单元测试（如 `test_gemm.cu`、`test_sampling.cu` 等），生产层的 `forward` 里几乎不用 `GPUBuf`。

**预期结果**：你会确认 `GPUBuf` 是「测试专用」的 RAII 显存封装，生产代码用 `IAllocator`+`BaseLayer` 这套带复用的体系。两类抽象各司其职，不要混用。

**待本地验证**：上面那条 grep 命令的具体输出需在你本地仓库执行后确认。

#### 4.4.5 小练习与答案

**练习 1**：如果在一个生产 layer 的 forward 里用 `GPUBuf` 而不是 `IAllocator::reMalloc` 来申请 workspace，会有什么问题？

**参考答案**：(1) 性能——`GPUBuf` 每次 forward 构造都 `deviceMalloc`、析构都 `cudaFree`，没有账本复用，稳态下无法命中 REUSE；(2) 异步性——`GPUBuf` 的 `cudaFree` 是同步的，且不在 layer 的 stream 上，可能打乱异步流水线。所以生产路径必须用 `IAllocator`。

**练习 2**：`GPUBuf` 的拷贝构造 `GPUBuf(const GPUBuf<T2>& buf_src)` 为什么要先 `deviceMalloc(&ptr, size, false)` 再 `set(buf_src)`，而不是直接 `ptr = buf_src.ptr`？

**参考答案**：因为 `GPUBuf` 拥有自己的显存（所有权语义），拷贝应是「深拷贝」——新对象申请独立显存并复制内容。直接赋值指针会导致两个 `GPUBuf` 析构时对同一块显存 `cudaFree` 两次（double-free）。`set` 里还会按需做类型转换（`T2→T`），这是浅拷贝做不到的。

---

### 4.5 为什么坚持 buffer 复用而非每步 malloc

把前四节串起来，回答本讲最后一个目标问题。FT 在每一层都坚持「buffer 复用 + 跨步保留」而非「每步 forward 都 malloc/free 全部 workspace」，根本原因有三：

1. **`cudaMalloc` / `cudaFree` 本身是慢且可能同步的操作**。在 GPU 上分配显存要走驱动层，开销在微秒到百微秒级，而一个 transformer block 有几十个 workspace。如果每步都全量 malloc/free，分配开销会淹没计算本身。

2. **稳态推理的 batch 几乎不变**。服务部署后，请求 batch 通常稳定（或只在几个固定值间切换）。`IAllocator` 的账本 + REUSE 让稳态下 `reMalloc` 退化为「仅 memset」，分配开销趋近于零。即便 batch 切换触发 INCREASE，CUDA 11.2 异步内存池也让 free+malloc 退化为池内指针搬运。

3. **靠 `is_allocate_buffer_` 与 `is_free_buffer_after_forward_` 两层开关，把「复用」做成可选策略**：
   - `is_free_buffer_after_forward_=false`：workspace 跨步保留，稳态命中 REUSE，最快，但常驻显存。
   - `is_free_buffer_after_forward_=true`：每步末尾释放，显存占用低，适合显存紧张场景，靠内存池兜底速度。
   - `is_allocate_buffer_`：让 `freeBuffer` 幂等，使得「forward 内释放 + 析构兜底」的组合不会 double-free。

这套设计把「热路径显存管理」从 layer 的业务逻辑里彻底剥离：layer 只管声明 `xxx_buf_` 指针、在 `allocateBuffer` 里调 `reMalloc`、在 `freeBuffer` 里调 `free`，复用、对齐、内存池、账本这些复杂的事都交给 `IAllocator`。这也是 u3-l1～u3-l4 讲过的所有层（layernorm/attention/ffn）能保持业务代码简洁的原因——它们不必关心显存从哪来、会不会泄漏。

---

## 5. 综合实践

**任务**：以 `FfnLayer` 为对象，画出「同一个 layer 连续三次 forward，batch = 4 → 16 → 16」时，`inter_buf_` 与 `is_allocate_buffer_` 在每一步的状态变化表，并标注 `reMalloc` 每次命中的三态分支。分两种配置各画一张：(A) `is_free_buffer_after_forward=false`；(B) `is_free_buffer_after_forward=true`。

**要求**：

1. 表格列至少包含：第几次 forward、`inter_buf_` 进入 allocateBuffer 时的值、`is_allocate_buffer_` 进入时的值、`reMalloc` 命中的态（INCREASE/REUSE/DECREASE/首次 malloc）、`inter_buf_` 离开 forward 时的值、`is_allocate_buffer_` 离开 forward 时的值。
2. 对配置 (A)，解释为什么第 3 次 forward 是「零 malloc」。
3. 对配置 (B)，解释为什么每次 forward 都要重新 malloc，以及为什么仍然不算很慢（提示：CUDA 内存池）。
4. 引用你判断的源码依据，至少给出三条永久链接：[FfnLayer.cc:481-482](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/FfnLayer.cc#L481-L482)（reMalloc 调用点）、[allocator.h:83-101](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/allocator.h#L83-L101)（三态分支）、[FfnLayer.cc:505-526](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/layers/FfnLayer.cc#L505-L526)（freeBuffer 幂等守卫）。

**参考结论**（配置 A，`is_free_buffer_after_forward=false`）：

| 次 | `inter_buf_` 进入 | `is_allocate_buffer_` 进入 | reMalloc 命中 | `inter_buf_` 离开 | `is_allocate_buffer_` 离开 |
| --- | --- | --- | --- | --- | --- |
| 1 (b=4) | `nullptr` | false | 首次 malloc | `0xAAA` | true |
| 2 (b=16) | `0xAAA` | true | **INCREASE** | `0xBBB`（新地址） | true |
| 3 (b=16) | `0xBBB` | true | **REUSE**（零 malloc） | `0xBBB` | true |

第 3 次零 malloc，因为旧大小（按 16）== 新请求大小（按 16），命中 [allocator.h:95-101](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/allocator.h#L95-L101) 的 REUSE 分支。配置 (B) 下每次 forward 末尾 `freeBuffer` 把 `inter_buf_` 置回 `nullptr`，所以每次 allocateBuffer 都走「`isExist(nullptr)`=false → malloc」，但因为 `cudaMallocAsync`/`cudaFreeAsync` 走内存池，free 是还池、malloc 是取池，开销极小。

---

## 6. 本讲小结

- `BaseLayer` 是所有层/模型的非模板抽象基类，收拢了「设备环境」（stream / cublas_wrapper / allocator / cuda_device_prop）并定义两个纯虚接口 `allocateBuffer()` / `freeBuffer()`；**不存在 `queryBuffer`**，workspace 通过子类 protected 成员指针访问。
- 具体层遵守统一约定：声明 `T* xxx_buf_=nullptr` 成员、把无参 `allocateBuffer()` 写成报错以强制使用带尺寸重载、`freeBuffer()` 用 `is_allocate_buffer_` 守卫保证幂等。
- `allocateBuffer` 里每一行 `ptr = (T*)allocator_->reMalloc(ptr, bytes, false)` 是 workspace 复用的入口；`reMalloc` 的 REUSE/INCREASE/DECREASE 三态决策（u2-l2）决定了「forward 多次调用、batch 变化」时的实际开销。
- `freeBuffer` 的幂等性由两层保证：`is_allocate_buffer_` 状态位守卫 + `allocator_->free` 对 nullptr 安全，使得「forward 内释放 + 析构兜底」不会 double-free。
- `gpu_buf.h` 的 `GPUBuf<T>` 是测试专用的 RAII 显存封装（构造 malloc、析构 cudaFree、无复用），与 `IAllocator` 体系互不通用，不要在生产 forward 里混用。
- FT 坚持 buffer 复用而非每步 malloc，因为 `cudaMalloc/Free` 慢且可能同步，而稳态 batch 稳定让 REUSE 命中后 `reMalloc` 退化为仅 memset，这是高吞吐的关键之一。

---

## 7. 下一步学习建议

- **横向验证**：拿 u3-l3（注意力层）、u3-l4（FFN 层）讲过的任意一个具体层，对照本讲的「成员指针 + allocateBuffer(size_t) + 幂等 freeBuffer」三件套检查一遍，你会发现它们完全套用同一模板。
- **纵向进入模型层**：本讲的 `BaseLayer` 契约同样适用于 `models/` 下的模型类（如 `Bert`、`ParallelGpt`），它们也持有自己的 workspace 并在 forward 中 `allocateBuffer`/`freeBuffer`。进入 u4-l1（BERT 模型与 forward 主流程）时，留意 model 层是如何把多个子 layer 的 workspace 协调起来的。
- **回看分配器细节**：如果想彻底搞清「内存池让 INCREASE/DECREASE 廉价」的机制，回看 u2-l2 里 `cudaMallocAsync`、`cudaMemPoolSetAttribute`、`CUDA_MEMORY_POOL_DISABLED` 这些内容，对应 [allocator.h:152-181](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/src/fastertransformer/utils/allocator.h#L152-L181)。
- **调试实践**：在 u1-l5 学过的 `FT_LOG_LEVEL=DEBUG` 下运行任意 example，观察 `reMalloc` 的 "Reuse original buffer ..." / "ReMalloc the buffer ... since it is too small." 日志，亲眼看到三态分支的命中情况。
