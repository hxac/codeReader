# GEMM 编译与计划缓存

## 1. 本讲目标

QuACK 的 GEMM 是整个项目最庞大、最复杂的部分。一次 `gemm` 调用背后，既有「按目标 GPU 架构挑选内核类」的分发逻辑，也有「把 CuTe-DSL 内核编译成机器码」的昂贵步骤，还要处理铺天盖地的可选参数（bias、alpha/beta、量化、变长序列、split-K……）。如果每次调用都重新走一遍这些流程，延迟会高到无法接受。

`quack/gemm.py` 用一套「**编译 / 计划 / 启动**」三层分离 + 两级缓存的架构解决了这个问题。本讲学完后，你应该能够：

1. 说清楚 `_compile_gemm` 的编译签名如何按 SM（流多处理器）版本挑选内核类，以及它如何用「符号张量（fake tensor）」让一份编译产物复用于一族形状。
2. 看懂 `gemm` 公共入口如何用一个 metadata key 把「已校验过的调用」缓存成 `_GemmPlan` 计划对象，命中时直接跳过校验与编译查找。
3. 解释为什么要把 `_build_gemm_plan`（构建计划，昂贵、与数据无关）和 `run_gemm_plan`（启动计划，廉价、只换数据指针）拆成两个函数。

本讲只聚焦 `quack/gemm.py` 这一个文件的「主机侧调度骨架」，不深入各 SM 的设备侧内核实现（那是 u5 单元的内容）。

## 2. 前置知识

本讲承接 u1-l3（目录结构）和 u2-l6（`@cute_op` 与 `jit_cache`），假设你已经了解：

- **CuTe-DSL 编译产物是一个 `.o` 机器码对象**：Python 写的 `@cute.jit` / `@cute.kernel` 函数经 `cute.compile` 编译、`export_to_c` 导出成 `.o`，冷编译约 500 ms，热加载约 1 ms。
- **`jit_cache` 是两级缓存**：内存字典 + 磁盘 `.o` 文件；磁盘路径里的「源码指纹」会把整个 `quack` 包哈希进 key，做到「源码改即失效」；多进程下用每 key 一把 `flock` 文件锁串行化冷编译。
- **符号张量 / fake tensor**：编译期用 `cute.sym_int()` 表示的「未知维度」，让编译产物对一族形状（如任意 batch）通用，从而减少需要编译的 cubin 数量。
- **SM 版本驱动内核选类**：QuACK 同一算子按 `device_capacity[0]`（8/9/10/11/12）分发到不同实现类（`gemm_sm80/90/100/120.py`）。
- **GEMM 的标准数学**：\(D = \alpha \cdot (A @ B) + \beta \cdot C + \text{bias}\)，其中 A 是 `(M, K)`、B 是 `(K, N)`、D/C 是 `(M, N)`，bias 沿 N（rowvec）或 M（colvec）方向广播。

如果你对 `jit_cache` 的两级缓存还不熟，建议先回看 u2-l6 再读本讲——本讲把它当作已知的「最底层编译缓存」，在此之上再叠加一层「计划缓存」。

## 3. 本讲源码地图

本讲几乎全部围绕一个文件：

| 文件 | 作用 |
|------|------|
| [quack/gemm.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py) | GEMM 主机侧的「编译 + 计划缓存 + 启动」骨架，是本讲主角 |

但它依赖和调用了几个关键的外部工具，理解这些工具能帮你看懂 `gemm.py`：

| 文件 / 符号 | 作用 |
|------|------|
| [quack/gemm_tvm_ffi_utils.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_tvm_ffi_utils.py) 的 `tensor_key` / `scalar_mode` / `compile_gemm_kernel` / `launch_gemm` | 张量 metadata key 构造、标量编译模式、真正的 `cute.compile` 调用、真正的内核启动 |
| [quack/cache/jit.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/cache/jit.py) 的 `jit_cache` | 最底层的 `.o` 两级缓存装饰器（u2-l6 已讲） |
| [quack/gemm_default_epi.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_default_epi.py) | 默认线性 epilogue 的各类 SM 实现（`GemmDefaultSm80/90/100/120`），即 `_compile_gemm` 选出来的内核类 |

调用链上下游（不在本讲深入，但需要知道位置）：

```
quack/gemm_interface.py  的 gemm()  (公共高层 API, u4-l3 详讲)
        │  └─ _gemm_execute()
        │        ├─ 热路径: run_gemm_plan(dispatch_plan, ...)   ← 持有计划直接启动
        │        └─ 冷路径: gemm_dispatch(...)  == quack.gemm.gemm()
        ▼
quack/gemm.py            ← 本讲主角
   gemm() ──key──> _gemm_plan_cache ──> _build_gemm_plan() ──> _compile_gemm()
                                                          │                │
                                                  构造 _GemmPlan     @jit_cache ──> .o 磁盘缓存
        └─ run_gemm_plan(plan, ...)   只塞每次调用的数据指针/标量
```

