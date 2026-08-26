# u7-l2 分布式编程模型

## 1. 本讲目标

学完本讲，你应该能够：

1. 掌握 PyPTO 多 rank 编程的**三级函数模型**：`@pl.jit.host`（主机编排）→ `@pl.jit`（每设备编排）→ `@pl.jit.incore`（片上内核），以及 `device=r` 派发、`pld.world_size()` 与 `DistributedConfig` 的配合方式。
2. 理解**对称内存 + 信号**（symmetric-memory + signals）这一分布式核心模型：`DistributedTensor`、窗口缓冲（`alloc_window_buffer` / `window`）、通信域（comm domain）分别是什么、由哪个 Pass 物化。
3. 掌握四类**通信原语**：`pld.system.notify` / `wait`（信号同步）、`pld.tile.remote_load` / `remote_store`（Tile 级单边读写）、`pld.tensor.put` / `get`（Tensor 级推/拉），以及 `pld.tensor.barrier` 内建屏障背后的**自清零信用屏障协议**。
4. 了解 `docs/en/user/distributed/05-tutorials.md` 组织的**16 步教学阶梯**（本版刚扩展到 steps 08–11 的 all-reduce 三连 + reveal），能按阶梯自学并说出每步的教学目标。
5. 能独立运行并修改多 rank 协同的示例程序，写出「分块计算 → 屏障 → 交换结果」的两 rank 程序。

本讲承接 u7-l1（manual_scope 与任务图）——那一讲关注**单设备内**任务依赖图的塑造；本讲把视野扩到**跨设备（跨 rank）**：数据如何放进对端可见的窗口、执行顺序如何用信号对齐。下一讲 u7-l3 将深入阶梯里的 all-reduce 三种手写实现与内建算子的对照。

## 2. 前置知识

### 2.1 rank、world size 与 SPMD

多卡协同最经典的组织方式是 **SPMD**（Single Program, Multiple Data）：同一份内核代码在每张卡（每个 **rank**）上各跑一份，各自处理自己那份数据，必要时通信。**rank** 是参与的设备编号（0、1、2…），**world size**（P）是参与设备总数。u1-l4 的 hello world 是「一个 Tensor、一张卡」；本讲的 hello_rank 则是「一个世界张量 `[N_RANKS, …]`、每个 rank 认领一片」。

### 2.2 双边消息 vs 单边访问（RMA）

MPI 风格的 `send`/`recv` 是**双边**通信：收发双方都要调用配对的接口。PyPTO 走的是**单边**（one-sided / RMA，Remote Memory Access）路线：每个 rank 在自己的 NPU 上划出一块**窗口内存**（window buffer），各 rank 窗口的地址布局**对称**；之后一方可以直接 `remote_load`（读对端）或 `remote_store`/`put`（写对端），**对端不需要执行任何配合代码**。硬件上这由 HCCL 窗口与 TLOAD/TSTORE/TGET/TPUT 等指令支撑。

### 2.3 信号与信用屏障

单边读写带来一个新问题：我怎么知道对端**已经把数据写好了**？答案是**信号**（signal）——一块小的 INT32 窗口张量。写完数据的一方 `notify`（在对端的信号格子上 +1 或置值），需要数据的一方 `wait`（阻塞直到自己的信号格子达到阈值）。用它搭出的「所有人都到齐才放行」结构就是**屏障**（barrier）。u7-l1 讲过设备内任务用 TaskId 表达依赖；跨 rank 没有共享的 TaskId 世界，信号就是跨 rank 的「依赖边」。

### 2.4 与前面讲义的衔接

- **三级函数与调用链**：u3-l2 讲过函数种类；本讲的 `@pl.jit.host` / `@pl.jit` / `@pl.jit.incore` 是 `@pl.jit` 家族在分布式场景的三件套。
- **`pl.dynamic` 动态维度**：u2-l2 讲过 `pl.dynamic("NR")` 让维度不进缓存键；本讲第 4.5 节正是用它让「rank 数」运行期才定。
- **Pass 流水线**：u3-l5 讲过 47 个 Pass 的分组；本讲会出现其中三个分布式 Pass（40/41/42），它们排在流水线末段。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `examples/distributed/01_hello_rank.py` | 分布式 hello world：rank 身份、三级模型、`device=r` 派发 |
| `examples/distributed/04_barrier.py` | 手写 N-rank 屏障（notify/wait）+ 内建 `pld.tensor.barrier` 揭示 |
| `examples/distributed/05_remote_load_store.py` | Tile 级单边读写：`remote_load` / `remote_store` 两种方向的环形移位 |
| `examples/distributed/06_put_get.py` | Tensor 级点对点：`put`（推）/ `get`（拉） |
| `examples/distributed/07_dynamic_rank_count.py` | 动态 rank 数：`NR = pl.dynamic("NR")`，同一份源码跑任意 P |
| `examples/distributed/08~11_allreduce_*.py` | all-reduce 三种手写 + 内建揭示（u7-l3 的主角，本讲只指路） |
| `python/pypto/language/distributed/__init__.py` | `pld` 命名空间入口：布局说明、算子与枚举再导出 |
| `python/pypto/language/distributed/op/system_ops.py` | `world_size` / `get_comm_ctx` / `rank` / `nranks` / `notify` / `wait` / `defer_wait` |
| `python/pypto/language/distributed/op/tensor_ops.py` | `alloc_window_buffer` / `window` / `put` / `get` / `allreduce` / `barrier` 等 |
| `python/pypto/language/distributed/op/tile_ops.py` | `remote_load` / `remote_store` |
| `python/pypto/language/distributed/typing/distributed_tensor.py` | `DistributedTensor` 注解类型 |
| `docs/en/user/distributed/00-model.md` | 模型词汇表 + 2-rank allreduce 快速上手 |
| `docs/en/user/distributed/05-tutorials.md` | 16 步教学阶梯总目录（本讲 4.5 节的骨架） |
| `docs/en/dev/distributed_ops.md` | 15 个分布式算子的权威参考（N6 算子族） |
| `docs/en/dev/passes/41-materialize_comm_domain_scopes.md` | 通信域物化 Pass：窗口/派发如何变成 `WindowBuffer` 与 `CommDomainScopeStmt` |

## 4. 核心概念与源码讲解

### 4.1 三级编程模型与 rank 身份：hello_rank

