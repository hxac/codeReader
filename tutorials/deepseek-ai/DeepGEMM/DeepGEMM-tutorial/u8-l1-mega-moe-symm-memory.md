# Mega MoE 概念与对称内存

## 1. 本讲目标

本讲是「Mega MoE 融合内核」单元的第一篇。学完后你应当能够：

1. 说清 **Mega MoE** 把哪些阶段融合进了同一个 kernel，以及为什么要融合（让 NVLink 通信与 tensor core 计算重叠）。
2. 看懂 **`SymmBuffer`** 如何借助 PyTorch 的 symmetric memory，在多进程（多 rank）之间建立一块「彼此可直接寻址」的缓冲，从而让设备 kernel 能用一次 TMA 直接读写远端 GPU 的显存。
3. 读懂 `get_symm_buffer_for_mega_moe` 里 **ring token 预算**的推导：什么时候按 prefill 给约 8 GB、什么时候按 decode 给约 18 GB，以及 `get_ring_limit_for_mega_moe` 返回的最小/最大值如何夹住最终选择。

本讲承接 u2-l2（UE8M0 打包缩放因子）与 u7-l1（M 轴分组 GEMM 的 contiguous 布局），是后续 u8-l2（权重变换）、u8-l3（wave 调度器）、u8-l4（融合内核内部）的前置。

## 2. 前置知识

阅读本讲前，请确认你已经理解以下概念（前几讲已建立）：

- **MoE（Mixture of Experts）**：每个 token 会被路由（route）到少数几个「专家」网络，而不是跑完整的 FFN。一次 MoE 前向通常包含两次线性层（Linear1、Linear2）夹一个非线性激活（这里固定是 SwiGLU）。
- **EP（Expert Parallelism）**：把不同 expert 切到不同 GPU（rank）上。于是每个 rank 收到的 token 里，只有路由到「本 rank 负责的 expert」那部分需要本地计算。**dispatch** 就是「把 token 发到对的 rank」的阶段，**combine** 就是「把各 rank 算好的结果按原 token 收回」的阶段，二者都是跨 GPU 的通信（走 NVLink）。
- **缩放因子（SF）与 UE8M0 打包**（u2-l2）：FP8/FP4 表示范围窄，需逐块缩放；SM100 用 4 个 UE8M0（仅指数）打包进一个 `int32`，由 UMMA 硬件吸收。
- **分组 GEMM 的 contiguous 布局**（u7-l1）：把多个变长段拼进一根长轴，用一张布局表标记每段归属。
- **对称内存（symmetric memory）**：本讲的新概念，下面会展开。

> 术语约定：本讲里 **rank** 指一个参与分布式训练的 GPU 进程；**宿主（host）** 指 CPU 侧代码，**设备（device）** 指 GPU 侧 kernel。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [deep_gemm/mega/__init__.py](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/mega/__init__.py) | Python 侧入口：`SymmBuffer` 类、`get_symm_buffer_for_mega_moe`、两个 mega-kernel 调用包装。 |
| [csrc/apis/mega.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/mega.hpp) | C++ 侧 API 层：计算 buffer 字节数与切片函数、按架构派发到 SM100 mega-kernel。 |
| [deep_gemm/include/deep_gemm/layout/mega_moe.cuh](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/layout/mega_moe.cuh) | 设备侧布局头文件：`Workspace`（元数据/屏障区）、`Data`/`Buffer`、候选 block 尺寸常量。 |
| [csrc/jit_kernels/heuristics/mega_moe.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/mega_moe.hpp) | 启发式：`get_num_wave_pool_tokens`（ring 容量上限）等预算推导。 |
| [tests/test_mega_moe.py](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/test_mega_moe.py) | 多进程示例：如何申请 buffer、变换权重、填输入、调用 mega-kernel。 |

---

## 4. 核心概念与源码讲解

### 4.1 融合 mega-kernel：Mega MoE 的概念与各阶段

#### 4.1.1 概念说明

传统 MoE 前向被拆成 **5 个独立 kernel**，串行执行：

1. **EP dispatch**：把本地 token 按 topk 路由结果发往各 rank（通信）。
2. **Linear1**（FP8 act × FP4 weight）：第一层 GEMM，输出 `2 * intermediate_hidden`（gate + up 拼在一起）。
3. **SwiGLU**：激活，把 gate、up 两半融合成 `intermediate_hidden`。
4. **Linear2**（FP8 × FP4）：第二层 GEMM，映射回 `hidden`。
5. **EP combine**：把各 rank 的结果按原 token 收回（通信）。