注意：上游 `gemm_interface.py` 的 `gemm()` 自己也有一层 `_gemm_iface_plan_cache`（接口层计划缓存），它捕获了 `quack.gemm.gemm()` 返回的 `dispatch_plan`，从而在热路径上连 `quack.gemm.gemm()` 的 key 都可以跳过。本讲聚焦 `quack.gemm.gemm()` 内部的那一层，但会顺带点明这种「层层缓存」的设计。

## 4. 核心概念与源码讲解

### 4.1 `_compile_gemm`：编译签名与 SM 选类

#### 4.1.1 概念说明

`_compile_gemm` 是真正触发 `cute.compile` 的函数。它的职责是：

1. **按目标 GPU 的 SM 版本挑一个内核类**（Hopper 选 `GemmDefaultSm90`，Blackwell 数据中心选 `GemmDefaultSm100`，等等）。
2. **构造一组「符号张量」**（fake tensor），它们只描述 dtype、主序（major）、哪些维度是符号维，不携带真实数据。
3. 把内核类 + 符号张量 + epilogue 参数交给 `compile_gemm_kernel`，由后者调用 `cute.compile` 产出机器码。

为什么用符号张量？因为编译是按「签名」特化的：dtype、主序、tile 形状、cluster 形状、各种开关不同，就需要不同的 cubin。但**具体的 M/N/K/batch 数值不需要进编译签名**——batch 用 `sym_int()` 表示，于是 `(1, M, K)` 和 `(4, M, K)` 共用同一份 cubin。这正是 u2-l6 讲过的「符号维度让产物对一族形状复用」。

`_compile_gemm` 顶部被 `@jit_cache` 装饰，意味着它的返回值（编译产物）会被 u2-l6 的两级缓存持久化到磁盘 `.o`。

#### 4.1.2 核心流程

```text
_compile_gemm(a_dtype, b_dtype, ..., device_capacity, ..., 大量标志位)
  │
  ├─ 1. SM 选类：sm_to_cls[device_capacity[0]]  →  GemmCls
  │        8 → GemmDefaultSm80,  9 → GemmDefaultSm90,
  │        10/11 → GemmDefaultSm100,  12 → GemmDefaultSm120
  │
  ├─ 2. make_fake_gemm_tensors(...)  →  mA, mB, mD, mC, m, n, k, l
  │      （batch / M / K 用 sym_int，主序由 a_major/b_major/... 决定）
  │
  ├─ 3. 构造 epi_args（EpilogueArguments）
  │      alpha/beta/sr_seed 用 fake_scalar(mode) 表示；
  │      rowvec/colvec、split_k 信号量与 workspace、SFD 都用符号张量
  │
  └─ 4. compile_gemm_kernel(GemmCls, ...)  →  cute.compile(...)  →  compiled_fn
         （@jit_cache 在外层把这个 compiled_fn 缓存成 .o）
```

#### 4.1.3 源码精读

**SM 选类**是整个编译流程的总开关。`device_capacity[0]` 是 SM 大版本号（8/9/10/11/12），用一个字典直接映射到对应的默认 epilogue 内核类：

[quack/gemm.py:93-100](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L93-L100) —— 按 SM 版本分发到不同内核类。注意 10 和 11 都映射到 `GemmDefaultSm100`（SM100/SM110 同属 Blackwell 数据中心，2-CTA tcgen05 MMA 路径相同）。

`@jit_cache` 装饰器在函数定义的正上方，它捕获的是**整个调用参数元组**作为 key：

[quack/gemm.py:48-49](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L48-L49) —— `@jit_cache` 把 `_compile_gemm` 变成一个「编译缓存函数」。它的 key 就是下面这一长串参数（dtype、主序、tile/cluster 形状、各种 bool 标志、device_capacity、各 mode……），**不含具体 M/N/K 数值**。

**符号张量构造**由 `make_fake_gemm_tensors` 完成，它返回 fake 的 mA/mB/mD/mC 以及符号维 m/n/k/l：

[quack/gemm.py:101-117](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L101-L117) —— 构造编译用的符号张量。`varlen_m`/`varlen_k`/`gather_A`/`batched`/`b_kn` 这些标志决定每个张量的逻辑形状与主序，但数值维度都是 `sym_int`。

epilogue 标量（alpha/beta）用一个小辅助函数 `fake_scalar` 把「编译模式」翻译成 fake 值——`mode 0`（缺省）返回 `None`，`mode 1`（主机常量）返回该 dtype 的字面量，`mode 2`（设备指针）返回一个 gmem 指针：

[quack/gemm.py:125-175](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L125-L175) —— 构造 `epi_args`（`EpilogueArguments`）。注意 split-K 的信号量和 partials workspace 也都是四维符号张量；SFD（量化输出的 scale factor）按是否 varlen 决定 batch 维。

最后交给 `compile_gemm_kernel`，后者实例化内核类并调用 `cute.compile`：