#### 4.1.1 概念说明

一个 PyPTO 分布式程序由三层函数构成，**每层跑在一个处理器上**：

| 层 | 装饰器 | 跑在哪 | 职责 |
| --- | --- | --- | --- |
| HOST 编排 | `@pl.jit.host` | 主机 CPU（每进程一次，不上 NPU） | 分配窗口、组织 `for r` 循环、用 `device=r` 派发 |
| 每设备编排 | `@pl.jit` | 各设备的编排层 | 承上启下的包装：接收本 rank 参数，调用片上内核 |
| 片上内核 | `@pl.jit.incore` | 本 rank 设备的 AICore | 只做计算与通信，看不见 world |

两条铁律：**HOST 永远不直接派发 InCore 内核**——它派发的是 `@pl.jit` 包装，包装再不带 `device=` 地调内核；**`pld.world_size()` 只能在 HOST 层调用**，InCore 内核想知道自己是谁，必须从参数里的 `DistributedTensor` 反查通信上下文（见 4.3 节 `get_comm_ctx`/`rank`）。

#### 4.1.2 核心流程

hello_rank 的数据流（每个 rank 计算 `y[r] = x[r] + r`，输出的那一行本身就证明了它是谁算的）：

```text
main()（Python 进程，单个进程，无 mpirun）
  └─ hello_rank.compile(x, y, RunConfig(distributed_config=DistributedConfig(device_ids=[0,1])))
       └─ HOST: for r in pl.range(pld.world_size()):
             per_rank(x[r], y[r], r, device=r)      # 派发到第 r 台设备
                  └─ InCore: add_rank(x[r], y[r], r)  # AICore 上 y = x + r
```

运行时按 `device_ids` **从这同一个 Python 进程 fork 出每设备一个 worker 进程**——不需要外部多进程启动器。

#### 4.1.3 源码精读

片上内核——注意签名顺序，**张量参数在前、标量在后**（分布式 TaskArgs 打包要求 tensors-first / scalars-last，标量夹在张量中间会在运行时报 "cannot add tensor after scalar"）：