这种串行结构有两个浪费：**dispatch/combine 是纯通信阶段，期间 tensor core 空转**；每两个 kernel 之间还要把中间结果写回 HBM 再读出。

**Mega MoE** 的核心想法是把这 5 步融合成 **一个 mega-kernel**，在该 kernel 内部让「拉取远端 token（dispatch pull）」与「本地 GEMM 计算」**重叠**：当一部分 SM 在做 NVLink 拉取时，另一部分 SM 已经在驱动 tensor core 做乘加。README 对此的描述是：

> Mega MoE fuses and overlaps EP dispatch, linear 1 (FP8xFP4), SwiGLU, linear 2 (FP8xFP4), and EP combine into a single mega-kernel, overlapping NVLink communication and tensor core computation. It requires multi-process launch with symmetric memory.
>
> ——[README.md:114-116](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/README.md#L114-L116)

这里有两个**硬性前提**：

- **多进程启动 + 对称内存**：因为 dispatch/combine 要跨 GPU，且融合进 kernel 后无法再用普通的集合通信库（如 NCCL）单独发起，必须让 kernel 自己直接读写远端显存。本讲的 4.2 专门讲这件事。
- **架构仅支持 SM100（Blackwell，`arch_major == 10`）**：mega-kernel 用到了 SM100 的 UMMA/tcgen05、grid_sync、NVLink 屏障等机制；SM90 会直接报不可达。

#### 4.1.2 核心流程

从用户视角看一次 Mega MoE 调用的生命周期：

```text
get_symm_buffer_for_mega_moe(...)   # ① 申请对称内存 buffer（每个 rank 都调一次，需多进程）
        │
        ▼
SymmBuffer.__init__                  # ② 用 torch 或 symm_mem 分配；多 rank 时 rendezvous 交换地址
        │
        ▼
transform_weights_for_mega_moe(...)  # ③ 把 L1/L2 权重重排为 kernel 所需布局（u8-l2 详讲）
        │
        ▼  （每个 iter 重复以下三步）
buffer.x[...].copy_(x)               # ④ 把当前输入拷进 buffer（输入必须落在对称内存里！）
buffer.topk_idx/topk_weights.copy_(...)
        │
        ▼
fp8_fp4_mega_moe(y, l1_w, l2_w, buffer)   # ⑤ 单次 kernel launch：dispatch→L1→SwiGLU→L2→combine 全在里面
        │
        ▼
y  # [num_tokens, hidden] 的 BF16 输出（直接落在普通显存）
```

注意第 ④ 步：输入 `x`、`topk_idx`、`topk_weights` **必须先拷进 buffer**，因为 mega-kernel 的 dispatch pull 会从这块对称内存的固定偏移读 token，而不是从用户传入的任意张量读。这跟普通 GEMM「直接吃用户张量」不同，是融合带来的接口约束。

#### 4.1.3 源码精读

Python 侧的入口包装极薄：`fp8_fp4_mega_moe` 把 buffer、`handle.buffer_ptrs`、rank 等参数原样透传给 C++ 扩展 `_C.fp8_fp4_mega_moe`。

- [deep_gemm/mega/__init__.py:155-176](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/mega/__init__.py#L155-L176)：注意它把 `sym_buffer.handle.buffer_ptrs`（每个 rank 一个地址）和 `sym_buffer.group.rank()`（自己是谁）一起传下去——这正是 kernel 能定位「远端 rank 的 buffer 在哪」的钥匙。

C++ 侧 `fp8_fp4_mega_moe` 做校验后，按架构派发，非 SM100 直接不可达：

- [csrc/apis/mega.hpp:233-248](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/mega.hpp#L233-L248)：`if (arch_major == 10)` 才调用 `sm100_fp8_fp4_mega_moe(...)`，否则 `DG_HOST_UNREACHABLE("Unsupported architecture")`。
- [csrc/apis/mega.hpp:179-182](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/mega.hpp#L179-L182)：FP8xFP4 路径固定 `recipe == (1,1,32)`、`activation == "swiglu"`——这也是本讲不再展开 recipe 的原因，它是 u2-l2 内容。

校验里有一个关键不变量：`num_experts % num_ranks == 0`，即 expert 必须能被 rank 数整除，保证每个 rank 分到同样多个 expert（[csrc/apis/mega.hpp:37](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/mega.hpp#L37)）。

#### 4.1.4 代码实践

**实践目标**：从测试代码里确认「输入必须拷进 buffer、输出落在普通显存」这一接口约定。

**操作步骤**：

1. 打开 [tests/test_mega_moe.py:104-121](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/test_mega_moe.py#L104-L121)。
2. 阅读 `run_fused()`：观察它每次调用前都把 `x`、`x_sf`、`topk_idx`、`topk_weights` 拷进 `buffer.*`，然后用 `torch.empty(...)` 在普通显存新建 `y`。
3. 阅读它最后的 `(deep_gemm.bf16_mega_moe if is_bf16xbf16 else deep_gemm.fp8_fp4_mega_moe)(**kernel_kwargs)`，确认 `sym_buffer=buffer` 被整体传入。

**需要观察的现象**：`run_fused` 没有任何 `dispatch` / `combine` / `swiglu` 的单独调用——这 5 步全部消失在 `fp8_fp4_mega_moe` 一次调用里。对比之下，同文件的 `run_baseline()`（[tests/test_mega_moe.py:175-202](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/test_mega_moe.py#L175-L202)）把这 5 步显式拆开。

**预期结果**：能口述「fused 路径 = 1 次 kernel launch，baseline 路径 = dispatch + L1 + swiglu + L2 + combine 5 步」。若在本机跑（需 SM100 + PyTorch ≥ 2.9 + 多进程），fused 与 baseline 的逐位相等断言 `torch.equal(fused_result, baseline_result)`（[tests/test_mega_moe.py:212](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/test_mega_moe.py#L212)）应通过；本机若无相应硬件则为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 Mega MoE 强制 `activation == "swiglu"`，而不像某些库那样允许传入任意激活函数？

**参考答案**：因为激活被融合进了 mega-kernel，它不是一个可插拔的 Python 回调，而是 kernel 内部一段写死的指令序列（SwiGLU 还需要在 FP8 路径下做跨 warp 的 amax 归约以产生下一层的 SF）。换激活等于重写 kernel，所以宿主侧用 `assert` 提前挡住其他值（[deep_gemm/mega/__init__.py:26](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/mega/__init__.py#L26)）。

**练习 2**：如果在一块 SM90（Hopper）GPU 上调用 `fp8_fp4_mega_moe`，会发生什么？

**参考答案**：C++ 侧 `device_runtime->get_arch_major()` 返回 9，`if (arch_major == 10)` 不成立，落到 `DG_HOST_UNREACHABLE("Unsupported architecture")`（[csrc/apis/mega.hpp:246-248](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/mega.hpp#L246-L248)），翻译成 Python 异常抛出。

---

### 4.2 对称内存 SymmBuffer

#### 4.2.1 概念说明

要让单个 kernel 在内部完成跨 GPU 的 dispatch/combine，kernel 必须能 **直接读写其它 rank 的显存**。NCCL 这类集合通信 API 是「主机侧发起、kernel 间同步」的模型，无法被塞进另一个 kernel 内部。解决办法是 **对称内存（symmetric memory）**：

- 每个 rank 各自分配一块**相同大小**的 device buffer；
- 通过 PyTorch 的 `torch.distributed._symmetric_memory`（需要 PyTorch ≥ 2.9），让每个 rank 都能拿到 **所有 rank 的 buffer 物理地址**；
- 于是 kernel 可以用这些地址发起 **NVLink 上的直接 load/store（在 DeepGEMM 里走 TMA 的 1D bulk copy）**，把「通信」变成 kernel 内部几条访存指令。

`SymmBuffer` 就是这块对称内存的 Python 句柄，它还顺带在 buffer 里切出 `x` / `x_sf` / `topk_idx` / `topk_weights` / `l1_acts` / `l1_acts_sf` / `l2_acts` / `l2_acts_sf` 共 8 个视图，让用户能往里填输入。

#### 4.2.2 核心流程

`SymmBuffer.__init__` 做四件事：

```text
① 问 C++ 要 buffer 总字节数 + 一个「切片函数」slice_input_buffers
② 按 group.size() 选择分配器：
     size==1 → torch.empty       + SimpleNamespace(buffer_ptrs=[data_ptr()])
     size>1  → symm_mem.empty    + symm_mem.rendezvous(buffer, group)
③ zero_() → group.barrier() → torch.cuda.synchronize()   # 全员初始化完毕才继续
④ 用 slice_input_buffers 切出 8 个视图
```

关键分叉在「单 rank」与「多 rank」：

| 情形 | 分配器 | handle | 含义 |
| --- | --- | --- | --- |
| `group.size() == 1` | `torch.empty` | `SimpleNamespace(buffer_ptrs=[本地 data_ptr])` | 无跨卡通信，kernel 只需本地址；用 torch 普通 buffer 即可，handle 仅是个长度为 1 的地址列表。 |
| `group.size() > 1` | `symm_mem.empty` | `symm_mem.rendezvous(buffer, group)` 的返回 | 每个 rank 互相登记 buffer，handle.buffer_ptrs 是「每个 rank 一个地址」的列表。 |

**为什么单 rank 也保留这套接口？** 这样 fused kernel 和测试逻辑在 EP=1（单卡）退化场景下仍能跑通，不用写两套代码路径——单 rank 时「dispatch/combine」退化为本地拷贝。

#### 4.2.3 源码精读

`SymmBuffer.__init__` 的核心片段：

- [deep_gemm/mega/__init__.py:35-52](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/mega/__init__.py#L35-L52)：
  ```python
  num_bytes, slice_input_buffers = _C.get_symm_buffer_size_for_mega_moe(...)
  allocator = torch if group.size() == 1 else symm_mem
  self.buffer = allocator.empty(num_bytes, dtype=torch.int8, device='cuda')
  self.handle = (
      types.SimpleNamespace(buffer_ptrs=[self.buffer.data_ptr()])
      if group.size() == 1
      else symm_mem.rendezvous(self.buffer, group=group)
  )
  self.buffer.zero_()
  self.group.barrier()
  torch.cuda.synchronize()
  ```

  注意三点：
  1. **`handle` 的统一接口**：无论哪条分支，`handle.buffer_ptrs` 都是一个「按 rank 顺序排列的地址列表」，这就是后续传给 kernel 的 `sym_buffer_ptrs`（见 [deep_gemm/mega/__init__.py:170](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/mega/__init__.py#L170) 与 [csrc/apis/mega.hpp:167](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/mega.hpp#L167)）。
  2. **`rendezvous` 的作用**：让本 rank 把「我这块 buffer 的物理地址」登记给组内所有 rank，并拿到它们的地址。没有这一步，kernel 手里只有本地地址，无法发起跨卡访问。rendezvous 必须在分配之后、任何 kernel 调用之前完成。
  3. **`zero_()` + `barrier()` + `synchronize()`**：确保每个 rank 都把 buffer 清零并真正写回显存后，再往下走。mega-kernel 内部的栅栏（grid_sync / NVLink barrier）依赖这块内存处于一致的初始状态。

8 个视图的切片发生在 C++ 的 `slice_input_buffers` 闭包里，宿主侧只调用一次：

- [csrc/apis/mega.hpp:121-157](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/mega.hpp#L121-L157)：用 `torch::from_blob` 在 buffer 的各个固定偏移上「零拷贝」地构造视图。注意注释里强调 `x_sf` 是 K-major，而 `l1_acts_sf` / `l2_acts_sf` 是 M-major——这是 SF 在不同阶段被 kernel 以不同方式 UTCCP 加载所需的布局差异（承接 u2-l2）。

调用结束后的销毁：

- [deep_gemm/mega/__init__.py:60-65](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/mega/__init__.py#L60-L65)：`destroy()` 把 `handle`/`buffer`/`group` 置空，释放对对称内存的引用（测试里 `dist.barrier()` 之后调用，见 [tests/test_mega_moe.py:271](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/test_mega_moe.py#L271)）。

#### 4.2.4 代码实践

> 这是本讲的主实践任务。

**实践目标**：精读 `SymmBuffer.__init__`，回答三个问题：(a) `group.size()==1` 与 `>1` 时分配器有何区别？(b) 两种 handle 有何区别？(c) 为什么多 rank 必须先 `rendezvous`？

**操作步骤**：

1. 打开 [deep_gemm/mega/__init__.py:18-52](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/mega/__init__.py#L18-L52)。
2. 画出一张表（参考本讲 4.2.2 的表格），逐行填写 `size==1` 与 `size>1` 在「分配器」「handle 类型」「`buffer_ptrs` 长度」三栏的差异。
3. 追踪 `self.handle.buffer_ptrs` 的去向：跳到 [deep_gemm/mega/__init__.py:164-175](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/mega/__init__.py#L164-L175)，确认它作为 `sym_buffer.handle.buffer_ptrs` 被传入 C++。
4. 在 C++ 侧 [csrc/apis/mega.hpp:219](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/mega.hpp#L219) 看到 `num_ranks = sym_buffer_ptrs.size()`——即「地址个数 = rank 数」，印证 `buffer_ptrs` 是「每 rank 一个地址」。

**需要观察的现象 / 预期结果**：

- **(a)** `size==1` 用 `torch.empty`（普通显存分配），`size>1` 用 `symm_mem.empty`（对称内存分配器，会注册为可被远端 rank 直接寻址）。
- **(b)** `size==1` 的 handle 是一个仅含本地 `data_ptr()` 的 `SimpleNamespace`；`size>1` 的 handle 是 `symm_mem.rendezvous` 的返回对象，其 `buffer_ptrs` 含每个 rank 的地址。
- **(c) 为什么需要 rendezvous**：mega-kernel 在内部直接用 `sym_buffer_ptrs[远端 rank]` 这个地址发起 NVLink 读写；只有经过 rendezvous，本 rank 才知道远端 buffer 的物理地址，且远端这块内存才被标记为「允许本卡直接访问」。`size==1` 时不存在远端，所以只需要本地 `data_ptr()`、不需要 rendezvous。

> 本实践为源码阅读型，无需运行；若要在本机验证 `buffer_ptrs` 长度，可临时在 `__init__` 里加一行 `print(len(self.handle.buffer_ptrs), group.size())`（需 SM100 + 多进程环境），结果应为「地址数 == rank 数」——若无法运行则标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：如果删掉 `__init__` 末尾的 `torch.cuda.synchronize()`，仅保留 `group.barrier()`，可能出现什么问题？

**参考答案**：`group.barrier()` 只保证「发起清零的 host 侧调用都已到达」，但 `zero_()` 是异步的 GPU 操作；没有 `torch.cuda.synchronize()` 就让某些 rank 提前进到 mega-kernel，此时远端 buffer 可能尚未真正写零，kernel 读到的元数据/计数区会是脏值。`synchronize()` 确保清零真正落盘后才放行（[deep_gemm/mega/__init__.py:50-52](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/mega/__init__.py#L50-L52)）。

**练习 2**：为什么 `handle` 要设计成统一暴露 `.buffer_ptrs` 属性，而不是单 rank 直接返回一个裸指针？

**参考答案**：为了让 kernel 的调用代码（[deep_gemm/mega/__init__.py:170](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/mega/__init__.py#L170)）在 EP=1 和 EP>1 时完全一致：`sym_buffer.handle.buffer_ptrs` 永远是「地址列表」，C++ 侧用它的长度推断 rank 数（[csrc/apis/mega.hpp:219](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/mega.hpp#L219)）。统一接口省掉了调用点的分支判断。

---

### 4.3 ring token 预算：buffer 该开多大

#### 4.3.1 概念说明

对称内存按字节计费、且要为每个 rank 都开一份，所以 **buffer 不能开到「最坏情况」那么大**——最坏情况下所有 token 都路由到同一个 expert，会撑爆显存。Mega MoE 采用 **ring buffer（环形缓冲）** 思路：只开一个固定容量的「token 池」，kernel 像 FIFO 一样复用它，处理完一个 expert 的 token 就把空间还给池子。

池子的容量用 **ring token 数**（`num_ring_tokens`）衡量：它表示「同时能驻留在池子里的 token 上限」。这个值太小，计算单元会饿着等通信；太大，显存吃不消。所以 `get_symm_buffer_for_mega_moe` 的核心就是 **在「最小必需容量」和「最大可用容量」之间，挑一个对当前 batch 性状合理的预算**。

两个边界由 `get_ring_limit_for_mega_moe` 给出：

- **下界（`num_min_ring_tokens`）**：每个 wave 只处理 1 个 expert 时所需的最小 token 数。
- **上界（`num_max_ring_tokens`）**：每个 wave 处理「本 rank 全部 expert」时所需的最大 token 数。

其中 **wave** 是调度单位：一个 wave 内，一组 SM 协作处理若干个 expert 的全部 token（u8-l3 详讲 wave 调度器）。容量必须至少够放下「一个 wave 想同时拉的 token」。

#### 4.3.2 核心流程

`get_symm_buffer_for_mega_moe` 的预算推导（[deep_gemm/mega/__init__.py:75-96](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/mega/__init__.py#L75-L96)）：

```text
① 对齐 token 数到 kLCMCandidateBlockM(=384)
② 算出 [num_min_ring_tokens, num_max_ring_tokens] = get_ring_limit_for_mega_moe(...)
③ 按性状选初始预算 budget：
     if num_max_tokens_per_rank >= 6144:   # 当作 prefill
         budget = align(768*1024, 384)      # ~8 GB（V4 Pro 配置内）
     else:                                  # 当作 decode
         budget = get_ring_limit_for_mega_moe(align(4096,384), 432//72=6, 6, 72)[1]  # ~18 GB
④ num_ring_tokens = clamp(budget, min, max)
```

其中 `get_ring_limit_for_mega_moe` 返回的两端来自 [csrc/apis/mega.hpp:22-28](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/mega.hpp#L22-L28)：

```cpp
return {
    get_num_wave_pool_tokens(num_ranks, num_topk, num_max_tokens_per_rank, 1, layout::kLCMCandidateBlockM),                   // min: 1 expert/wave
    get_num_wave_pool_tokens(num_ranks, num_topk, num_max_tokens_per_rank, num_experts_per_rank, layout::kLCMCandidateBlockM) // max: 全 expert/wave
};
```

而 `get_num_wave_pool_tokens` 的容量公式（[csrc/jit_kernels/heuristics/mega_moe.hpp:80-93](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/mega_moe.hpp#L80-L93)）：

\[
\text{pool} =
\begin{cases}
T_{\text{all}} & \text{if } E_{\text{wave}} = 1 \\
\min\bigl(T_{\text{all}}\cdot E_{\text{wave}},\ \text{align}(T_{\text{all}}\cdot K_{\text{topk}} + E_{\text{wave}}(B-1),\ B)\bigr) & \text{otherwise}
\end{cases}
\]

其中 \(T_{\text{all}}=\text{num\_ranks}\cdot\text{num\_max\_tokens\_per\_rank}\) 是「所有 rank 的 token 总和」，\(E_{\text{wave}}\) 是每 wave 处理的 expert 数，\(K_{\text{topk}}\) 是每个 token 选中的 expert 数，\(B=\text{kLCMCandidateBlockM}=384\) 是对齐粒度。直觉是：

- 1 expert/wave：只需容下所有 rank 的 token 一次（\(T_{\text{all}}\)）。
- 多 expert/wave：要么「每个 expert 都收下全部 token」（\(T_{\text{all}}\cdot E_{\text{wave}}\)），要么「按真实路由量 \(T_{\text{all}}\cdot K_{\text{topk}}\) 再给每个 expert 补一点对齐 padding」，取较小者。

`kLCMCandidateBlockM = 384` 是所有候选 `block_m`（`{8,16,32,64,96,128,192}`）的最小公倍数（[deep_gemm/include/deep_gemm/layout/mega_moe.cuh:10-14](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/layout/mega_moe.cuh#L10-L14)），用它能保证 token 数对任何 `block_m` 都整除，避免因 `block_m` 选型不同而需要重新分配 buffer。

最终选定的 `num_ring_tokens` 会被存进 `SymmBuffer`，并一路传到 C++ 的 `get_symm_buffer_size_for_mega_moe` 里参与 buffer 各分区的尺寸计算（[csrc/apis/mega.hpp:44-45](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/mega.hpp#L44-L45) 有断言校验它落在 `[min,max]` 且被 384 整除）。

#### 4.3.3 源码精读

Python 侧的预算选择：

- [deep_gemm/mega/__init__.py:76](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/mega/__init__.py#L76)：先对齐到 `get_token_alignment_for_mega_moe()`（即 `kLCMCandidateBlockM=384`）。
- [deep_gemm/mega/__init__.py:82-96](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/mega/__init__.py#L82-L96)：prefill 分支给 `768*1024`（约 8 GB，注释说明「在 V4 Pro 配置内」）；decode 分支用一组硬编码的「4K batch、6 topk、72 rank」参数查表取其上界（约 18 GB）；最后 `max(min)` 再 `min(max)` 夹到合法区间。

C++ 侧 token 对齐常量：

- [csrc/apis/mega.hpp:18-20](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/mega.hpp#L18-L20)：`get_token_alignment_for_mega_moe()` 返回 `layout::kLCMCandidateBlockM`。

buffer 各分区如何随 `num_ring_tokens` 变化：L1/L2 的激活与 SF 用 ring 容量（`num_ring_tokens` / `num_sf_ring_tokens`），而输入区（`x` 等）与 combine 区用「最大接收量」（`num_max_tokens_per_rank`）。看 [csrc/apis/mega.hpp:89-106](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/mega.hpp#L89-L106)：

```cpp
const auto l1_token_buffer = layout::Buffer(input_token_layout, 1, num_ring_tokens, ...);
const auto l1_sf_buffer    = layout::Buffer(input_sf_layout, 1, num_sf_ring_tokens, ...);
const auto l2_token_buffer = layout::Buffer(intermediate_token_layout, 1, num_ring_tokens, ...);
```

其中 `num_sf_ring_tokens` 是「在所有候选 `block_m` 下取最大」的 SF 池容量（[csrc/apis/mega.hpp:80-87](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/mega.hpp#L80-L87)），公式见 [deep_gemm/include/deep_gemm/layout/mega_moe.cuh:28-31](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/layout/mega_moe.cuh#L28-L31)：`get_num_sf_ring_tokens = (num_ring_tokens / block_m) * align(block_m, 128)`，因为 SF 按 128 对齐打包。

最后，整个 buffer 的字节起点是 `Workspace`（元数据/屏障区，[csrc/apis/mega.hpp:52-54](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/mega.hpp#L52-L54) 调用 [deep_gemm/include/deep_gemm/layout/mega_moe.cuh:40-195](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/layout/mega_moe.cuh#L40-L195) 的 `Workspace` 构造），随后 `input_token_buffer` 以 `workspace.get_end_ptr()` 为起点往后排（[csrc/apis/mega.hpp:66-69](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/mega.hpp#L66-L69)）。Workspace 里存的是 grid_sync 计数器、NVLink 屏障信号、每 expert 的 send/recv 计数、ring 的 full/empty 计数等——这些是 mega-kernel 内部多 rank 协作的「控制平面」（u8-l4 详讲）。

#### 4.3.4 代码实践

**实践目标**：手算一个具体配置下的 ring 预算边界，理解 `get_ring_limit_for_mega_moe` 的输出。

**操作步骤**：

1. 取一组小参数：`num_ranks=8, num_max_tokens_per_rank=4096, num_topk=6, num_experts=64`（故 `num_experts_per_rank=8`），`block_m=384`。
2. 套用 `get_num_wave_pool_tokens` 公式（[csrc/jit_kernels/heuristics/mega_moe.hpp:80-93](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/mega_moe.hpp#L80-L93)）：
   - \(T_{\text{all}} = 8 \times 4096 = 32768\)。
   - min（`E_wave=1`）：\(= 32768\)。
   - max（`E_wave=8`）：\(\min(32768\times 8,\ \text{align}(32768\times 6 + 8\times 383,\ 384))\)。
3. 手算 max 的第二项：\(32768\times 6 = 196608\)，\(8\times 383=3064\)，和为 \(199672\)，`align(_, 384)` = \(\lceil 199672/384\rceil \times 384 = 520 \times 384 = 199680\)。于是 max = \(\min(262144,\ 199680) = 199680\)。
4. 对照代码确认：`get_ring_limit_for_mega_moe` 返回 `(32768, 199680)`，分别对应「每 wave 1 个 expert」和「每 wave 全部 8 个 expert」。

**需要观察的现象**：min 与 max 之间相差约 6 倍——这正解释了为什么 `get_symm_buffer_for_mega_moe` 要在中间挑一个「性状相关」的预算，而不是无脑取 max。

**预期结果**：手算 `(min, max) = (32768, 199680)`。若用本机（需 SM100 + 多进程）打印 `_C.get_ring_limit_for_mega_moe(4096, 8, 6, 8)`，应得到相同二元组；否则「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么对齐粒度选 `kLCMCandidateBlockM=384`，而不是直接选某个 `block_m`（比如 128）？

**参考答案**：`block_m` 是运行时启发式根据 token-per-expert 选的（候选集 `{8,16,32,64,96,128,192}`，见 [deep_gemm/include/deep_gemm/layout/mega_moe.cuh:10-11](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/layout/mega_moe.cuh#L10-L11)），但 buffer 是一次性分配、跨多次调用复用的。384 是全部候选的最小公倍数，对任何 `block_m` 都整除，于是「换 `block_m` 不必换 buffer」。若用 128，遇到 `block_m=192` 就会因不整除而崩溃。

**练习 2**：decode 分支为何用 `get_ring_limit_for_mega_moe(align(4096,384), 6, 6, 72)[1]` 这样一组「看似与实际配置无关」的硬编码参数来查表？

**参考答案**：这组参数（4K batch、6 topk、72 rank）是「EP64/EP72、4K 解码」这一典型高负载的画像，取它的上界 `[1]` 作为预算，意图是「让 wave 启发式在这种 decode 场景能选到尽可能多的 expert-per-wave」，从而提升 L2 复用与吞吐。注释明确指出该预算约 18 GB（[deep_gemm/mega/__init__.py:89-94](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/mega/__init__.py#L89-L94)）。最后仍会被实际配置的 `[min,max]` 夹住，保证不会越界。

---

## 5. 综合实践

把三块知识串起来：**为一次虚拟的 Mega MoE 调用，走完「概念 → 申请对称内存 → 看懂分区」的完整路径**。

**背景**：EP8、`num_experts=64`、`num_max_tokens_per_rank=4096`、`num_topk=6`、`hidden=7168`、`intermediate_hidden=3072`、`mma_type='fp8xfp4'`。

**任务**：

1. **判定性状与预算**（用 4.3 的结论）：`num_max_tokens_per_rank=4096 < 6144`，故走 decode 分支。请说明此时 `num_ring_tokens` 的初始预算来源、以及它最终会被哪两个值夹住。
2. **推演 SymmBuffer 的分配**（用 4.2 的结论）：因为是 EP8（`group.size()>1`），写出 `__init__` 会走哪条分配器分支、handle 是什么类型、`buffer_ptrs` 的长度为何等于 8。
3. **对照分区**（用 4.3.3 的结论）：在 [csrc/apis/mega.hpp:66-111](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/mega.hpp#L66-L111) 中，指出哪些 buffer 用 `num_max_tokens_per_rank`（输入/combine 区，与实际收到 token 量挂钩）、哪些用 `num_ring_tokens`（L1/L2 计算区，可被 ring 复用）。
4. **接口约束**（用 4.1 的结论）：写出调用 `fp8_fp4_mega_moe` 前，必须先把哪 4 个张量拷进 buffer 的哪 4 个视图（参考 [tests/test_mega_moe.py:104-121](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/test_mega_moe.py#L104-L121)）。

**预期产出**：一份不超过半页的「Mini Runbook」，含上述 4 点的简答。若本机具备 SM100 + PyTorch ≥ 2.9 + 8 卡，可进一步用 `torch.multiprocessing.spawn` 跑 `tests/test_mega_moe.py`（[tests/test_mega_moe.py:309-312](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/test_mega_moe.py#L309-L312)）验证 buffer 字节数与打印（`Buffer: ... GiB`，[tests/test_mega_moe.py:129](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/test_mega_moe.py#L129)）；否则标注「待本地验证」。

---

## 6. 本讲小结

- **Mega MoE** 把 EP dispatch + Linear1(FP8×FP4) + SwiGLU + Linear2(FP8×FP4) + EP combine 融合成**单个 mega-kernel**，让 NVLink 通信与 tensor core 计算重叠；仅支持 SM100，SM90 直接不可达。
- 融合的硬前提是**多进程 + 对称内存**：每个 rank 各开一块等大 buffer，经 rendezvous 互相登记地址，使 kernel 能用 `sym_buffer_ptrs` 直接读写远端显存。
- **`SymmBuffer`** 在 `group.size()==1` 时用 `torch.empty` + 本地 `data_ptr`；`>1` 时用 `symm_mem.empty` + `rendezvous`，两条分支统一暴露 `handle.buffer_ptrs`（每 rank 一个地址）。
- **ring token 预算**在 `get_ring_limit_for_mega_moe` 给出的 `[min, max]` 区间内挑选：min=1 expert/wave、max=全 expert/wave，由 `get_num_wave_pool_tokens` 公式算出；prefill 给约 8 GB、decode 给约 18 GB，最后夹到合法区间。
- 所有 token 数对齐到 `kLCMCandidateBlockM=384`（候选 `block_m` 的最小公倍数），保证换 `block_m` 不必换 buffer。
- 接口约束：**输入必须先拷进 buffer**（`x`/`x_sf`/`topk_idx`/`topk_weights`），输出 `y` 落在普通显存——因为 mega-kernel 的 dispatch pull 从对称内存的固定偏移读 token。

---

## 7. 下一步学习建议

- **u8-l2 Mega MoE 的权重变换**：本讲只说了「权重需经 `transform_weights_for_mega_moe` 重排」，下一讲深入 `_interleave_weights`（gate/up 交错）与 `_transpose_sf_for_utccp`（SF 转置）的细节。
- **u8-l3 Mega MoE 调度器与 wave 调度**：本讲反复提到「wave / expert-per-wave」，下一讲打开 `scheduler/mega_moe.cuh` 的 `MegaMoEScheduler` 状态机，看 `num_experts_per_wave` 是如何在 ring 容量约束下被选定的。
- **u8-l4 融合 mega 内核与通信重叠**：进入 `sm100_fp8_fp4_mega_moe.cuh` 内部，看 dispatch pull / 计算 / combine push 如何在同一个 kernel 里流水线化，以及 `Workspace` 里的 grid_sync / NVLink 屏障如何协调多 rank。

若想先补基础，可回看 u6-l3（cluster/grid 同步的 PTX 封装，grid_sync 在本讲的 `Workspace` 里被实际使用）与 u6-l2（UMMA/tcgen05，mega-kernel 的计算指令基础）。