[quack/gemm.py:194-222](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L194-L222) —— 调用 `compile_gemm_kernel` 产出 `compiled_fn`。真正的 `cute.compile` 在 [quack/gemm_tvm_ffi_utils.py:748](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_tvm_ffi_utils.py#L748)（实例化 `GemmCls(...)`）与 [quack/gemm_tvm_ffi_utils.py:771](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_tvm_ffi_utils.py#L771)（`cute.compile(...)`）处。

> **关键认识**：`_compile_gemm` 的 key 是「签名级」的（dtype + 主序 + tile/cluster + 标志 + mode），**不是「形状级」的**。两个形状不同但签名相同的调用会命中同一个编译产物。这是「编译去重」之所以能在大量不同形状下保持 cubin 数量可控的根本原因。

#### 4.1.4 代码实践

**实践目标**：理解 SM 选类是纯查表，并看清编译 key 不含具体形状。

**操作步骤（源码阅读型，无需 GPU）**：

1. 打开 [quack/gemm.py:93-100](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L93-L100)，确认 `device_capacity[0]` 的取值集合是 `{8,9,10,11,12}`。
2. 回到 `_compile_gemm` 的形参列表 [quack/gemm.py:49-92](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L49-L92)，逐个数：哪些参数是「dtype/主序」、哪些是「tile/cluster 形状」、哪些是「bool 标志」、哪些是「mode（int）」。
3. 问自己：**这一长串参数里，有没有任何一个会随「M=1024 还是 M=2048」而变化？** 答案是没有——形状只通过符号张量间接进入编译，不进 key。

**需要观察的现象 / 预期结果**：你会确认「编译缓存 key = 签名」，与「形状」解耦。这意味着同一个签名下，无论跑多少种 M/N，`.o` 缓存只有一份；这也解释了为什么 `@jit_cache` 的磁盘目录能保持紧凑。

> 待本地验证（需要 GPU）：在两次 `gemm` 调用之间改变 M（如 512→1024，其余相同），观察 `~/.cache` 下 quack 的 `.o` 文件数量是否**不增加**；若改 dtype 或 tile 形状，则 `.o` 数量增加。

#### 4.1.5 小练习与答案

**练习 1**：为什么 SM100 和 SM110 都映射到 `GemmDefaultSm100`，而不是各给一个类？

**参考答案**：SM100（B200/B300）和 SM110 同属 Blackwell 数据中心架构，关键的 tcgen05 MMA、2-CTA 模式、TMEM 累加器路径完全一致；`device_capacity` 里的小版本号（minor）已经能区分两者的细微差异（如 `get_compile_target_capacity`），不必在「默认 epilogue 内核类」这一层再拆，避免无谓的代码复制。

**练习 2**：`_compile_gemm` 被调用时，`alpha` 是 1.0 还是 2.0 会影响编译产物吗？

**参考答案**：**不影响具体的数值**，但会影响编译产物。传进来的不是 `alpha` 本身，而是 `alpha_mode`（0/1/2，见 4.2.3）。`alpha=1.0` 对应 `mode 0`（epilogue 把 alpha 编译掉，不占参数槽），`alpha=2.0` 对应 `mode 1`（编译进一个接受主机常量的 epilogue）。两个 mode 编译出**不同的 cubin**，所以它们是不同的编译 key——但 `alpha=2.0` 和 `alpha=3.0`（同为 mode 1）共用同一份 cubin。

---

### 4.2 `gemm` 入口与 metadata key 缓存

#### 4.2.1 概念说明

`_compile_gemm` 解决了「编译产物复用」，但每次 `gemm` 调用如果都要走「校验断言 → 推导主序/dtype → 查编译缓存 → 构造静态参数模板」这一串，仍然有不可忽视的主机开销。而且，**很多调用的 metadata（形状、步长、dtype、各种开关）是完全相同的**——典型场景是推理循环里反复对同样形状的张量做矩阵乘。

于是 `gemm()` 在编译缓存之上又加了一层**计划缓存** `_gemm_plan_cache`：把「同一个 metadata 已经校验并构建好的启动计划」缓存成一个 `_GemmPlan` 对象。命中时直接拿计划去启动，跳过全部重复工作。

这层缓存与编译缓存的关键区别：

| | `_gemm_plan_cache`（计划缓存） | `@jit_cache`（编译缓存） |
|---|---|---|
| **key 粒度** | 形状 + 步长 + dtype + 全部标志 + mode（**含具体形状**） | 签名（dtype + 主序 + tile/cluster + 标志 + mode，**不含形状**） |
| **存储** | 进程内字典，进程结束即失效 | 内存字典 + 磁盘 `.o`，跨进程跨次运行复用 |
| **命中后省掉什么** | 校验、主序推导、编译查找、静态模板构造 | 真正的 `cute.compile`（约 500 ms） |
| **典型开销** | 命中约微秒级 | 冷约 500 ms，热（磁盘加载）约 1 ms |

「形状进计划 key、不进编译 key」正是两层去重能各司其职的精髓：很多**不同形状**的计划条目，向下汇聚到**同一份**编译产物。

#### 4.2.2 核心流程

```text
gemm(A, B, D, C, tile_M, tile_N, cluster_M, cluster_N, ..., 大量参数)
  │
  ├─ 1. 把 alpha/beta/sr_seed 归一化成 mode（scalar_mode）
  ├─ 2. 构造 metadata key（一个很长的 tuple）
  │        每个 Tensor → tensor_key(t) = (dtype, shape, stride)
  │        每个 Optional → 是否非 None
  │        标量 → mode；布尔/枚举 → 原值
  ├─ 3. plan = _gemm_plan_cache.get(key)
  │      ├─ 命中：直接用旧 plan
  │      └─ 未命中：plan = _build_gemm_plan(...)；存入 _gemm_plan_cache[key]
  ├─ 4. run_gemm_plan(plan, A, B, D, C, ...)   ← 只传每次调用的真实数据
  └─ 5. return plan   （上游接口层会捕获它做更高一层缓存）
```

#### 4.2.3 源码精读

**计划对象 `_GemmPlan`** 是一个 `NamedTuple`，字段全是「与具体数据指针无关」的不可变值：

[quack/gemm.py:225-255](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L225-L255) —— `_GemmPlan` 的定义。注意 `compiled_fn`（编译产物）、`epi_static`/`scheduler_static`（无 per-call 值时可复用的静态模板）、tile/cluster 尺寸、split-K 配置都在里面，而**没有任何 Tensor 数据指针**。docstring 明说「everything here is immutable, so reusing a plan across calls is safe」。

**计划缓存本身**就是一个普通字典：

[quack/gemm.py:258-261](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L258-L261) —— `_gemm_plan_cache: dict[tuple, _GemmPlan]`。注释点明了它的去重哲学：「按 (shape, stride, dtype, flag) 组合增长，真正的昂贵编译在下一层 `@jit_cache` 去重」。

**入口函数 `gemm`** 的签名极长（30+ 参数），覆盖了 bias、alpha/beta、变长序列、量化、split-K 等所有可选项：

[quack/gemm.py:378-443](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L378-L443) —— `gemm` 公共入口签名。注意它返回 `_GemmPlan`（而非 `Tensor`），这正是为了让上游能捕获并复用计划。

**标量归一化**用 `scalar_mode` 把 alpha/beta 等「三种存在形式」编码成编译期 mode：

[quack/gemm_tvm_ffi_utils.py:434-438](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_tvm_ffi_utils.py#L434-L438) —— `scalar_mode`：0 = 缺省（中性值，对应的 epilogue op 会被编译掉）、1 = 主机常量、2 = 设备指针。三种 mode 编译出不同 cubin，所以它们必须进 key。`gemm()` 里的调用见 [quack/gemm.py:444-448](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L444-L448)（注意 `sr_seed_mode` 的推导略不同：张量→2，`RS` 舍入→1，否则→0）。

**metadata key 构造**是本模块的核心。每个张量经 `tensor_key` 压成 `(dtype, shape, stride)`：

[quack/gemm_tvm_ffi_utils.py:428-431](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_tvm_ffi_utils.py#L428-L431) —— `tensor_key`：把一个 Tensor 压成「除数据指针外的全部 metadata」。`gemm()` 把 A/B/D/C/SFA/SFB/各 bias/各 cu_seqlens 全部这样压平，再拼上所有 bool 标志、mode、tile/cluster 尺寸，组成最终 key：

[quack/gemm.py:459-501](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L459-L501) —— 完整的 `key` 元组。注释强调「key 捕获了下方 plan build 读取的**每一个**输入（形状和步长已经隐含主序、校验断言、fp4/SF 形状检查），所以一次命中 = 一次已校验调用的精确重放，只是换了数据指针」。

**缓存查找与填充**是标准的「get-or-build」模式：

[quack/gemm.py:502-546](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L502-L546) —— 未命中则调 `_build_gemm_plan` 构建并存入字典；命中则直接复用。注意 `ag_args`（AllGather）在构造 key 前会先做一次几何校验 [quack/gemm.py:454-458](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L454-L458)，让「坏几何」在昂贵的编译之前就失败。

最后**启动并返回计划**：

[quack/gemm.py:547-570](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L547-L570) —— `run_gemm_plan(plan, ...)` 只接收每次调用的真实张量与标量；`return plan` 把计划交还上层（`gemm_interface.py` 会把它存进自己的接口层缓存）。

> **关键认识**：key 里**只有 metadata，没有数据指针**。两个数据完全不同但形状/步长/dtype/标志相同的调用会命中同一个 plan——这正是「同形状反复调用」能享受微秒级开销的原因。

#### 4.2.4 代码实践

**实践目标**：列出 `_gemm_plan_cache` 的 key 到底包含哪些字段，并验证「同形状不同数据」会命中。

**操作步骤（源码阅读型）**：

1. 打开 [quack/gemm.py:459-501](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L459-L501)，按类别把 key 元组的字段分组填进下表：

   | 类别 | 字段 |
   |------|------|
   | 张量 metadata（经 `tensor_key`） | A, B, D, C, SFA, SFB, rowvec_bias, colvec_bias, cu_seqlens_m, cu_seqlens_k |
   | 「是否存在」布尔 | A_idx 是否非 None、batch_idx_permute、tile_count_semaphore、ag_args 是否非 None |
   | 设备 / 尺寸 | A.device、tile_M、tile_N、tile_K、cluster_M、cluster_N、cluster_K |
   | 调度 / 行为开关 | pingpong、persistent、is_dynamic_persistent、max_swizzle_size、add_to_output、rounding_mode、use_tma_gather、num_warps、b_kn |
   | 标量 mode | alpha_mode、beta_mode、sr_seed_mode、sfd_norm_const 的 mode |
   | split-K / 量化 | split_k、split_k_mode、bs_format_a、bs_format_b、SFD、SFDCol |
   | concat | concat_key（排序后的 tuple） |

2. 对照 `_build_gemm_plan`（见 4.3）实际读取的输入，确认这个 key 集合「不漏不冗余」。

**需要观察的现象 / 预期结果**：你能解释为什么 `tensor_key` 既要 `shape` 又要 `stride`——因为步长决定了主序（k-major 还是 n-major），而主序进编译签名，影响 cubin；只看 shape 看不出主序。

> 待本地验证（需要 GPU）：连续两次调用 `quack.gemm.gemm(...)`，仅数据指针不同、其余相同；在第二次调用前打补丁给 `_gemm_plan_cache.get` 加计数，确认第二次命中（dict 长度不变）。

#### 4.2.5 小练习与答案

**练习 1**：假设你把 `alpha` 从 `1.0` 改成 `2.0`（其余不变），`_gemm_plan_cache` 会命中吗？`@jit_cache`（编译缓存）会命中吗？

**参考答案**：**计划缓存不命中**——`alpha_mode` 从 0 变成 1，key 改变，会构建一个新的 plan。但**编译缓存大概率命中**：新 plan 走 `_compile_gemm` 时，编译 key 里 `alpha_mode=1` 可能之前已经为别的形状编译过（因为编译 key 不含形状），于是直接加载已有的 `.o`。这正体现了「计划 key 比编译 key 细」。

**练习 2**：为什么 key 里要单独放 `A.device`，而不是假设它和某个张量的 device 一致？

**参考答案**：`device_capacity`（SM 版本）是从 `A.device` 推导的，它决定选哪个内核类、决定 cluster/TMA 等硬件特性开关，是编译签名的核心组成部分。把 `A.device` 显式放进 key，是为了让「同一形状在不同 GPU 上」生成不同的 plan 与 cubin，避免跨架构误用。

---

### 4.3 `_build_gemm_plan` / `run_gemm_plan`：构建与启动的分离

#### 4.3.1 概念说明

把构建（build）和启动（run）拆成两个函数，是这套架构最值得品味的设计。两者的分工：

- **`_build_gemm_plan`（构建）**：做所有「与数据指针无关、但与 metadata 强相关」的昂贵工作——参数校验（大量 assert）、主序/dtype 推导、blockscaled 格式解析、SM 选类、**编译**、静态参数模板构造。它只在计划缓存未命中时调用一次，产物是 `_GemmPlan`。
- **`run_gemm_plan`（启动）**：做所有「每次调用都必须重做、且依赖真实数据」的廉价工作——分配 split-K 的 per-call 缓冲、把每次调用的标量（alpha/beta/sr_seed）填进 epilogue 参数、把每次调用的张量指针塞进编译产物并启动。它每次调用都跑，但极轻量。

为什么这样拆？因为「校验 + 编译」是 **metadata-only** 的（同样的形状永远得到同样的结论），没必要每次重复；而「真实数据指针 / per-call 标量的具体数值」是 **per-call** 的，必须每次处理。把两者塞进同一个函数，会让缓存命中时也无法跳过 per-call 工作；拆开后，缓存命中路径只需调 `run_gemm_plan`。

此外还有一个优化：当 epilogue 完全没有 per-call 值时（alpha/beta/sr_seed 全缺省、无 bias、无 SFD、非 SERIAL/PARALLEL split-K），整个 `EpilogueArguments` NamedTuple 可以**构建一次、永久复用**，存进 `plan.epi_static`。`run_gemm_plan` 检测到它非 None 就直接拿来用，连 NamedTuple 都不必重建。调度器参数同理（`plan.scheduler_static`）。

#### 4.3.2 核心流程

`_build_gemm_plan`（仅未命中时跑）：

```text
_build_gemm_plan(A, B, D, C, *, tile/cluster, ..., 全部 mode)
  │
  ├─ 1. 一长串校验 assert（varlen 互斥、gather_A 约束、split-K 限制、
  │       blockscaled 约束、量化输出 SFD/SFDCol 形状、2D/3D 操作数一致性…）
  ├─ 2. device_capacity = get_device_capacity(A.device)；断言 SM∈{8,9,10,11,12}
  ├─ 3. blockscaled：解析 format、推导 sf_dtype/sf_vec_size、校验 SF
  ├─ 4. SM8x 的 add_to_output 降级（C = D）、SEPARATE split-K 把 epilogue 路由到归约核
  ├─ 5. get_majors / get_dtypes 推导主序与 dtype
  ├─ 6. compiled_fn = _compile_gemm(...)        ← 真正的编译（@jit_cache 兜底）
  ├─ 7. max_active_clusters = get_max_active_clusters(...)
  ├─ 8. 构造 epi_static / scheduler_static（若无 per-call 值）
  └─ 9. return _GemmPlan(compiled_fn, ..., tile_M, tile_N, cluster_M, cluster_N, ...)
```

`run_gemm_plan`（每次调用都跑）：

```text
run_gemm_plan(plan, A, B, D, C, *, rowvec_bias, colvec_bias, alpha, beta, sr_seed, ...)
  │
  ├─ 1. split-K 的 per-call 缓冲（SEPARATE: workspace；SERIAL/PARALLEL: semaphore+workspace）
  │       —— 这些是每次调用新建的分配，绝不进缓存计划
  ├─ 2. epi_args = plan.epi_static  或  按 mode 重新填入本次的 alpha/beta/sr_seed/SFD
  ├─ 3. scheduler_args = plan_scheduler_args(plan, ...)   （静态模板或重建）
  ├─ 4. varlen_args = make_varlen_args(cu_seqlens, A_idx)
  ├─ 5. launch_gemm(plan, A, B, D_gemm, C_gemm, epi_args, scheduler_args, varlen_args, SFA, SFB)
  └─ 6. 若 staged split-K：再跑 _reduce_staged_split_k(...) 归约核
```

#### 4.3.3 源码精读

**`_build_gemm_plan` 的校验段**非常长，因为 GEMM 的可选组合极多。先看 varlen/gather/AllGather 的互斥与前置条件：

[quack/gemm.py:714-729](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L714-L729) —— 典型校验：varlen_m 与 varlen_k 互斥、gather_A 要求 varlen 且 cluster_N=1、AllGather 要求 dense + persistent + split_k=1。这些 assert 是「同形状每次都得到同一结论」的典型——正是它们可以被 key 缓存掉的原因。

split-K 的校验把 mode 规范化、把不支持的组合（变长、随机舍入、blockscaled+SEPARATE）拦下：

[quack/gemm.py:730-744](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L730-L744) —— split-K 校验。注意第 736 行把 int 形式的 `split_k_mode` 规范化成 `SplitKMode` 枚举，这一步也属于「构建期一次性」工作。

blockscaled 与量化输出的校验是最复杂的部分（解析 format、推导 SF 向量长度、检查 `(32,4,4)` 内层 atom 的形状与步长）：

[quack/gemm.py:760-855](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L760-L855) —— blockscaled 与 SFD/SFDCol 的全部校验。这些断言完全由 metadata（形状、步长、dtype）决定，不依赖数据，因此适合放在构建期。

**主序/dtype 推导**与**编译调用**：

[quack/gemm.py:891-958](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L891-L958) —— `get_majors`/`get_dtypes` 推导主序与 dtype（`b_kn` 时单独重判 B 的主序），然后调用 `_compile_gemm`。注意 SEPARATE split-K 会在这里把 `d_dtype` 改成 `Float32`、把所有 mode 清零（因为 epilogue 被路由到归约核，GEMM 只写裸 f32 partials）[quack/gemm.py:898-910](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L898-L910)。

**静态模板构造**——这是构建/启动分离的「加速器」：

[quack/gemm.py:970-992](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L970-L992) —— 当 epilogue 无任何 per-call 值（alpha/beta/sr_seed 全 mode 0、无 rowvec/colvec、split_k=1 或 staged、无 SFD/SFDCol）时，构造一个全 `None` 的 `EpilogueArguments` 存进 `epi_static`；调度器同理存进 `scheduler_static`。这些模板进 plan 后，`run_gemm_plan` 命中即可零成本复用。

**`_GemmPlan` 的返回**把所有不可变结果打包：

[quack/gemm.py:994-1014](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L994-L1014) —— 返回 `_GemmPlan`。注意它带回了 `compiled_fn`、两个静态模板、`max_active_clusters`、`is_sm100_family`、split-K 配置、tile/cluster 尺寸——这些正是 `run_gemm_plan` 启动时需要的全部「非 per-call」信息。

现在看 **`run_gemm_plan`** 如何用这份计划。首先是 split-K 的 per-call 缓冲——它们必须每次新建，所以**绝不**进缓存计划：

[quack/gemm.py:604-627](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L604-L627) —— split-K 缓冲按模式分配：SEPARATE 用 `_staged_split_k_workspace` 造 f32 partials 工作区并改写 `D_gemm`；SERIAL/PARALLEL 用 `_split_k_buffers` 造 per-tile 信号量 + partials。注释明确「Split-K buffers are per-call allocations, never part of the cached plan」。

然后是 epilogue 参数的「静态优先，否则按本次标量重建」：

[quack/gemm.py:630-649](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L630-L649) —— `epi_args = plan.epi_static`；为 None 时才用 `scalar_arg` 把本次的 alpha/beta/sr_seed/sfd_norm_const 按 mode 填进新建的 `EpilogueArguments`。`scalar_arg` 见 [quack/gemm_tvm_ffi_utils.py:441-449](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_tvm_ffi_utils.py#L441-L449)（mode 0→None、1→dtype 字面量、2→数据指针）。

最后调度器参数与真实启动：

[quack/gemm.py:650-655](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L650-L655) —— `plan_scheduler_args`（静态优先）+ `make_varlen_args` + `launch_gemm`。`launch_gemm` 最终调用 `plan.compiled_fn(...)` 把真实张量指针送进编译产物（[quack/gemm_tvm_ffi_utils.py:497-505](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_tvm_ffi_utils.py#L497-L505)）。

> **关键认识**：构建期把「校验 + 编译 + 静态模板」一次性烘焙进不可变的 `_GemmPlan`；启动期只剩「per-call 缓冲分配 + 标量填充 + 一次 `compiled_fn` 调用」。这就是命中路径能做到微秒级的原因。

#### 4.3.4 代码实践

**实践目标**：亲手验证「构建与启动分离」带来的命中路径收益，并理解 per-call 工作的最小集合。

**操作步骤（源码阅读 + 本地验证）**：

1. 在 [quack/gemm.py:502](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L502) 处的逻辑里，区分两种路径：
   - 未命中：`_build_gemm_plan`（含校验 + `_compile_gemm`）+ `run_gemm_plan`。
   - 命中：**只**调 `run_gemm_plan`。
2. 列出 `run_gemm_plan` 在「无 split-K、epi_static 非空」最佳情况下实际做的事（参考 [quack/gemm.py:630-655](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L630-L655)）：跳过 split-K 分配 → 直接用 `plan.epi_static` → `plan_scheduler_args` 返回 `plan.scheduler_static` → `make_varlen_args`（无 varlen 时也很轻）→ `launch_gemm`。
3. 对比未命中路径要做的事：几十条 assert + 主序推导 + blockscaled 解析 + 一次编译查找。

**需要观察的现象 / 预期结果**：你会清楚地看到，命中路径省掉了**所有**「与数据无关的重复推理」，只保留了「必须依赖真实数据」的极少步骤。

> 待本地验证（需要 GPU）：写一个循环，对同一组形状连续调用 `quack.gemm.gemm(...)` 100 次。第一次会触发编译（数百 ms），后续 99 次应在毫秒以内（命中 `_gemm_plan_cache`，且 `compiled_fn` 已在内存）。若打开 `QUACK_CACHE_ENABLED=0` 重跑，编译缓存失效但计划缓存仍命中——可以借此分离两层缓存的贡献。

#### 4.3.5 小练习与答案

**练习 1**：为什么 split-K 的信号量和 partials workspace 不能存进 `_GemmPlan`，而必须每次在 `run_gemm_plan` 里新建？

**参考答案**：因为它们是**每次调用的临时分配**，存进不可变的计划会导致并发调用（例如多个 batch 同时跑）共享同一块缓冲而互相覆盖。计划只烘焙「与数据指针无关、跨调用不变」的东西；per-call 缓冲属于「每次调用都唯一」的资源，必须在启动期分配。docstring 在 [quack/gemm.py:264-267](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L264-L267) 明确了这一点。

**练习 2**：`epi_static` 在什么条件下非 None？它非 None 时 `run_gemm_plan` 省掉了什么？

**参考答案**：当 epilogue 没有任何 per-call 值时（alpha/beta/sr_seed 全是 mode 0、无 rowvec/colvec bias、split_k=1 或 staged split-K、无 SFD/SFDCol），`epi_static` 是一个全 `None` 的 `EpilogueArguments`（见 [quack/gemm.py:971-989](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L971-L989)）。非 None 时，`run_gemm_plan` 跳过「重建 NamedTuple + 多次 `scalar_arg`」[quack/gemm.py:630-632](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L630-L632)，直接复用静态模板。对于一个纯 `D = A @ B` 的 GEMM，这是常见情况，省下的开销虽小但在高频调用下可观。

**练习 3**：上游 `gemm_interface.py` 的 `_gemm_execute` 有一条「warm replay」分支会直接调 `run_gemm_plan(dispatch_plan, ...)`（[quack/gemm_interface.py:723-748](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_interface.py#L723-L748)）。这条分支能跳过 `quack.gemm.gemm()` 的 key 查找，为什么这样做是安全的？

**参考答案**：因为接口层计划缓存（`_gemm_iface_plan_cache`）的 key 已经「担保」了 metadata 与构建这个 `dispatch_plan` 时一致——而 `run_gemm_plan` 只依赖 metadata 一致性（它内部只用 per-call 数据 + 计划里的不可变字段）。所以只要接口层 key 命中，就能安全地绕过 `quack.gemm.gemm()` 的 key 与查找，直接启动。这正是「层层缓存、每层各自担保一致性」的设计收益。

## 5. 综合实践

把三个模块串起来，做一次「完整调用链跟踪」。

**任务**：模拟跟踪一次 `quack.gemm.gemm(A, B, D, C=None, tile_count_semaphore=None, tile_M=128, tile_N=128, cluster_M=2, cluster_N=1, ...)`（假设 SM100、bf16、无 bias、alpha=1.0、无 split-K、计划缓存为空）。

**步骤**：

1. **入口**（4.2）：进入 [quack/gemm.py:378](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L378)。`scalar_mode(1.0)=0`、`scalar_mode(1.0)=0`、`sr_seed_mode=0`。
2. **构造 key**（4.2）：在 [quack/gemm.py:459-501](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L459-L501) 处，A/B/D 各被压成 `(bfloat16, shape, stride)`，C/SFA/SFB/bias/cu_seqlens 全为 None，拼上 `tile_M=128, tile_N=128, cluster_M=2, cluster_N=1`、`persistent=True`、各 mode=0 等。
3. **未命中**（4.2）：`_gemm_plan_cache.get(key)` 为 None → 进入 `_build_gemm_plan`。
4. **构建**（4.3）：过校验（[quack/gemm.py:714-855](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L714-L855)），`device_capacity=(10,0)`，`get_majors/get_dtypes` 推出主序与 bf16，调 `_compile_gemm`。
5. **编译**（4.1）：`sm_to_cls[10] → GemmDefaultSm100`，`make_fake_gemm_tensors` 造符号张量，`compile_gemm_kernel` → `cute.compile`。`@jit_cache` 在磁盘 `.o` 上命中或落盘。
6. **静态模板**（4.3）：因 alpha/beta/sr_seed 全 mode 0、无 bias、split_k=1，[quack/gemm.py:971-989](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L971-L989) 构造出非空 `epi_static`；调度器也构造 `scheduler_static`。
7. **返回计划**：`_GemmPlan` 存入 `_gemm_plan_cache[key]`。
8. **启动**（4.3）：`run_gemm_plan` 检测 `split_k==1` 跳过缓冲分配，直接用 `plan.epi_static`，`launch_gemm` 调 `plan.compiled_fn(...)` 启动内核。

**预期结果**：你能画出这次调用的完整数据流，并指出「编译只发生一次（且可能直接从磁盘加载）、校验只跑一次、后续同形状调用只走第 1→3（命中）→8 步」。

**思考题**：如果第 2 次调用把 `alpha` 从 `1.0` 改成 `2.0`，跟踪链在哪一步分叉？分叉后是否仍可能命中编译缓存？（提示：见 4.2.5 练习 1）

## 6. 本讲小结

- **三层分离**：`quack/gemm.py` 把 GEMM 主机侧工作分成「编译（`_compile_gemm`）— 计划（`_build_gemm_plan`）— 启动（`run_gemm_plan`）」三层，各司其职。
- **SM 选类是查表**：`_compile_gemm` 用 `sm_to_cls[device_capacity[0]]` 在 `{8,9,10,11,12}` 间挑选内核类（10/11 同归 `GemmDefaultSm100`）。
- **编译 key 是签名级，不含形状**：编译产物用符号张量（`sym_int`）表示 batch/M/K，使一份 cubin 复用于一族形状；`@jit_cache` 把这个产物持久化成磁盘 `.o`。
- **计划 key 是形状级，含全部 metadata**：`_gemm_plan_cache` 用 `(dtype, shape, stride, flags, modes, ...)` 做更细的 key，缓存「已校验的启动计划」`_GemmPlan`，命中时跳过校验、主序推导、编译查找、静态模板构造。
- **两层去重各司其职**：很多不同形状的计划条目，向下汇聚到同一份编译产物——「形状进计划 key、不进编译 key」是这套设计能同时控制 cubin 数量与 per-call 开销的关键。
- **构建/启动拆分 + 静态模板**：构建期烘焙校验、编译、`epi_static`/`scheduler_static`；启动期只剩 per-call 缓冲分配、标量填充与一次 `compiled_fn` 调用，命中路径做到微秒级。

## 7. 下一步学习建议

本讲只讲了 GEMM 的「主机侧调度骨架」。要继续深入，建议：

1. **u4-l2（GemmConfig 配置空间）**：本讲的 `tile_M/tile_N/cluster_*` 是谁选出来的？去 `gemm_config.py` 看各 SM 的 autotune 配置空间、`default_config` 与 `SplitKMode` 三种模式。
2. **u4-l3（公共 GEMM API 表面）**：本讲的 `gemm()` 其实是「调度层」入口，用户真正调用的是 `gemm_interface.py` 的 `gemm/gemm_act/gemm_gated` 等；去看接口层计划缓存 `_gemm_iface_plan_cache` 如何与本章的 `_gemm_plan_cache` 叠加。
3. **u5-l1（GemmBase 共享主循环）**：本讲的 `compiled_fn` 启动后，设备侧内核长什么样？进入 `gemm_base.py` 看 mainloop 与 epilogue 驱动。
4. **u8-l2（.o JIT 缓存与异步编译池）**：本讲把 `@jit_cache` 当作黑盒，去 `cache/jit.py` 与 `cache/async_compile.py` 看两级缓存的文件锁、源码指纹、`--async-compile=N` 多 worker 池的完整实现。

> 阅读源码时，推荐带着「这次调用的 key 是什么？命中了哪一层缓存？省掉了什么？」这三个问题去对照 `quack/gemm.py`，能快速把本讲的知识内化。