[examples/distributed/01_hello_rank.py:46-57](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/distributed/01_hello_rank.py#L46-L57) 定义 `add_rank`：`pl.load` 搬入 → `pl.cast` 把 rank 标量转 FP32 → `pl.add` → `pl.store` 写回 `pl.Out` 输出。这是 u1-l5 讲过的三段式，唯一的新东西是「标量 rank 是参数传进来的」。

[examples/distributed/01_hello_rank.py:60-67](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/distributed/01_hello_rank.py#L60-L67) 是中间的每设备包装 `per_rank`：一行转发。它存在的意义就是给 HOST 一个可挂 `device=r` 的派发目标。

[examples/distributed/01_hello_rank.py:70-77](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/distributed/01_hello_rank.py#L70-L77) 是 HOST 编排 `hello_rank`：`for r in pl.range(pld.world_size())` 枚举 rank，`per_rank(x[r], y[r], r, device=r)` 把第 r 片输入输出和 r 本身钉到第 r 台设备。`x[r]` 的下标把世界张量 `[N_RANKS, ROWS, COLS]` 降成内核要的 `[ROWS, COLS]`。

[examples/distributed/01_hello_rank.py:106-116](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/distributed/01_hello_rank.py#L106-L116) 是编译入口：`RunConfig(platform=…, distributed_config=DistributedConfig(device_ids=device_ids, num_sub_workers=0))`。`DistributedConfig` 声明哪些 NPU 设备参与；默认平台 `a2a3sim` 是模拟器，没有双卡也能跑。

`pld` 命名空间本身的结构（与 `pl` 完全同构）：[python/pypto/language/distributed/__init__.py:22-35](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/distributed/__init__.py#L22-L35) 说明它按 `pld.<分类>.<算子>` 三段式组织——`pld.system.*`（world_size / get_comm_ctx / rank / nranks）、`pld.tensor.*`（alloc_window_buffer / window / get / put）、`pld.tile.*`（remote_load / remote_store），并把 `NotifyOp` / `WaitCmp` / `AtomicType` / `ReduceOp` 四个枚举再导出到顶层，让用户直接写 `pld.NotifyOp.AtomicAdd`。

#### 4.1.4 代码实践

1. **实践目标**：跑通第一个多 rank 程序，确认「同一个内核、按 rank 参数化」。
2. **操作步骤**（在 u1-l2 搭好的环境中）：

   ```bash
   python examples/distributed/01_hello_rank.py                 # 默认 a2a3sim + 设备 0,1
   python examples/distributed/01_hello_rank.py --compile-only  # 只编译，打印产物目录
   ```

3. **观察现象**：程序结束打印 `OK`（内置断言 `y == x + arange(N)` 已在 [examples/distributed/01_hello_rank.py:123-127](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/distributed/01_hello_rank.py#L123-L127) 校验）；`--compile-only` 会输出编译产物目录。
4. **预期结果**：`OK`。若你把 `-d` 改成只给一个设备，会得到 `need exactly 2 devices` 的退出错误（[examples/distributed/01_hello_rank.py:100-101](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/distributed/01_hello_rank.py#L100-L101)）。模拟器平台的实际运行耗时待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：把内核从 `y = x + r` 改成 `y = x * (r + 1)`，输出的哪一部分证明了哪个 rank 生产的？
**答案**：`y[r]` 这一「行」由 rank r 生产——每个 rank 只写自己认领的输出片，行内容（乘了 `r+1`）即身份证明。参考 `02_programming_model.py`，它正是这个程序。

**练习 2**：为什么 `per_rank` 这层「一行转发」的包装不可省略、HOST 不能直接 `add_rank(..., device=r)`？
**答案**：`device=` 派发的目标是每设备编排层（`@pl.jit`），InCore 内核由包装层不带 `device=` 调用；这保证控制面（HOST 循环）与执行面（AICore 内核）之间有明确的桥接层，也保证内核签名不含任何编排信息（见 `docs/en/user/distributed/00-model.md` § Per-Rank Dispatch）。

### 4.2 窗口内存：DistributedTensor 与 alloc/window 两段式

#### 4.2.1 概念说明

跨 rank 通信的地基是**窗口缓冲**（window buffer）：HOST 声明一块**每 rank 对称**的缓冲，所有 rank 的同位窗口构成一个可互相单边读写的地址空间池。一个**通信域**（comm domain）是共享同一对称窗口池的 rank 子集，默认是全世界。

使用上是**两段式**：

1. `pld.alloc_window_buffer(...)`——按字节（或 shape+dtype 便捷形式）声明缓冲，返回一个**分配身份令牌**（`PtrType`）；
2. `pld.window(buf, shape, dtype=…)`——把令牌物化成带类型/形状的 **`DistributedTensor` 视图**。

为什么拆两步？因为**一块缓冲可以在循环里开多个视图**：HOST 的 `for r` 每轮用同一个 `buf` 造一个 `window`，传给该 rank 的内核。

`DistributedTensor` 在 DSL 表面与 `pl.Tensor` 完全同构（同样的 `[shape, dtype, layout]` 下标），唯一区别在 IR 层：它的类型节点是独立的 `ObjectKind`（`ir.DistributedTensorType`）。这让所有跨 rank 算子的验证器可以**精确匹配**地把普通 `Tensor` 拒之门外——普通张量永远不可能被误喂进远程槽位。文档列出的仅有的两个例外：`put` 的 `src` 和 `get` 的 `dst` 接受普通 `Tensor`（本地侧只需要一段可读/可写的 GM）。

#### 4.2.2 核心流程

```text
HOST 编排函数
  ├─ buf  = pld.alloc_window_buffer([1, SIZE], dtype=pl.FP32)   # 控制面：声明布局
  └─ for r in pl.range(pld.world_size()):
       view = pld.window(buf, [1, SIZE], dtype=pl.FP32)          # 控制面：类型化视图
       per_rank(x[r], y[r], view, device=r)                      # 视图随参数进入内核

编译流水线末段（Pass 41 MaterializeCommDomainScopes）
  ├─ 收集每个 alloc_window_buffer 赋值 → 构造 ir.WindowBuffer 记录（大小/名字/跨度）
  ├─ 收集每个 window 视图 → 绑定 view_var → alloc
  ├─ 扫描 device= 派发 → 推断设备描述符
  └─ 把每个 host_orch 函数体包进 CommDomainScopeStmt（每个推断出的通信域一层）
     运行时据此给各 rank 绑定物理缓冲
```

关键点：**用户从不手写通信域作用域**——`CommDomainScopeStmt` 与 `WindowBuffer` 全部由 Pass 41 从 `alloc_window_buffer` 调用和 `device=` 关键字**推断物化**（见 [python/pypto/language/distributed/__init__.py:12-18](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/distributed/__init__.py#L12-L18) 的说明）。

#### 4.2.3 源码精读

[python/pypto/language/distributed/op/tensor_ops.py:181-238](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/distributed/op/tensor_ops.py#L181-L238) 是 `alloc_window_buffer` 的两种形态：规范字节形态 `alloc_window_buffer(size)` 与便捷形态 `alloc_window_buffer(shape, *, dtype)`（字节数自动算作 `product(shape) × dtype.get_byte()`）。注意它**只能在 HOST 层调用**，且必须以 `buf = pld.tensor.alloc_window_buffer(...)` 简单赋值的形式出现——`name` 由解析器从赋值左侧注入（[同文件:239-243](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/distributed/op/tensor_ops.py#L239-L243) 在 name 为空时直接抛错）。

[python/pypto/language/distributed/op/tensor_ops.py:266-304](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/distributed/op/tensor_ops.py#L266-L304) 是 `window`：形状与 dtype 在这里进入类型系统，产物类型 `DistributedTensorType` 携带一个稍后由 Pass 41 回填的 `WindowBuffer` 反向引用；实现里显式检查入参必须是 `PtrType`。

[python/pypto/language/distributed/typing/distributed_tensor.py:59-72](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/distributed/typing/distributed_tensor.py#L59-L72) 是 `DistributedTensor` 类本体：继承 `Tensor`、DSL 表面一致，区别只在 IR 级 `ObjectKind`，`pl.load`/`pl.store` 对它透明地操作**本 rank 的那片**。

[examples/distributed/05_remote_load_store.py:109-119](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/distributed/05_remote_load_store.py#L109-L119) 是完整的两段式使用现场：先 `alloc_window_buffer` 造 `data_buf` 与 `signal_buf` 两块缓冲，再在 rank 循环里各开一个视图传给 `per_rank_load`。**数据窗口 + 信号窗口**是这个范式的标配组合。

Pass 侧的对照表在 [docs/en/dev/passes/41-materialize_comm_domain_scopes.md:12-20](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/passes/41-materialize_comm_domain_scopes.md#L12-L20)：`WindowBuffer` 之于窗口，正如 u5-l7 讲过的 `MemRef` 之于片上内存——分配追踪到消费点、构造反向引用、穿到类型上供代码生成 O(1) 查询。

#### 4.2.4 代码实践

1. **实践目标**：亲眼看到「窗口是本 rank 可见的普通张量」——不通信，只读写自己的片。
2. **操作步骤**：运行教学阶梯 step 03（窗口内存专讲，无任何通信）：

   ```bash
   python examples/distributed/03_window_buffer.py
   ```

   然后阅读该文件，找到 `alloc_window_buffer` 与 `window` 的调用位置，数一数：缓冲声明了几次？视图开了几次？
3. **观察现象**：程序打印 `OK`（内置 golden 校验）。
4. **预期结果**：缓冲在 HOST 声明一次、循环内每 rank 开一次视图；窗口内存的生存期是本次编排调用——默认编排结束窗口即回收（要跨调用保留需 `persistent=True`，见 `docs/en/user/distributed/03-execution.md`）。实际运行待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`pl.load(data, ...)` 里的 `data` 是 `DistributedTensor` 时，读的是谁的数据？
**答案**：**本 rank 自己窗口片**。`DistributedTensor` 上的普通 `pl.load`/`pl.store` 语义不变；要读**对端**的片必须走 `pld.tile.remote_load` 等远程算子。

**练习 2**：为什么 `put` 的 `src`、`get` 的 `dst` 允许普通 `Tensor`，而 `put` 的 `dst`、`get` 的 `src` 必须是 `DistributedTensor`？
**答案**：窗口绑定侧是对端需要槽位的一侧（`put.dst` 是对端被写、`get.src` 是对端被读）；本地侧只需一段可读/可写的本地 GM，普通 `Tensor` 即可（经 `AsTensorTypeLike` 匹配放行），这让内核能直接从 host 背书张量推送而无需先暂存进窗口（见 [docs/en/dev/distributed_ops.md:7-16](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/distributed_ops.md#L7-L16)）。

### 4.3 信号同步：notify/wait 与（信用）屏障

#### 4.3.1 概念说明

`pld.system.notify` / `pld.system.wait` 是纯**控制面**原语——不搬任何数据：

- `notify(signal, peer=…, offsets=…, value=…, op=…)`：把 `value` 写进 **peer 那个 rank** 的信号窗格里。`op` 二选一：`NotifyOp.AtomicAdd`（原子加，多写者安全）或 `NotifyOp.Set`（非原子置值）。
- `wait(signal, offsets=…, expected=…, cmp=…)`：阻塞**本 rank 自己**的信号窗格直到满足比较。`cmp` 二选一：`WaitCmp.Eq`（等于）或 `WaitCmp.Ge`（大于等于）。

InCore 内核里的身份三连：`ctx = pld.get_comm_ctx(data)` 从窗口张量反查通信上下文，`pld.rank(ctx)` / `pld.nranks(ctx)` 读出「我是谁 / 一共几人」。`world_size()` 只在 HOST 可调，这是刻意的分层。

**手写屏障**就是「通知所有人、等所有人」：每个 rank 对所有 peer `notify(+1)`，再对所有 peer 的格子 `wait(Ge 1)`。**内建** `pld.tensor.barrier(signal)` 是同一件事的一句话形式，但它的底层不是简单计数——而是一套**自清零信用屏障**（credit barrier）协议，见下。

#### 4.3.2 核心流程

手写单次会合屏障（每个 rank 执行）：

```text
for peer in 0..P-1, peer != my_rank:
    notify(signal, peer, [my_rank, 0], value=1, op=AtomicAdd)   # 给每个 peer 的「我这一行」+1
for src in 0..P-1, src != my_rank:
    wait(signal, [src, 0], expected=1, cmp=Ge)                  # 等每个 peer 都到过账
```

内建集合通信共用的信用屏障协议（每个 rank、每次调用内按代号 g 计数）：

\[\begin{aligned} \text{body:}\quad & \forall \text{peer} \neq r:\ \text{notify}(+1) \ \to\ \forall \text{src} \neq r:\ \text{wait}(\ge g) \\ \text{epilogue:}\quad & \forall \text{src} \neq r:\ \text{notify}(\text{self}, \langle\text{src cell}\rangle, -P,\ \text{AtomicAdd}) \end{aligned}\]

每个信号格是一个**信用计数器**：每次 notify 是生产者的 +1，尾声是唯一消费者的 −P（P 为 rank 数，可为运行期标量）。加减原子且可交换，所以**当所有 rank 完成尾声后信号可证明全零**——信号不携带任何跨调用状态，每次调用代号都从 1 重新开始，因此同一信号窗可以背靠背复用（包括在 `for`/`while`/`if` 里）。

`Ge` 而非 `Eq` 是**承重设计**：快的对端可能在慢的 rank 首次轮询前就把格子推过目标值，等值等待将永远无法通过。同理，`Set` 绝不能与 `AtomicAdd` 混用在同一批格子上——置值会清掉已累积的计数。

#### 4.3.3 源码精读

[examples/distributed/04_barrier.py:59-98](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/distributed/04_barrier.py#L59-L98) 是手写屏障内核 `barrier_handrolled`：先 `get_comm_ctx`/`rank` 拿身份（L65-66），两个 `pl.range` 循环分别做 notify-all（L74-82）与 wait-all（L83-90）；屏障后把**本 rank 信号行**逐格抄进输出（L95-97）——输出矩阵里 rank r 的行是「除自己以外全为 1」，这就是「每个 peer 都到了」的物证。注意文件 docstring 的提醒：这是**单次**会合；同一窗口做第二次屏障需要清零或按代递增阈值，因为计数单调、`Ge(1)` 早已满足。

[examples/distributed/04_barrier.py:101-121](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/distributed/04_barrier.py#L101-L121) 是揭示版 `barrier_builtin`：一句话 `signal = pld.tensor.barrier(signal)`（L116）之后 `remote_load` 读下一个 rank 的数据片。没有屏障时 load 会与对端的 store 竞争；golden `y[r] = x[(r+1)%N]` 成立恰好证明屏障排好了序。

[python/pypto/language/distributed/op/system_ops.py:49-70](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/distributed/op/system_ops.py#L49-L70) 是 `world_size()`：返回 INT64 标量，**仅 HOST 层合法**（InCore 里调用是解析期错误），返回包装成 DSL `Scalar` 使 `pl.range(pld.world_size())` 这类组合自然书写。

[python/pypto/language/distributed/op/system_ops.py:73-122](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/distributed/op/system_ops.py#L73-L122) 是身份三连 `get_comm_ctx` / `rank` / `nranks`：`get_comm_ctx` 的 C++ 验证器用精确 `ObjectKind` 匹配拒绝普通 `Tensor`；`rank`/`nranks` 代码生成时分别降为运行时 `CommContext::rankId` / `rankNum` 字段的一次 i32 读取。

[python/pypto/language/distributed/op/system_ops.py:125-186](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/distributed/op/system_ops.py#L125-L186) 是 `notify` / `wait` 的 DSL 包装。两个细节值得注意：`notify` 把同一逻辑操作数叫 `target`、`wait` 叫 `signal`，指的都是那块窗口信号张量；位置参数（positional-or-keyword）与仅关键字（`op=` / `cmp=`，降为 IR 属性打印成整数）的切分是为了**打印出的 IR 能被解析器原样读回**——这正是 u4-l7 讲过的往返一致性纪律在分布式算子上的延续。

[python/pypto/language/distributed/op/tensor_ops.py:771-801](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/distributed/op/tensor_ops.py#L771-L801) 是内建 `barrier`：接受秩 1 `[world_size]` 或秩 2 `[world_size, 1]` 的 INT32 信号窗，返回重绑的同一视图；文档字符串明确它走 `LowerCompositeOps` 展开成 notify-all/wait-all 序列、且按上述自清零协议**跨调用可复用**。

信用屏障协议的权威描述在 [docs/en/dev/distributed_ops.md:143-175](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/distributed_ops.md#L143-L175)：含协议伪代码、`Ge` 承重论证、以及两条约束（mesh 与 ring 的信号不能共用一块窗；中途出错的调用会漏信用，需 host 侧 `reset_persistent_windows` 恢复）。

#### 4.3.4 代码实践

1. **实践目标**：分别运行手写与内建屏障，理解「内建是手写的语法糖」。
2. **操作步骤**：

   ```bash
   python examples/distributed/04_barrier.py                    # 手写：输出信号行矩阵
   python examples/distributed/04_barrier.py --use-builtin     # 内建：用数据验证顺序
   ```

3. **观察现象**：手写模式下输出是 `[N,N,1]` 的 INT32 张量，对角线为 0、其余为 1（每个 peer 都到过账）；内建模式下输出满足 `y[r] = x[(r+1)%N]`。
4. **预期结果**：两次都打印 `OK`（内置断言分别见 [examples/distributed/04_barrier.py:240-241](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/distributed/04_barrier.py#L240-L241) 与 L233-238）。尝试把手写版里的 `Ge` 换成 `Eq` 在 P=2 下仍可能通过（两次 notify 不会把格子推过 1），但这是脆弱的巧合——协议层面 `Ge` 才是正确选择。

#### 4.3.5 小练习与答案

**练习 1**：手写屏障里每个 rank 用 `offsets=[my_rank, 0]` 通知所有 peer，每个格子只有唯一写者——那为什么还用 `AtomicAdd` 而不用 `Set`？
**答案**：唯一写者时 `Set` 确实等价（示例 docstring 也是这么说的）；`AtomicAdd` 是**规范形态**，它同时适用于多写者共享格子的屏障（多个 rank 往同一格累加），教学上教一次、到处能用。

**练习 2**：`pld.world_size()` 与 `pld.nranks(ctx)` 都返回「人数」，为什么是两个算子？
**答案**：`world_size` 是**编译/编排期**的全世界人数，只在 HOST 合法；`nranks(ctx)` 是**运行期**从本内核所属通信域读出的人数，为子域通信和动态 rank 数（4.5 节）留出空间。两者在默认全域 + 固定 P 时相等。

### 4.4 远程数据搬运：remote_load/remote_store 与 put/get

#### 4.4.1 概念说明

数据原语按「IR 层级 × 发起方」分四件：

| 算子 | 层级 | 方向 | 语义 | 硬件 |
| --- | --- | --- | --- | --- |
| `pld.tile.remote_load` | Tile | 拉 | 把对端窗口片读进**本地片上 Tile**，产出 `TileType` | TLOAD |
| `pld.tile.remote_store` | Tile | 推 | 把本地 Tile 写进对端窗口片，纯副作用 | TSTORE |
| `pld.tensor.put` | Tensor | 推 | 本地 GM ↔ 对端窗口的批量搬运 | TPUT |
| `pld.tensor.get` | Tensor | 拉 | 对端窗口 → 本地 GM 的批量搬运 | TGET |

**推（push）与拉（pull）**是同一交换的两侧：`put` 由发送方发起，`get` 由接收方发起。命名空间编码的是 **IR 层级**而非随意分组：`remote_load` 产 Tile，所以与 `tile.load` 同居 `pld.tile`；`put`/`get` 两侧都是 GM 级操作数，与 `alloc_window_buffer` 同居 `pld.tensor`。另外还有 `pld.tensor.remote_store`——同一推送的 Tensor 级形态，由 `ConvertTensorToTileOps` 一比一降到 Tile 形态（「一个算子、两个层级」的又一例）。

`put`/`get` 的搬运路径要经过一块 **VEC 暂存 Tile**（GM → UB → 远端 GM），该 Tile 由 `ConvertTensorToTileOps` 作为内部 `tile.create + pld.tile.put/get` 物化，**从不出现在 DSL 表面**；可选 `chunk_rows`/`chunk_cols` 把暂存 Tile 缩成子块让硬件自动分块（搬运可大于 UB），`pipeline=True` 双缓冲乒乓重叠。put/get 声明 VECTOR 核亲和。

#### 4.4.2 核心流程

以「一步环形移位」为例（`y[r] = x[(r±1) % N]`），四种写法对照：

```text
remote_load 版（拉）:  stage(自己的片) → barrier → remote_load(next 的片) → 写 y
remote_store 版（推）: stage(自己的片) → remote_store 进 next 的窗口 → barrier
                      → pl.load 读自己的窗口（刚被 prev 写过）→ 写 y
put 版（推）:          stage → put(dst, peer=next) → notify(next)+wait(prev)
                      → pl.load 读自己的 dst → 写 y
get 版（拉）:          stage → notify(prev)+wait(next) → get(dst, peer=next)
                      → pl.load 读自己的 dst → 写 y
```

注意 **notify 的方向要跟发起方对齐**：`put` 推给谁就通知谁、等的是「往我这里推的人」；`get` 从谁那里拉，就要让**读我的人**收到通知——06 示例的 get 分支通知的是 `(my_rank + nranks - 1) % nranks`（前一个 rank），这样等待条件恰好由「我 get 的那个 rank」满足，对任意 P 都成立。

#### 4.4.3 源码精读

[examples/distributed/05_remote_load_store.py:42-62](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/distributed/05_remote_load_store.py#L42-L62) 是 `shift_by_load`：L54-55 `pl.load` 读本地输入并 `pl.store` 进**自己的窗口片**（stage，让对端有东西可拉）；L57 内建屏障保证全员就位；L59-60 `peer = (my_rank + 1) % nranks` 后 `pld.tile.remote_load(data, peer=peer, offsets=[0,0], shape=[1,SIZE])` 一步拉回下一个 rank 的片。签名里 `data` 与 `signal` 都是 `pld.DistributedTensor` 注解。

[examples/distributed/05_remote_load_store.py:65-86](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/distributed/05_remote_load_store.py#L65-L86) 是推的方向 `shift_by_store`：先 `pld.tile.remote_store(local, data, peer=peer, offsets=[0,0])` 把本地 Tile 推进下一个 rank 的窗口（L79），屏障后用**普通** `pl.load` 读自己的窗口（L84）——上一个 rank 刚写进去。

[examples/distributed/06_put_get.py:41-78](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/distributed/06_put_get.py#L41-L78) 是 `put_step`：L58 `pld.tensor.put(dst, peer=peer, src=src, atomic=pld.AtomicType.None_)` 全片推送（`atomic=None_` 即覆写模式，`Add` 则原子累加——split-K 跨卡累加的对应物）；L61-73 notify 目标 peer + wait 自己的格子，完成握手后才读回自己的 `dst`。

[examples/distributed/06_put_get.py:81-124](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/distributed/06_put_get.py#L81-L124) 是 `get_step`：先信号后取数——L106-118 通知**前一个** rank（即「从我这里读数据的人」）、等自己的格子；L120 `pld.tensor.get(dst, peer=get_peer, src=src)` 才发起拉取。docstring（L89-94）解释了为何这样配对在任意 rank 数下都正确。

四件原语的完整规范（验证器规则、动态搬运维度、分块/双缓冲约束、混合内核里的核亲和表）都在 [docs/en/dev/distributed_ops.md:196-396](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/distributed_ops.md#L196-L396)，其中 `put`/`get` 的签名与分块语义见 L285-396。混合内核中 `put`/`get` 声明 VECTOR 亲和、`notify`/`wait` 故意不声明（SHARED 复制到双lane，`wait` 在 cube lane 上的存在本身是承重的），详见 [docs/en/dev/distributed_ops.md:73-108](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/distributed_ops.md#L73-L108)。

#### 4.4.4 代码实践

1. **实践目标**：运行同一段环形移位的四个变体，体会「推/拉 × Tile/Tensor」四种视角。
2. **操作步骤**：

   ```bash
   python examples/distributed/05_remote_load_store.py                # remote_load（拉）
   python examples/distributed/05_remote_load_store.py --mode store   # remote_store（推）
   python examples/distributed/06_put_get.py                          # put（推）
   python examples/distributed/06_put_get.py --mode get               # get（拉）
   ```

3. **观察现象**：拉式两个变体输出 `y[r] = x[(r+1)%N]`（我要下一个 rank 的数据）；推式两个变体输出 `y[r] = x[(r-1)%N]`（我的数据被写去下一个 rank，我拿到上一个 rank 的）。golden 计算在 [examples/distributed/05_remote_load_store.py:135-140](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/distributed/05_remote_load_store.py#L135-L140) 与 [06_put_get.py:179-184](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/distributed/06_put_get.py#L179-L184)。
4. **预期结果**：四个命令各打印一次 `OK`。注意 05 的屏障是 `pld.tensor.barrier`（内核内一句话），06 用的是裸 notify/wait 握手——两代抽象同台。

#### 4.4.5 小练习与答案

**练习 1**：`remote_load` 和 `get` 都是「拉」，什么时候选哪个？
**答案**：产物形态决定选择。`remote_load` 直接产**片上 Tile**（马上要参与计算、走 TLOAD），省一次落 GM；`get` 把数据落到**本地 GM**（`dst` 可以是普通 `Tensor`），适合大批量搬运或后续由别的内核消费。`get` 还自带 chunk/pipeline 选项，能搬超过 UB 容量的数据。

**练习 2**：把 06 示例 put 分支里的 `atomic=pld.AtomicType.None_` 改成 `pld.AtomicType.Add`，P=2 的一次性程序行为会怎样？
**答案**：目标窗初始为 0（本例只做一次交换），`Add` 与覆写数值结果相同；差别在多次交换或多人写同一目标时才显现——`Add` 是「对端区域 += 源」的合并语义，是跨 rank 累加（如 all-to-all combine）的基础。本例 golden 仍应通过；如需观察差异需构造两次推送（待本地验证）。

### 4.5 动态 rank 数与教学阶梯（steps 01–16）

#### 4.5.1 概念说明

前六个示例都硬编码 `N_RANKS = 2`。一旦把 rank 数写进 shape，换 P 就要改源码重编。**动态 rank 数**的做法是 u2-l2 讲过的 `pl.dynamic`：

```python
SIZE = 64
NR = pl.dynamic("NR")        # rank 数运行期才绑定
```

世界张量声明为 `[NR, 1, SIZE]`，内核循环用运行期 `pld.nranks(ctx)` 做界、peer 用 `(my_rank ± 1) % nranks` 计算——**同一份源码不经修改即可在 P=2/3/4… 上编译运行**，只改 `-d` 参数。这也是缓存键设计的延续：`NR` 折叠进键的 `None` 槽位，一份产物服务所有 P（u2-l1）。

教学阶梯 `docs/en/user/distributed/05-tutorials.md` 把上述全部概念组织成 **16 步、一步一个程序**的序列，遵循两条教学纪律：

- **揭示纪律（reveal discipline）**：走读页绝不提前介绍内建（`pld.tensor.barrier`、`allreduce`…）——等你先手写一遍、知道它降级成什么，内建才登场；
- **递进纪律（progression）**：每一步只用之前步骤（或前置章节）教过的概念。

**本版更新**（c7ba9fb → ec5d20c）：阶梯从 7 步扩到 **11 步可运行**——新增 steps 08–11 的 all-reduce 系列（mesh 全互读 / 两阶段 reduce-scatter+all-gather / ring 环形旋转三种手写实现 + 第 11 步揭示 `pld.tensor.allreduce` 内建并 diff IR），配套四篇走读 `13~16-allreduce_*.md`，CI 增加 P=4 腿。steps 12–15（其余集合通信）与 16（组合）仍在规划中。08–11 的精读属于下一讲 u7-l3。

#### 4.5.2 核心流程

16 步阶梯的结构（✅ 已交付 / 规划中）：

| 步 | 程序 | 教学目标 | 状态 |
| --- | --- | --- | --- |
| 01 | `01_hello_rank.py` | rank 身份、`world_size`、`DistributedConfig` | ✅ |
| 02 | `02_programming_model.py` | 三级模型：host → device → chip | ✅ |
| 03 | `03_window_buffer.py` | 窗口内存：alloc/window，只读写自己的片 | ✅ |
| 04 | `04_barrier.py` | 纯信号：notify/wait 手写屏障，揭示 `barrier` | ✅ |
| 05 | `05_remote_load_store.py` | Tile 级 RMA：remote_load/remote_store | ✅ |
| 06 | `06_put_get.py` | Tensor 级点对点：put/get，推 vs 拉 | ✅ |
| 07 | `07_dynamic_rank_count.py` | 动态 rank 数：一份源码任意 P | ✅ |
| 08 | `08_allreduce_mesh.py` | all-reduce v1（mesh）：人人读人人、本地求和 | ✅ 新 |
| 09 | `09_allreduce_two_phase.py` | all-reduce v2：reduce-scatter + all-gather | ✅ 新 |
| 10 | `10_allreduce_ring.py` | all-reduce v3（ring）：分块绕环 | ✅ 新 |
| 11 | `11_allreduce_reveal.py` | **揭示**：`pld.tensor.allreduce`（mesh/ring），diff IR | ✅ 新 |
| 12–15 | broadcast / allgather / reduce_scatter / all_to_all | 其余集合通信（各带揭示） | 规划 |
| 16 | `16_putting_it_together.py` | 三种集合通信组合进一个内核 | 规划 |

集合通信与原语的分层也体现在 Pass 流水线（u3-l5 的 40–42 号）：Pass 40 `SynthesizeAllReduceSignals` 在 host 编排省略 `signal` 时（仅 mesh 模式）自动合成一块私有信号窗；Pass 41 `MaterializeCommDomainScopes` 物化通信域与 `WindowBuffer`；Pass 42 `LowerHostTensorCollectives` 把 host 级张量集合通信降成内建芯片派发（u7-l3 的主角之一）。

#### 4.5.3 源码精读

[examples/distributed/07_dynamic_rank_count.py:43-44](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/distributed/07_dynamic_rank_count.py#L43-L44) 是全部魔法所在：两行——`SIZE = 64` 与 `NR = pl.dynamic("NR")`。对比 05/06 顶部的 `N_RANKS = 2` 常量，rank 数从此不进任何 shape。

[examples/distributed/07_dynamic_rank_count.py:47-84](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/distributed/07_dynamic_rank_count.py#L47-L84) 是 rank 无关的内核：L56-58 从 `get_comm_ctx` 拿 `my_rank`/`nranks`；L63 `peer = (my_rank + 1) % nranks` 用运行期值算邻居；循环界、golden 全部按实际 P 推导。文件 docstring（L11-32）点明它的定位——固定 P=2 的地基步骤与 P=4 集合通信比较（steps 08+）之间的桥。

[docs/en/user/distributed/05-tutorials.md:54-71](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/user/distributed/05-tutorials.md#L54-L71) 是阶梯总表（上表的出处）；[同文件:15-44](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/user/distributed/05-tutorials.md#L15-L44) 阐明两条教学纪律与「先手写后揭示」的设计理由；[同文件:76-142](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/user/distributed/05-tutorials.md#L76-L142) 是**抽象对照表**——每个 `pld` 抽象一行：用途、章节出处、运行层级、教学步号，并声明「覆盖契约：代码里存在的每个抽象都能从某个示例学会」。

[docs/en/user/distributed/00-model.md:131-143](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/user/distributed/00-model.md#L131-L143) 一句话定义模型：「PyPTO 的分布式模型是**对称内存 + 信号**」，并强调所有内建集合通信都是这些原语的**组合**——`pld.tensor.*` 是语法糖，不是另一个库。

#### 4.5.4 代码实践

1. **实践目标**：验证「一份源码、任意 P」，并按阶梯目录建立自学地图。
2. **操作步骤**：

   ```bash
   python examples/distributed/07_dynamic_rank_count.py -d 0,1
   python examples/distributed/07_dynamic_rank_count.py -d 0,1,2,3 --mode get   # 若有 4 设备/模拟实例
   ```

   然后打开 `docs/en/user/distributed/05-tutorials.md`，把 16 步表格抄录成自己的笔记，每步用一句话记录教学目标。
3. **观察现象**：两次运行 golden 都按实际 P 推导（P=4 时 `y[r] = x[(r+1)%4]`），源码零修改。四设备模拟是否可用取决于本地模拟器实例配置，待本地验证。
4. **预期结果**：`OK`；你的笔记应能回答「窗口内存在哪一步教、屏障在哪一步揭示、动态 rank 数为什么排在集合通信之前」。

#### 4.5.5 小练习与答案

**练习 1**：为什么「动态 rank 数」（step 07）恰好排在集合通信系列（steps 08+）之前？
**答案**：steps 08–11 要在 P=2 与 P=4 下对比 mesh/两阶段/ring 三种算法的通信量与轮次差异（P=2 时三者退化成同一次交换，看不出差别）；只有先让同一份源码能跑任意 P（step 07 的 `pl.dynamic`），这种对比才不需要为每个 P 维护一份代码。CI 的 P=4 腿也建立在这之上。

**练习 2**：host 编排里写 `pld.tensor.allreduce(data)`（省略 `signal`）合法吗？
**答案**：在 `for`/`while` 循环外合法——Pass 40 `SynthesizeAllReduceSignals` 会自动合成一块 `[world_size, core_num]` 的私有 INT32 信号窗（仅 mesh 模式；`mode="ring"` 必须显式给信号）。InCore 降级路径与刻意构造内部协议的测试则始终用显式信号（见 [docs/en/dev/distributed_ops.md:506-519](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/distributed_ops.md#L506-L519)）。

## 5. 综合实践

把本讲四个模块串成一个任务：**两 rank 分治计算 + 屏障 + 交换结果**。

**任务**：rank r 各自把自己的半区输入翻倍（`2 * x[r]`），屏障对齐后**互换**计算结果——最终每个 rank 的输出是**对端**算好的那一半（`y[r] = 2 * x[(r+1) % 2]`）。

下面的程序是**示例代码**（仿照 `05_remote_load_store.py` 的拉式结构改写，尚未入库验证，运行结果待本地验证）：

```python
# swap_doubles.py —— 示例代码：分治计算 + barrier + remote_load 交换
import pypto.language as pl
import pypto.language.distributed as pld
from pypto.ir.distributed_compiled_program import DistributedConfig
from pypto.runtime import RunConfig

N_RANKS = 2
SIZE = 64


@pl.jit.incore
def compute_and_swap(
    x: pl.Tensor[[1, SIZE], pl.FP32],                       # 本 rank 的半区输入
    y: pl.Out[pl.Tensor[[1, SIZE], pl.FP32]],               # 本 rank 的输出（对端算好的半区）
    data: pld.DistributedTensor[[1, SIZE], pl.FP32],        # 数据窗口
    signal: pld.DistributedTensor[[N_RANKS, 1], pl.INT32],  # 信号窗口
):
    ctx = pld.get_comm_ctx(data)
    my_rank = pld.rank(ctx)
    nranks = pld.nranks(ctx)

    # 1. 分治计算：翻倍自己的半区（u1-l5 的三段式）
    local = pl.load(x, [0, 0], [1, SIZE])
    local = pl.add(local, local)
    # 2. 暂存进自己的窗口片，让对端有东西可拉
    data = pl.store(local, [0, 0], data)
    # 3. 屏障：全员算完并存好（4.3 节）
    signal = pld.tensor.barrier(signal)
    # 4. 交换：拉对端算好的半区（4.4 节）
    peer = (my_rank + 1) % nranks
    recv = pld.tile.remote_load(data, peer=peer, offsets=[0, 0], shape=[1, SIZE])
    return pl.store(recv, [0, 0], y)


@pl.jit
def per_rank(x, y, data, signal):
    return compute_and_swap(x, y, data, signal)


@pl.jit.host
def swap_program(
    x: pl.Tensor[[N_RANKS, 1, SIZE], pl.FP32],
    y: pl.Out[pl.Tensor[[N_RANKS, 1, SIZE], pl.FP32]],
):
    data_buf = pld.alloc_window_buffer([1, SIZE], dtype=pl.FP32)
    signal_buf = pld.alloc_window_buffer([N_RANKS, 1], dtype=pl.INT32)
    for r in pl.range(pld.world_size()):
        data = pld.window(data_buf, [1, SIZE], dtype=pl.FP32)
        signal = pld.window(signal_buf, [N_RANKS, 1], dtype=pl.INT32)
        per_rank(x[r], y[r], data, signal, device=r)
```

驱动侧仿照 [examples/distributed/05_remote_load_store.py:176-201](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/distributed/05_remote_load_store.py#L176-L201)：`torch.randn` 造 `x`、`torch.zeros` 造 `y`，带 `DistributedConfig(device_ids=[0,1])` 编译执行，然后断言 `torch.allclose(y, 2 * x[(torch.arange(2) + 1) % 2])`。

**验证与思考题**：

1. 把第 3 步的 `pld.tensor.barrier` 删掉，程序可能输出什么？为什么说这是竞态而非确定性错误？（提示：没有屏障时 `remote_load` 可能赶上对端还没 `store`。）
2. 把第 4 步换成**推式**：用 `pld.tile.remote_store` 把自己的结果推进对端窗口、屏障后 `pl.load` 读自己的窗口。golden 应改成 `y[r] = 2 * x[(r-1) % 2]`——对照 [05_remote_load_store.py:65-86](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/distributed/05_remote_load_store.py#L65-L86) 检查你的版本。
3. 若把窗口张量的注解从 `pld.DistributedTensor` 误写成 `pl.Tensor`，会在哪一步失败？（提示：4.2 节讲过的精确 `ObjectKind` 匹配——`get_comm_ctx` 的验证器直接拒绝。）

## 6. 本讲小结

- PyPTO 分布式模型 = **对称内存 + 信号**：每 rank 一块对称窗口（`alloc_window_buffer` → `window` 两段式产出 `DistributedTensor`），通信走单边 RMA，同步走 notify/wait；所有内建集合通信都是这些原语的组合（语法糖）。
- **三级函数模型**：`@pl.jit.host`（控制面：分配窗口、`device=r` 派发，不直接碰 InCore）→ `@pl.jit`（每设备包装）→ `@pl.jit.incore`（片上计算与通信）；`world_size()` 只在 HOST，InCore 用 `get_comm_ctx`/`rank`/`nranks` 反查身份。
- `DistributedTensor` 与 `pl.Tensor` DSL 表面同构、IR 层独立 `ObjectKind`——跨 rank 算子验证器据此把普通张量拒之门外（仅 `put.src`/`get.dst` 两个本地侧例外）；通信域与 `WindowBuffer` 由 Pass 41 从 alloc + `device=` 推断物化，用户不手写。
- 数据原语四件套按「层级 × 方向」分工：`remote_load`（拉→Tile）、`remote_store`（推，Tile/Tensor 两形态）、`put`（推，GM 批量、可分块/双缓冲）、`get`（拉，GM 批量）；notify 方向必须与发起方对齐（put 通知目标、get 通知读自己的人）。
- 屏障的底层是**自清零信用屏障**：AtomicAdd 计数 + 尾声 −P 清零 + `Ge` 比较，信号不携带跨调用状态，因此内建 `barrier`/`allreduce` 可在循环里复用；手写单次屏障复用窗口则需清零或按代递增阈值。
- 教学阶梯 16 步覆盖契约 + 揭示纪律：本版新增 steps 08–11（all-reduce 三种手写 + 内建揭示）；`pl.dynamic("NR")` 让同一份源码服务任意 P，是 P=4 对比与 CI 腿的基础。

## 7. 下一步学习建议

- **下一讲 u7-l3（All-Reduce 深入）**：精读 steps 08–11——mesh 全互读、两阶段 reduce-scatter+all-gather、ring 环形旋转三种手写实现的通信量/轮次差异（P=2 与 P=4 各跑一遍），并用 `16-allreduce_reveal.md` 的 IR diff 对照 `pld.tensor.allreduce` 内建（mesh/ring 两模式）的降级产物；同时关注 Pass 42 `LowerHostTensorCollectives` 如何把 host 级集合通信降成内建芯片派发。
- **补齐模型词汇**：通读 `docs/en/user/distributed/00-model.md` 的 2-rank allreduce 快速上手与逐行走读表，再翻 `01-collectives.md`（各集合通信语义）与 `02-primitives.md`（原语底座，含 `defer_wait` 延迟完成）。
- **源码延伸**：对照 `docs/en/dev/passes/41-materialize_comm_domain_scopes.md` 的 `MemRef`/`WindowBuffer` 类比表回看 u5-l7 内存规划；想深挖算子规范（验证器、动态维度、核亲和）就通读 `docs/en/dev/distributed_ops.md` 的 Op reference 一节。
