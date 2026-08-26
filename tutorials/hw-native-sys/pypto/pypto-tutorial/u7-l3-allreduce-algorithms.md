# All-Reduce 深入：三种手写实现与内建算子对照

## 1. 本讲目标

上一讲（u7-l2）我们建立了 PyPTO 的分布式编程模型：**对称窗口内存 + 单边 RMA + notify/wait 信号**，并且留下一句话——「内建集合通信都只是这些原语的组合」。本讲就把这句话拆开验货。

学完本讲，你应当能够：

1. 手写出 all-reduce 的三种经典实现，并说出各自的通信量与轮次差异：
   - **mesh 全互读**：每个 rank 读所有 peer 的完整切片；
   - **两阶段**（reduce-scatter + all-gather）：通信量约为 mesh 的一半；
   - **ring 环形旋转**：总通信量与两阶段相同，但每轮搬运量恒为 \(N/P\)，同步只发生在相邻 rank 之间。
2. 熟练使用 `pld.system.notify` / `pld.system.wait` 构造两类同步：
   - **全网格 barrier**（notify 所有 peer、wait 所有 peer 槽位）；
   - **邻居就绪握手**（store 之后 notify 右邻居、remote_load 之前 wait 左邻居）。
3. 会用 `pld.tensor.allreduce(data, signal, op=..., mode=...)` 这一个调用替代整套手写调度，并能**对照内建算子与手写版本的降级 IR**，指出「编译器替你选了什么、又没替你做什么」。

## 2. 前置知识

本讲假设你已完成 u7-l2（分布式编程模型）。快速回顾几个会反复用到的概念：

- **rank 与 world_size**：一次分布式运行中有 \(P\) 个参与方（rank），每个 rank 拿到对称窗口内存的一份。片上内核里通过 `pld.get_comm_ctx(data)` 反查通信上下文，再用 `pld.rank(ctx)` / `pld.nranks(ctx)` 得到「我是谁 / 一共几个」。
- **窗口两段式**：`pld.alloc_window_buffer(...)` 分配对称缓冲，`pld.window(buf, shape, dtype=...)` 切出视图。窗口内存**分配时清零**——这一点是本讲所有信号协议的隐含前提。
- **notify / wait 信号**：`pld.system.notify(signal, peer=r, offsets=[...], value=1, op=pld.NotifyOp.AtomicAdd)` 向 rank `r` 的信号窗格子上做原子加；`pld.system.wait(signal, offsets=[...], expected=1, cmp=pld.WaitCmp.Ge)` 自旋等待自己窗里的某个格子 ≥ 期望值。两者组合成 barrier。
- **remote_load**：`pld.tile.remote_load(data, peer=r, offsets=..., shape=...)` 从 rank `r` 的窗口**拉**一块 Tile 到本 rank 片上——单边 RMA，对端不参与。
- **Tile 尺寸必须编译期已知**：`pl.load` / `remote_load` 的 `shape` 参数是 Tile 形状，不是普通循环边界。这条约束决定了本讲四个示例中「哪些必须写 rank 数工厂、哪些可以保持动态」。

还有一个贯穿全讲的术语——**cost card（通信量账单）**：设每个 rank 持有长度为 \(N\) 的切片、共 \(P\) 个 rank，我们统一按「每个 rank 的远程读取字节数」和「轮数（同步次数）」来给每种算法记账。

## 3. 本讲源码地图

本讲的代码集中在两个地方：`examples/distributed/` 的教学阶梯 steps 08–11，以及内建算子的降级实现。

| 文件 | 作用 |
| --- | --- |
| [examples/distributed/08_allreduce_mesh.py](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/distributed/08_allreduce_mesh.py) | 手写 all-reduce v1：mesh 全互读。四个示例中唯一保持 rank 数**动态**的一个 |
| [examples/distributed/09_allreduce_two_phase.py](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/distributed/09_allreduce_two_phase.py) | 手写 all-reduce v2：reduce-scatter + all-gather，两个 barrier、`[2, nr]` 信号 |
| [examples/distributed/10_allreduce_ring.py](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/distributed/10_allreduce_ring.py) | 手写 all-reduce v3：环形旋转，`2*(P-1)` 轮、邻居就绪握手、`[2*(P-1), nr]` 信号 |
| [examples/distributed/11_allreduce_reveal.py](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/distributed/11_allreduce_reveal.py) | The reveal：`pld.tensor.allreduce` 一个调用搞定，`--mode mesh / ring` 二选一 |
| [docs/en/user/distributed/13-allreduce_mesh.md](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/user/distributed/13-allreduce_mesh.md) ~ [16-allreduce_reveal.md](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/user/distributed/16-allreduce_reveal.md) | 四篇官方 walkthrough，每篇附运行命令、代码讲解、cost card、踩坑表 |
| [python/pypto/language/distributed/op/tensor_ops.py](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/distributed/op/tensor_ops.py) | `pld.tensor.allreduce` 的 DSL 包装层：参数校验、host 信号合成、IR Call 构造 |
| [src/ir/transforms/lower_composite_ops_pass.cpp](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/lower_composite_ops_pass.cpp) | **InCore** 内建 allreduce 的降级规则（Pass 12 `LowerCompositeOps`）：mesh 与 ring 两条展开路径 |
| [docs/en/dev/passes/42-lower_host_tensor_collectives.md](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/passes/42-lower_host_tensor_collectives.md) | **HOST 编排层**集合通信降级（Pass 42）文档：`mode` 分发到 `builtin.tensor.allreduce` / `allreduce_ring` |
| [.github/workflows/ci.yml](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/.github/workflows/ci.yml) | CI 里 steps 08–11 的 P=4 运行腿，是本讲实践命令的权威来源 |

一个容易混淆的点先说清：`pld.tensor.allreduce` 有**两条互不相同的降级路径**，取决于调用出现在哪一层函数里——

- 写在 **InCore 内核**（steps 11 的写法）里 → 由 **Pass 12 `LowerCompositeOps`** 在编译期展开成一串 `pld.system.notify` / `wait` / `pld.tile.remote_load` / `tile.load` / `tile.store` 原语；
- 写在 **host 编排函数**里 → 由 **Pass 42 `LowerHostTensorCollectives`** 改写成 `builtin.tensor.allreduce`（或 `..._ring`）内部算子派发，真正的工作交给 AICPU 侧的内建内核。

本讲 step 11 走的是 InCore 路径（Pass 12），但 Pass 42 的文档同样是必读——它给出了 host 路径的信号形状约束与 ring 的能力边界（只支持 `Sum`+`FP32`、最多 16 个设备）。

## 4. 核心概念与源码讲解

### 4.1 mesh 全互读：最简单的 all-reduce 基线

#### 4.1.1 概念说明

all-reduce 的语义是：\(P\) 个 rank 各持有一个切片，调用结束后**每个 rank 都持有所有切片的逐元素归约结果**（本讲取 Sum）。

mesh 是最朴素的拼法：**每个 rank 把其他所有 rank 的切片整个拉过来，在本地逐个相加**。它不需要任何切分技巧，是衡量后面两种算法的「对照组」。它的缺点也正是它的定义——远程读取量随 \(P\) 线性增长。

这个示例同时示范了一个重要的工程事实：**rank 数可以保持动态**。信号窗的形状是运行期表达式 `[pld.world_size(), 1]`，不是编译期常量，所以一份源码可以服务任意 `P`（用 `-d` 在运行时挑选）。

#### 4.1.2 核心流程

手写集合通信共享同一个「四阶段」骨架：

```text
阶段 1  stage-in   把本 rank 的输入切片写进自己的窗口槽位
阶段 2  barrier    notify 所有 peer / wait 所有 peer（step 04 的握手，逐字复用）
阶段 3  accumulate 从自己的切片起步，remote_load 每个 peer 的切片并累加
阶段 4  stage-out  把累加结果写进本 rank 的输出张量
```

关键在阶段 2：`pl.store` 写窗口和 `remote_load` 读对端窗口都是异步落到硬件的，**没有 barrier 的话阶段 3 可能读到某个 peer 尚未落地的 store**——这是竞态，而且与时机相关，P=2 时往往藏得住、P=4 时才爆。

按每个 rank 记账：

\[
\text{mesh 每rank远程读取量} = (P-1)\cdot N \text{ 字节},\qquad \text{轮次} = 1 \text{ 个 barrier} + (P-1) \text{ 次远程读}
\]

#### 4.1.3 源码精读

**rank 数保持动态。** `NR = pl.dynamic("NR")` 出现在模块顶层，信号参数的注解直接使用它：

- [examples/distributed/08_allreduce_mesh.py:L55-L67](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/distributed/08_allreduce_mesh.py#L55-L67) — `SIZE = 64` 与 `NR = pl.dynamic("NR")`；InCore 内核 `reduce_step` 的签名：`data` 是 `[1, SIZE]` 的 `DistributedTensor`（数据窗），`signal` 是 `[NR, 1]` 的 INT32 `DistributedTensor`（信号窗）。

**阶段 1–2：stage-in + 全网格 barrier。** 每个 rank 拥有专属的一行信号（`offsets=[my_rank, 0]`），`AtomicAdd`/`Ge(1)` 保证 wait 只有在**每个** peer 都 stage 完之后才放行：

- [examples/distributed/08_allreduce_mesh.py:L74-L98](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/distributed/08_allreduce_mesh.py#L74-L98) — 先 `pl.load` 输入再 `pl.store` 进 `data` 窗口；随后两个 `pl.range(nranks)` 循环：第一个对每个 `peer != my_rank` 执行 `pld.system.notify(signal, peer=peer, offsets=[my_rank, 0], value=1, op=pld.NotifyOp.AtomicAdd)`，第二个对每个 `src != my_rank` 执行 `pld.system.wait(signal, offsets=[src, 0], expected=1, cmp=pld.WaitCmp.Ge)`。

**阶段 3–4：mesh 本体与写回。** 从自己的切片起算，逐个拉 peer：

- [examples/distributed/08_allreduce_mesh.py:L100-L108](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/distributed/08_allreduce_mesh.py#L100-L108) — `acc = pl.load(data, [0, 0], [1, SIZE])` 起步；循环里 `recv = pld.tile.remote_load(data, peer=peer, offsets=[0, 0], shape=[1, SIZE])`、`acc = pl.add(acc, recv)`；最后 `return pl.store(acc, [0, 0], y)`。

**host 编排：两块共享窗 + 每 rank 一次派发。** 注意信号窗形状是运行期表达式：

- [examples/distributed/08_allreduce_mesh.py:L121-L134](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/distributed/08_allreduce_mesh.py#L121-L134) — `data_buf = pld.alloc_window_buffer([1, SIZE], dtype=pl.FP32)`、`signal_buf = pld.alloc_window_buffer(pld.world_size() * pl.INT32.get_byte())`；循环 `for r in pl.range(pld.world_size())` 内每次 `pld.window(...)` 后 `self.per_rank(x[r], y[r], data, signal, device=r)`。

**为什么这里必须用 `@pl.program` 而不是 `@pl.jit`？** 示例 docstring 把这一点讲得很明确：

- [examples/distributed/08_allreduce_mesh.py:L29-L37](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/distributed/08_allreduce_mesh.py#L29-L37) — `signal` 是形状为 `[pld.world_size(), 1]` 的窗口，`@pl.jit` 必须为每个传给 dep 的参数静态推断 shape/dtype，会报 `missing inferred tensor metadata for parameter 'signal'`；`@pl.program` 类形式没有这个要求。**迫使切换的是动态的信号形状，不是编译期形状**——而 steps 09/10 切换的理由更强（见 4.2.3）。

**golden 与容差对照。** 归约顺序与 torch 不同，必须带容差比较：

- [examples/distributed/08_allreduce_mesh.py:L137-L140](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/distributed/08_allreduce_mesh.py#L137-L140) — `expected_allreduce`：`inputs.sum(dim=0)` 后 `torch.stack([reduced] * P)`，即每个 rank 都应拿到同一份总和。
- [examples/distributed/08_allreduce_mesh.py:L186-L189](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/distributed/08_allreduce_mesh.py#L186-L189) — `assert torch.allclose(y, expected, rtol=1e-5, atol=1e-5)`，失败时打印 max diff，成功打印 `OK`。

**踩坑速查**（摘自官方 walkthrough 的表格）：

- [docs/en/user/distributed/13-allreduce_mesh.md:L104-L118](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/user/distributed/13-allreduce_mesh.md#L104-L118) — 「Fatal pitfall — a missing barrier lets the load race the store」：删掉阶段 2 会出现「部分 rank 的和里混着 0」「只在 P=4 出错」这类时序相关症状。

#### 4.1.4 代码实践

**实践目标**：在模拟器上跑通 mesh，并亲眼确认「P=2 藏得住的竞态在 P=4 才显形」这条警告的边界。

1. 操作步骤（CI 的 P=4 腿用的就是同一条命令，见 [.github/workflows/ci.yml:L590-L591](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/.github/workflows/ci.yml#L590-L591)）：

   ```bash
   python examples/distributed/08_allreduce_mesh.py -p a2a3sim -d 0,1
   python examples/distributed/08_allreduce_mesh.py -p a2a3sim -d 0,1,2,3
   ```

2. 需要观察的现象：两个 rank 数都应打印 `OK`（官方 walkthrough 的「Expected output」即为 `OK`，见 [docs/en/user/distributed/13-allreduce_mesh.md:L36-L39](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/user/distributed/13-allreduce_mesh.md#L36-L39)）。注意这一份源码**没有** rank 数工厂——`-d` 换成任意 ≥2 的设备数都直接复用同一份编译产物逻辑（特化键里 `NR` 折叠为动态维）。
3. 加一个对照实验：把 `examples/distributed/08_allreduce_mesh.py` 复制为自己的脚本（不要改源文件），删除阶段 2 的两个循环，再跑 P=2 与 P=4 各若干次。
4. 预期结果：正常版本恒 `OK`；删 barrier 的版本可能出现部分 rank 求和缺项（数值偏小）或断言失败。竞态与时机相关，**多跑几次**才可靠；若机器上始终复现不了，记录为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`SIZE=64`、FP32、`P=4` 时，mesh 每个 rank 远程读取多少字节？两阶段呢？

**答案**：mesh：\((P-1)\cdot N = 3 \times 64 \times 4 = 768\) 字节。两阶段：\(2\cdot\frac{P-1}{P}\cdot N = 2 \times \frac{3}{4} \times 64 \times 4 = 384\) 字节——恰好一半。

**练习 2**：为什么 `signal` 的形状是 `[NR, 1]`（每个 rank 一行），而不是 `[1, 1]` 一个全局格子？

**答案**：因为 barrier 的语义是「每个 rank 等待**所有 peer**各自完成 stage」。每个 peer 把 `+1` 原子加到**属于我的那一行**（`offsets=[my_rank, 0]` 是通知方写的目标坐标，`peer=` 是发给谁），我 wait 自己行上的 `Ge(1)` 才能区分「谁到了、谁没到」。单一全局格子只能表达「总数够不够」，无法保证每个 peer 都已 store 落地，也无法支持后面「每轮一行」的扩展。

**练习 3**：示例 08 为什么不需要「信号清零」逻辑，而内建 allreduce 需要（见 4.4）？

**答案**：示例 08 的每个信号格子在整个程序生命周期里只被使用一次（单次 barrier，计数从 0 单调到 `P-1` 后不再读取）；而内建算子要支持**同一信号窗跨调用复用**（包括在 `for`/`while` 里反复调用），所以必须在尾声把信用减回零。窗口分配即清零只保证「第一次调用」从零开始。

### 4.2 两阶段：reduce-scatter + all-gather，通信量减半

#### 4.2.1 概念说明

mesh 的浪费在于：每个 rank 都读走了 peer 的**整份**切片，但求和的结果只有 \(N\) 个值。换个组织方式——把每个切片切成 \(P\) 块，让 **rank \(r\) 只负责归约第 \(r\) 块**：

1. **reduce-scatter（RS）**：rank \(r\) 从每个 peer 窗口里只读**第 \(r\) 块**（\(N/P\) 大小），本地求和后写进自己的 `result` 窗口第 \(r\) 块。结束后 rank \(r\) 独占完整的归约第 \(r\) 块。
2. **all-gather（AG）**：每个 rank 从 peer \(c\) 那里读回**已经归约好的**第 \(c\) 块，拼装成完整结果。

每阶段搬 \((P-1)\) 块 × \(N/P\) 字节，总计：

\[
\text{两阶段每rank远程读取量} = 2\cdot\frac{P-1}{P}\cdot N \text{ 字节},\qquad \text{轮次} = 2 \text{ 个 barrier}
\]

约为 mesh 的一半，代价是多一个 barrier 轮次。另一个隐含约束：\(P\) 必须整除 \(N\)（否则尾块没法均分）。

#### 4.2.2 核心流程

```text
阶段 1  stage-in    本 rank 完整切片 → data 窗口
阶段 2  Barrier A   信号第 0 行；保证所有输入已 stage（RS 读之前）
阶段 3  RS          我只读每个 peer 的第 my_rank 块并求和
                    结果写进 result 窗口的第 my_rank 块
阶段 4  Barrier B   信号第 1 行；保证所有归约块已 stage（AG 读之前）
阶段 5  AG          从 peer c 读 result 的第 c 块，写进输出 y 的第 c 块
```

信号是 `[2, nr]` 的矩阵——**一个 barrier 一行**。这不是装饰：`AtomicAdd`/`Ge` 计数器是单调的，如果两个 barrier 共用同一行，Barrier A 过后那一行已经 ≥1，Barrier B 的 `Ge(1)` 会**立即通过**，AG 就可能读到尚未完成 RS 的块。每轮一行的纪律，ring 步会推广成「每轮一行、共 \(2(P-1)\) 行」。

#### 4.2.3 源码精读

**rank 数工厂：第一次被迫要编译期 `nr`。** 原因不是信号行数，而是 chunk 尺寸是 **Tile 形状**：

- [examples/distributed/09_allreduce_two_phase.py:L44-L58](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/distributed/09_allreduce_two_phase.py#L44-L58) — `build_two_phase_allreduce(nr)`：先检查 `SIZE % nr != 0` 抛 `ValueError`，再算 `chunk = SIZE // nr`。docstring 明说：迫使 `nr` 成为编译期常量的是「`chunk` 作为下面每个 `pl.load`/`remote_load` 的 `[1, chunk]` tile 形状」，信号 `[2, nr]` 只是顺带从同一常量里落下来的。

**信号两行与 Barrier A：**

- [examples/distributed/09_allreduce_two_phase.py:L69-L97](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/distributed/09_allreduce_two_phase.py#L69-L97) — 信号注解 `pl.InOut[pld.DistributedTensor[[2, nr], pl.INT32]]`；Barrier A 用 `offsets=[0, my_rank]` notify、`offsets=[0, src]` wait——**行 0 专属 Barrier A**。

**RS：chunk 所有权。** 每个 rank 读所有 peer 窗口的**同一块**（自己的块）：

- [examples/distributed/09_allreduce_two_phase.py:L99-L110](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/distributed/09_allreduce_two_phase.py#L99-L110) — `acc = pl.load(data, [0, my_rank * chunk], [1, chunk])`；循环里 `pld.tile.remote_load(data, peer=peer, offsets=[0, my_rank * chunk], shape=[1, chunk])`——注意 offsets 里用的是 **my_rank** 而不是 peer，这正是「chunk 所有权」的体现；求和后 `result = pl.store(acc, [0, my_rank * chunk], result)`。

**Barrier B 与 AG：一行一轮、按位拼装。**

- [examples/distributed/09_allreduce_two_phase.py:L112-L140](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/distributed/09_allreduce_two_phase.py#L112-L140) — Barrier B 用 `offsets=[1, ...]`（行 1）；AG 循环 `for c in pl.range(nranks)`：从 `peer=c` 读 `offsets=[0, c * chunk]` 的块，写进 `y` 的 `[0, c * chunk]`——**读哪块、写哪块都由 c 决定**，顺序不能乱。

**踩坑速查：**

- [docs/en/user/distributed/14-allreduce_two_phase.md:L93-L105](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/user/distributed/14-allreduce_two_phase.md#L93-L105) — 「Fatal pitfall — reusing one signal row for both barriers」：症状是 AG 读到 RS 尚未写完的块；修法就是 `[2, nr]` 一行一轮。同表还有「P=2 时两算法坍缩成同一交换、差异不可见」的提醒。

#### 4.2.4 代码实践

**实践目标**：用纸面推演验证 chunk 所有权与信号行分配，再用 P=2/P=4 对比确认「省一半流量」这件事**在 P=2 时根本观察不到**。

1. 纸面推演（不需要机器）：取 `P=4, SIZE=64, chunk=16`，为 rank 1 列一张表，写明 Barrier A、RS 循环每一轮的 `peer`、`offsets`，Barrier B、AG 循环每一轮的 `c`、`offsets`。
2. 运行（CI 同款命令，见 [.github/workflows/ci.yml:L592-L593](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/.github/workflows/ci.yml#L592-L593)）：

   ```bash
   python examples/distributed/09_allreduce_two_phase.py -p a2a3sim -d 0,1
   python examples/distributed/09_allreduce_two_phase.py -p a2a3sim -d 0,1,2,3
   ```

3. 需要观察的现象：两档都应打印 `OK`（[docs/en/user/distributed/14-allreduce_two_phase.md:L36-L39](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/user/distributed/14-allreduce_two_phase.md#L36-L39)）。注意 `-d 0,1,2` 会因 `64 % 3 != 0` 被工厂里的 `ValueError` 拒绝——这是设计内的用户错误。
4. 预期结果：`P=4` 时每个 rank 远程读取 \(2 \times 3 \times 16 = 96\) 个 FP32 元素，恰为 mesh（\(3 \times 64 = 192\)）的一半；`P=2` 时两算法都是「读一个 peer 的完整切片」，完全等价。功能正确性由示例自带的 allclose 断言保证；通信量数字来自上面的公式推导（**待本地验证**：模拟器不直接打印流量统计，如需实测可对比运行耗时作近似）。

#### 4.2.5 小练习与答案

**练习 1**：把 Barrier B 的 `offsets` 从 `[1, my_rank]` / `[1, src]` 误写成 `[0, my_rank]` / `[0, src]`，会发生什么？

**答案**：Barrier B 会复用 Barrier A 的行 0。该行在 Barrier A 后已 ≥1，`Ge(1)` 立即满足，Barrier B 形同虚设；AG 的 `remote_load` 便可能与某些 rank 的 RS `store` 竞态，读回未归约（或部分归约）的块。P=2 时每 rank 只有一个 peer、单块交换，竞态窗口极小，很可能照常通过——所以要在 P=4 验证。

**练习 2**：两阶段在 `P=2` 时退化成什么？为什么文档说「P=2 把两算法坍缩成同一交换」？

**答案**：`P=2` 时 `chunk = N/2 = N`（`SIZE // 2` 当 `SIZE=N`，即整份切片），RS 变成「读一个 peer 的整份切片求和」，AG 变成「读一个 peer 的整份归约块」——与 mesh 的单 peer 全读在数据量上相同。公式也印证：mesh 为 \((P-1)N = N\)，两阶段为 \(2(P-1)N/P = N\)。

**练习 3**：为什么示例 09 必须用工厂 `build_two_phase_allreduce(nr)`，而示例 08 不用？

**答案**：09 的 `chunk = SIZE // nr` 被用作 `pl.load` / `remote_load` 的 `shape=[1, chunk]`——Tile 形状必须在内核编译时确定，所以 `nr` 必须是编译期常量，由工厂按 `-d` 的设备数在**构建 Program 之前**折叠进源码。08 中唯一随 `nr` 变化的是信号**行数** `[NR, 1]`，信号行数不是 Tile 形状，可以用 `pl.dynamic` + `pld.world_size()` 保持运行期确定。

### 4.3 ring 环形旋转：每步定量与邻居就绪握手

#### 4.3.1 概念说明

两阶段虽然把总量减半，但每个阶段里每个 rank 仍要**读所有 \(P-1\) 个 peer**。ring 把「每 peer」也去掉：把 rank 排成一个环，**每个 rank 只跟左邻居交换数据**，块在环上旋转：

- **RS 阶段（\(P-1\) 轮）**：每轮一块从左邻居传来、在本地累加后再发给右邻居；\(P-1\) 轮后每个 rank 恰好持有自己那块的完整归约。
- **AG 阶段（\(P-1\) 轮）**：归约好的块继续绕环传递，每 rank 边传边抄；再 \(P-1\) 轮后人人有全量。

\[
\text{ring每rank远程读取量} = 2\cdot\frac{P-1}{P}\cdot N \text{ 字节（与两阶段相同）},\qquad
\text{但分摊为 } 2(P-1) \text{ 轮、每轮 } N/P \text{ 字节}
\]

ring 存在的理由在**每轮尺寸**：弱扩展（\(N\) 随 \(P\) 增长）下每轮搬运量恒为 \(N/P\)，不随世界变大而膨胀。同步也变成**邻居就绪握手**：store 之后 notify **右**邻居、`remote_load` 之前 wait **左**邻居——每 rank 每轮只发 1 个信号、只等 1 个格子，O(P) 个信号每 rank；若每轮用全网格 barrier 则是 O(P²) 每 rank。

#### 4.3.2 核心流程

```text
left  = (my_rank - 1 + nranks) % nranks     # +nranks 防止负 dividend
right = (my_rank + 1) % nranks

阶段 1  stage-in   本 rank 的 P 块依次写进 scratch（一维平铺，块 c 起始 c*chunk）
                    notify 右邻居（信号行 0）
阶段 2  RS × (P-1) 轮 s = 0..P-2：
                    wait 左邻居（信号行 s）
                    recv   = remote_load(scratch@left, 块 left_send_idx)
                    acc    = load(scratch, 块 recv_add_idx) + recv → store 回原位
                    notify 右邻居（信号行 s+1）
阶段 3  AG × (P-1) 轮 s：行号从 P-1 开始递增
                    wait 左邻居（本轮行）→ 拷贝左邻居的发送块到本地 recv_idx
                    → notify 右邻居（下一行），最后一轮不再 notify
阶段 4  stage-out  scratch 的 P 块 → 输出 y
```

信号是 `[2*(P-1), P]`：**一行一轮、一格一 rank**。三个下标表达式是调度的心脏（`step = s + 1`）：

- RS 轮 `s`：`recv_add_idx = (my_rank - step - 1 + nranks) % nranks`（在哪个块上累加），`left_send_idx = (left - step + nranks) % nranks`（左邻居发来的是哪个块）；
- AG 轮 `s`：`recv_idx = (my_rank - step + nranks) % nranks`（拷到哪个块），`left_send_idx = (left - step + 1 + nranks) % nranks`。

#### 4.3.3 源码精读

**工厂与轮数。** 与 09 同一理由（chunk 是 Tile 形状）需要编译期 `nr`：

- [examples/distributed/10_allreduce_ring.py:L45-L58](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/distributed/10_allreduce_ring.py#L45-L58) — `build_ring_allreduce(nr)`：整除检查后 `total_rounds = 2 * (nr - 1)`、`chunk = SIZE // nr`；docstring 强调 `[2*(nr-1), nr]` 信号从同一常量写出，但「仅信号行数本身并不迫使我们建工厂——step 08 就把行数保持为动态」。
- [examples/distributed/10_allreduce_ring.py:L67-L69](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/distributed/10_allreduce_ring.py#L67-L69) — `scratch` 是 `[1, SIZE]` 的 DistributedTensor（P 块平铺），`signal` 是 `[total_rounds, nr]` 的 INT32。

**左右邻居与 docstring 里的协议说明。**

- [examples/distributed/10_allreduce_ring.py:L70-L85](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/distributed/10_allreduce_ring.py#L70-L85) — 内核 docstring 写明握手方向（「payload 读左邻居；同步只碰两个相邻 rank」）与「`alloc_window_buffer` 清零每个格子，所以逐格 `AtomicAdd(0→1)`/`WaitGe(1)` 无需复位」；随后 `ctx`/`my_rank`/`nranks` 与 `left`/`right` 两个取模式。

**阶段 1：逐块 stage-in 后只通知右邻居。**

- [examples/distributed/10_allreduce_ring.py:L87-L98](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/distributed/10_allreduce_ring.py#L87-L98) — `for c in pl.range(nranks)` 内 `pl.load(x, [0, c * chunk], [1, chunk])` → `pl.store(src_tile, [0, c * chunk], scratch)`；然后**单个** `pld.system.notify(signal, peer=right, offsets=[0, my_rank], value=1, op=pld.NotifyOp.AtomicAdd)`——与 mesh 的「notify 所有 peer」形成直接对照。

**阶段 2：RS 的每一轮。** 注意 wait 在 remote_load **之前**、notify 在 store **之后**：

- [examples/distributed/10_allreduce_ring.py:L100-L136](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/distributed/10_allreduce_ring.py#L100-L136) — 轮内先算三个下标（L102-L106），`pld.system.wait(signal, offsets=[rs_round, left], expected=1, cmp=pld.WaitCmp.Ge)`，`recv = pld.tile.remote_load(scratch, peer=left, offsets=[0, left_send_idx * chunk], shape=[1, chunk])`，`acc = pl.load(...) + recv` 后 store 回 `recv_add_idx` 位，最后 `notify(peer=right, offsets=[rs_round + 1, my_rank])` 把「下一轮我的块就绪」传给右邻居。

**阶段 3：AG 与「最后一轮不 notify」。**

- [examples/distributed/10_allreduce_ring.py:L138-L172](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/distributed/10_allreduce_ring.py#L138-L172) — `ag_round = (nranks - 1) + s`（AG 从行 P-1 起占用）；`if s < nranks - 2:` 才 notify 下一行——注释说明最后一轮的行号会超出信号的 `2*(nr-1)` 行。

**阶段 4 与 host。**

- [examples/distributed/10_allreduce_ring.py:L174-L205](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/distributed/10_allreduce_ring.py#L174-L205) — 逐块 load scratch → store y；host 编排只有 scratch 与 signal 两块共享窗、每 rank 一次 `self.per_rank(..., device=r)` 派发。

**官方 walkthrough 的两点提炼：**

- [docs/en/user/distributed/15-allreduce_ring.md:L80-L92](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/user/distributed/15-allreduce_ring.md#L80-L92) — 「`+ nranks` 保证 dividend 非负」：截断取模下 `(my_rank - 1) % nranks` 在 rank 0 处得 `-1`；「块在旋转，不是 rank 在旋转」；同步代价对比（邻居握手 O(P) 每 rank vs 全网格 barrier O(P²) 每 rank）。
- [docs/en/user/distributed/15-allreduce_ring.md:L99-L113](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/user/distributed/15-allreduce_ring.md#L99-L113) — 踩坑表：「P=2 挂起 → 左邻居下标为负」「结果只在 P=2 正确 → 单轮掩盖了旋转 bug，要在 P=4 检查每个块位置」。

#### 4.3.4 代码实践

**实践目标**：把轮次下标在纸面上跑通，再用 P=4 确认旋转正确性——P=2 只有一轮，几乎检验不出任何下标错误。

1. 纸面推演：`P=4, nranks=4`，为 **rank 2** 写出 RS 三轮（`s=0,1,2`）每轮的 `step`、`recv_add_idx`、`left_send_idx`、`rs_round`，以及 AG 三轮的 `recv_idx`、`left_send_idx`、`ag_round`。参考答案见下方练习 1。
2. 运行（CI 同款，[.github/workflows/ci.yml:L594-L595](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/.github/workflows/ci.yml#L594-L595)）：

   ```bash
   python examples/distributed/10_allreduce_ring.py -p a2a3sim -d 0,1
   python examples/distributed/10_allreduce_ring.py -p a2a3sim -d 0,1,2,3
   ```

3. 需要观察的现象：两档均打印 `OK`；`-d 0,1,2` 同样被整除检查拒绝。
4. 预期结果：`P=4` 时共 `2*(4-1) = 6` 轮、每轮搬运 `64/4 = 16` 个 FP32 元素；对照 4.2 的两阶段（2 个「轮组」、每轮组 3 次 × 16 元素的读取），总量一致、轮次切法不同。正确性由示例自带断言验证；「每轮定量」是调度层面的推论，模拟器不直接打印（**待本地验证**）。

#### 4.3.5 小练习与答案

**练习 1**：`P=4`、rank 2（`left=1, right=3`），写出 RS 轮 `s=0` 与 AG 轮 `s=0` 的全部下标。

**答案**：RS `s=0`：`step=1`，`recv_add_idx=(2-1-1+4)%4=0`，`left_send_idx=(1-1+4)%4=0`，`rs_round=0`——即 wait 信号 `[0, 1]`，从 rank 1 的块 0 读数据，累加进自己的块 0，notify 信号 `[1, 2]` 给 rank 3。AG `s=0`：`step=1`，`recv_idx=(2-1+4)%4=1`，`left_send_idx=(1-1+1+4)%4=1`，`ag_round=(4-1)+0=3`——wait `[3, 1]`，把 rank 1 的块 1 拷进自己的块 1，因 `s=0 < nranks-2=2` 再 notify `[4, 2]`。两个阶段的 `left_send_idx` 都等于目标块——「同一轮里收发的就是同一个块」正是该调度 RS 公式的性质（源码 L1333-L1336 的注释也点明了这一点）。

**练习 2**：ring 的信号为什么是 `[2*(P-1), P]` 而不是 `[2, P]`（像两阶段那样）？

**答案**：ring 有 `2*(P-1)` 个轮次，而两阶段只有 2 个「轮组」。计数器单调且只在 `Ge(1)` 上判定，若两轮共用一行，后一轮的 wait 会被前一轮残留的计数立即满足（与 4.2 练习 1 同型的错误）。每轮一行、一格一 rank，是「一轮一行」纪律在 ring 上的推广；最后一轮之后不再 notify，正是因为没有下一行可用了。

**练习 3**：把 RS 轮里的 `pld.system.wait` 挪到 `remote_load` **之后**会发生什么？

**答案**：wait 就失去了意义——`remote_load` 已经发出，可能读到左邻居上一轮尚未 store 完成的数据，同步没有覆盖到它该保护的读。正确的顺序是「先 wait 左邻居就绪，再读」，就像 mesh 里 barrier 必须在任何远程读之前。这类错误的表现同样是时序相关的错值/挂起，P=2 难复现。

### 4.4 内建 `pld.tensor.allreduce`：reveal 与降级 IR 对照

#### 4.4.1 概念说明

三个手写版本的教学目的不是「以后要手写」，而是「**知道内建算子在替你选什么**」。step 11 的 reveal：

```python
data = pld.tensor.allreduce(data, signal, op=pld.ReduceOp.Sum, mode=mode)
```

一个调用承担「barrier + 跨 rank 归约 + 写回窗」，`mode=` 选算法（`"mesh"` 默认 / `"ring"`）。**stage-in 与 stage-out 仍然是你的**——内建只拥有中间那段。

它在两层函数里有两条不同的降级路径（见第 3 节的对照表），本示例走 InCore 复合路径（Pass 12 `LowerCompositeOps` 在编译期展开成原语）；Pass 42 `LowerHostTensorCollectives` 负责 host 编排层调用，把它们改写成 `builtin.tensor.allreduce` / `builtin.tensor.allreduce_ring` 内部派发。两条路径的信号约定也不同：InCore ring 用 `[2*(NR-1), NR]`，**host** 内建 ring 用 `[2*(NR-1)+1, NR]`（多一行给返回 barrier）。

#### 4.4.2 核心流程

InCore 复合路径的降级（`mode` 分发）：

```text
pld.tensor.allreduce(target, signal, op, mode)
  ├─ mode 校验：必须是 "ring" 或 "mesh"（其它值按用户错误拒绝）
  ├─ mode="mesh"（默认）→ LowerTensorAllReduceRule：
  │     EmitCommSetup（get_comm_ctx / nranks / rank）
  │     ready barrier（代数 1：AtomicAdd 1 → WaitGe 1）
  │     for 每个 UB 尺寸 chunk（≤16 KiB）：
  │         acc = tile.load(target, offsets, shape, valid_shape)
  │         remote_load 每个 peer 的同一 chunk 并归约
  │         chunk 完成屏障（代数 1+k）→ tile.store 回 target
  │     尾声：从每个非自身格子减去本次调用的总信用 N（自清零）
  └─ mode="ring" → LowerTensorRingAllReduceRule：
        把 packed ND 目标重解释为一条 [1, N] 线性流
        FP32 用均衡的 floor(i·N/P) 分块边界（FP16 对齐到 32 字节）
        RS 阶段 (P-1) 轮 + AG 阶段 (P-1) 轮
        每个 UB 子块：本轮行上的「就绪 barrier + 读完成 barrier」
        尾声：从信号的每一行减去 2*chunk_count（自清零）
```

关键差异（也是 IR diff 的看点）：**内建 ring 的每一轮用的是全网格 barrier（`EmitNotifyAll`/`EmitWaitAll`），而不是你在 step 10 写的邻居就绪握手**。调度与分块相同、同步不同——这正是官方 walkthrough 说的「the point of the diff」。

自清零信用屏障（credit barrier）是内建能复用同一信号窗的原因：每次调用的 barrier 从代数 1 数起（`AtomicAdd(1) → WaitGe(g)`），尾声一次性 `AtomicAdd(-N)` 把信用减回零；加法可交换，所以所有 rank 的尾声跑完后信号**可证明**回到全零，下一次调用又从代数 1 开始。

#### 4.4.3 源码精读

**示例侧：工厂只折叠 `mode`（以及顺手把 `nr` 折进来拼两种信号形状）。**

- [examples/distributed/11_allreduce_reveal.py:L46-L66](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/distributed/11_allreduce_reveal.py#L46-L66) — `build_reveal_allreduce(nr, mode)`：`mode == "ring"` 时 `sig_rows, sig_cols = 2*(nr-1), nr`，`mode == "mesh"` 时为 `nr, 1`。docstring 指出与 steps 09/10 的本质区别：内建自己拥有 chunking，**没有任何 Tile 形状随 rank 数变化**，`nr` 本可保持动态；真正必须在 trace 时固定的是 `mode`——它同时决定降级路径**和**信号布局，而 mesh/ring 是「两个不同形状」而非「同一形状的两个长度」。
- [examples/distributed/11_allreduce_reveal.py:L78-L90](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/distributed/11_allreduce_reveal.py#L78-L90) — 内核只剩三段：stage-in（L81-L82）、`data = pld.tensor.allreduce(data, signal, op=pld.ReduceOp.Sum, mode=mode)`（L86，注意**必须重绑定** `data =`）、stage-out（L89-L90）。
- [examples/distributed/11_allreduce_reveal.py:L143-L149](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/distributed/11_allreduce_reveal.py#L143-L149) — CLI 的 `--mode` 参数（`mesh` 默认 / `ring`）。

**DSL 包装层：参数校验与 host 信号合成。**

- [python/pypto/language/distributed/op/tensor_ops.py:L582-L608](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/distributed/op/tensor_ops.py#L582-L608) — 两个 `@overload`：host 编排可省 signal；InCore 必须显式传。
- [python/pypto/language/distributed/op/tensor_ops.py:L746-L768](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/distributed/op/tensor_ops.py#L746-L768) — 省略 signal 时若 `mode != "mesh"` 直接 `ValueError`（host 信号合成只支持 mesh）；显式路径解包两个 `DistributedTensor` 后构造 `_ir_tensor.allreduce(target_expr, signal_expr, op, mode=mode, core_num=core_num)` 这个 IR Call。
- [python/pypto/language/distributed/op/tensor_ops.py:L677-L700](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/distributed/op/tensor_ops.py#L677-L700) — docstring 里的信用屏障协议说明：mesh 全有效调用共 `1 + chunk_count` 个信用、部分有效矩形路径恰 2 个；ring 每子块一对「就绪 + 读完成」代数，尾声从每行减 `2 * chunk_count`。

**降级规则：`mode` 分发与 mesh 展开。**

- [src/ir/transforms/lower_composite_ops_pass.cpp:L864-L899](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/lower_composite_ops_pass.cpp#L864-L899) — `LowerTensorAllReduceRule`：InCore 路径强制显式 signal（`CHECK_SPAN(args.size() == 2, ...)`，把缺 signal 当**用户错误**报出可读信息）；`mode` 是公开 DSL kwarg，未知值显式拒绝（L894-L896）；`mode == "ring"` 则转交给 ring 规则（L897-L899）。
- [src/ir/transforms/lower_composite_ops_pass.cpp:L142-L143](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/lower_composite_ops_pass.cpp#L142-L143) — `kAllReduceChunkBytes = 16 KiB` 与 32 字节 Tile 对齐：内建按 UB 尺寸分块，这就是 IR diff 里「你的 mesh 被 chunk 化」的来源。
- [src/ir/transforms/lower_composite_ops_pass.cpp:L981](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/lower_composite_ops_pass.cpp#L981) 与 [L1034-L1036](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/lower_composite_ops_pass.cpp#L1034-L1036) — ready barrier（代数 1）之后按列 chunk 循环：每个 chunk 内 `tile.load` → 逐 peer `remote_load` 累加 → 完成屏障（代数 `1+k`）→ `tile.store`。L1031-L1033 的注释解释了为什么每个 chunk 写回前必须再 barrier：快 rank 可能在慢 peer 还没读走该 chunk 前就覆写它。
- [src/ir/transforms/lower_composite_ops_pass.cpp:L215-L226](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/lower_composite_ops_pass.cpp#L215-L226) — `ValidateMeshSignalShape`：mesh 信号必须 2D 且 `shape[1] == 1`；静态的 ring 形状信号会被明确拒绝（「不能与 ring 共用一个信号窗」）。

**降级规则：ring 展开与「每轮全网格 barrier」。**

- [src/ir/transforms/lower_composite_ops_pass.cpp:L1159-L1194](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/lower_composite_ops_pass.cpp#L1159-L1194) — `LowerTensorRingAllReduceRule`：ring 信号必须是 INT32、2D `[2*(NR-1), NR]`；两个维度都是编译期常量时交叉校验 `shape[0] == 2*(shape[1]-1)`（写错成 `3*(NR-1)` 会在运行期产生越界行索引，这里提前拦下）。
- [src/ir/transforms/lower_composite_ops_pass.cpp:L1311-L1316](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/lower_composite_ops_pass.cpp#L1311-L1316) — **本讲最重要的一处代码**：ring 规则里的 `emit_barrier` lambda 直接调 `EmitNotifyAll` + `EmitWaitAll`——即每个子块走的是**全网格** barrier（notify 所有 peer、wait 所有 peer），不是 step 10 的「notify 右 / wait 左」。这就是 IR diff 里同步差异的代码出处。
- [src/ir/transforms/lower_composite_ops_pass.cpp:L351-L371](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/lower_composite_ops_pass.cpp#L351-L371) 与 [L405-L425](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/lower_composite_ops_pass.cpp#L405-L425) — `EmitNotifyAll` / `EmitWaitAll`：`for peer in 0..nranks: if peer != my_rank: notify/wait`——正是手写 mesh 阶段 2 的机器生成版。
- [src/ir/transforms/lower_composite_ops_pass.cpp:L467-L479](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/lower_composite_ops_pass.cpp#L467-L479) 与 [L496-L519](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/lower_composite_ops_pass.cpp#L496-L519) — `EmitBarrier`（代数自增 + AtomicAdd1/WaitGe(g)）与 `EmitEpilogueReset`（对每个非自身格子 `AtomicAdd(-total)`，自清零）。L1311 上方的 `EmitBarrier` 注释还规定：它只能用于规则的直线代码，**循环里的 barrier 必须手工发 notify/wait 并用调用局部的 expected 值**——ring/mesh-chunk 规则正是这么做的。

**host 路径（Pass 42）与两条路径的能力差。**

- [docs/en/dev/passes/42-lower_host_tensor_collectives.md:L42-L45](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/passes/42-lower_host_tensor_collectives.md#L42-L45) — host 编排层的 `mode` 分发：默认 `mode="mesh"` 降到 `builtin.tensor.allreduce`，`mode="ring"` 降到 `builtin.tensor.allreduce_ring`，其它值按用户错误拒绝；文档同时写明「InCore allreduce 调用继续走 LowerCompositeOps」。
- [docs/en/dev/passes/42-lower_host_tensor_collectives.md:L137-L151](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/passes/42-lower_host_tensor_collectives.md#L137-L151) — host 内建 ring 的信号是 `[2*(NR-1)+1, NR]`（多一行返回 barrier），且**目前只支持 `Sum` + `FP32`、最多 16 个设备**、要求 `numel % NR == 0`、src 形状必须静态可知。
- [docs/en/dev/passes/42-lower_host_tensor_collectives.md:L85-L97](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/passes/42-lower_host_tensor_collectives.md#L85-L97) — 打印形态 `pl.builtin.tensor.allreduce(data, signal, op=0, dtype=pl.FP32, core_num=1, attrs={...})`：`builtin.tensor.*` 是 `internal_only` 算子，无 DSL 包装，但打印器能写出、解析器能读回（u3-l1 讲过的 `pl.builtin.<ns>.<op>` 回读路径）。

**reveal 文档的 IR diff 一节（教学核心）。**

- [docs/en/user/distributed/16-allreduce_reveal.md:L77-L96](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/user/distributed/16-allreduce_reveal.md#L77-L96) — 原文结论：`--mode mesh` 展开成 step 08 的模式（ready barrier + remote_load 累加块，可能被 chunk 化到 UB 尺寸）；`--mode ring` 展开成 step 10 的形状（`2*(nr-1)` 轮、`N/P` 块），**但每轮降级为全网格 barrier（`EmitNotifyAll`/`EmitWaitAll`）而非邻居就绪握手**——「same schedule and chunks, different synchronization」。cost card：算法由内建选择，模式仍由你选择。
- [docs/en/user/distributed/16-allreduce_reveal.md:L98-L113](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/user/distributed/16-allreduce_reveal.md#L98-L113) — 踩坑表：「Fatal pitfall — 模式用错信号形状」（ring 严格校验 `[2*(nr-1), nr]`；mesh 只静态校验列数为 1，不校验行数）、「内建 ring 比 hand-roll 慢 → 每轮 O(P²) 的全网格 barrier vs 你的 O(P) 邻居握手，当每轮同步开销要紧时用手写 ring」、「结果未归约 → 忘了 `data =` 重绑定」。

#### 4.4.4 代码实践

**实践目标**：两个模式各跑 P=2/P=4，然后做本讲的核心动作——**dump 内建 ring 的降级 IR，与手写 ring 逐段对照**。

1. 运行四种组合（CI 同款，[.github/workflows/ci.yml:L596-L599](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/.github/workflows/ci.yml#L596-L599)）：

   ```bash
   python examples/distributed/11_allreduce_reveal.py -p a2a3sim -d 0,1
   python examples/distributed/11_allreduce_reveal.py -p a2a3sim -d 0,1,2,3
   python examples/distributed/11_allreduce_reveal.py -p a2a3sim -d 0,1 --mode ring
   python examples/distributed/11_allreduce_reveal.py -p a2a3sim -d 0,1,2,3 --mode ring
   ```

   预期均打印 `OK`（[docs/en/user/distributed/16-allreduce_reveal.md:L28-L39](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/user/distributed/16-allreduce_reveal.md#L28-L39)）。
2. 拿到降级 IR：示例自带 `--compile-only`，会打印产物目录；`ir.compile` 的 `dump_passes` 默认为 `True`（[python/pypto/ir/compile.py:L198](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/ir/compile.py#L198)），逐 Pass IR 落在 `<output_dir>/passes_dump/`（[python/pypto/ir/compile.py:L366-L367](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/ir/compile.py#L366-L367)），文件名形如 `NN_after_<pass_name>.py`（[python/pypto/ir/pass_manager.py:L470-L479](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/ir/pass_manager.py#L470-L479)）。InCore allreduce 在 `lower_composite_ops` 这一步展开（Default 策略的第 12 个 Pass）：

   ```bash
   python examples/distributed/11_allreduce_reveal.py -p a2a3sim -d 0,1,2,3 --mode ring --compile-only
   # 记下打印的 output_dir，然后：
   ls <output_dir>/passes_dump/ | grep lower_composite   # 定位展开前后的两个快照
   ```

   对 `10_allreduce_ring.py` 重复同样的 `--compile-only`。
3. 需要观察的现象（写进对照笔记的三栏：手写 ring ↔ 内建 ring 的 IR）：
   - **轮次结构**：两者都应出现 `2*(P-1)` 个轮次的循环（P=4 即 6 轮），信号形状同为 `[6, 4]`；
   - **同步形态**：手写版每轮是「1 个 `pld.system.notify(peer=right, ...)` + 1 个 `pld.system.wait(offsets=[round, left], ...)`」；内建版每轮应看到 notify/wait **循环遍历所有 peer**（`for peer in ...: if peer != my_rank: ...` 的展开形态，即 `EmitNotifyAll`/`EmitWaitAll` 的产物）；
   - **尾声**：内建版多出一块「从信号格子减回去」的尾声（`EmitEpilogueReset`），手写版没有——因为手写的每行只用一次；
   - **分块**：内建版把目标视为一条线性流、按 ≤16 KiB 的均衡边界切块；手写版的 chunk 就是 `SIZE // nr`。
4. 预期结果：笔记能回答——「内建 ring 的每轮搬运对应手写版的哪一段？哪一处不同？为什么文档说手写 ring 在每轮同步开销要紧时更快？」。上述 IR 形态描述源自降级源码的静态阅读（4.4.3 引用的行号）；你机器上 dump 出的具体变量名与行号以实际文件为准（**待本地验证**）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 step 11 的工厂要折叠 `mode`，而 `nr`「本可保持动态」？

**答案**：内建拥有 chunking，`SIZE // nr` 之类的 Tile 形状不再出现在用户源码里，所以没有任何编译期常量依赖 `nr`（文档还注明 ring 布局用 `pl.dynamic` rank 数也能编译并通过 golden）。但 `mode` 是一个**字符串开关**，它同时选择降级规则（mesh 规则 vs ring 规则）和信号注解的形状——`[nr, 1]` 与 `[2*(nr-1), nr]` 是两个拓扑不同的形状，不是同一形状的两个长度，无法用 `pl.dynamic` 表达。把 `nr` 一并折进工厂只是为了让一份源码能拼出两种布局。

**练习 2**：内建 ring 与手写 ring 的信号形状一致吗？host 内建 ring 呢？

**答案**：InCore 复合路径（step 11 走的）与手写 ring 一致，都是 `[2*(NR-1), NR]`（降级代码 L1179-L1194 校验）。**host** 内建 ring 是 `[2*(NR-1)+1, NR]`——多出的一行用于返回 barrier（Pass 42 文档 L137-L139）。所以「同一个 allreduce」在不同层写的信号形状不同，不能把 host 层的信号窗直接拿来喂 InCore 调用。

**练习 3**：什么时候应该放弃内建、回到手写 ring？

**答案**：当**每轮同步开销**成为瓶颈时。内建 ring 每轮发全网格 barrier：每 rank 每轮 O(P) 个信号、全网 O(P²)；手写 ring 每轮只有 1 发 1 等（邻居就绪握手）。官方踩坑表（16-allreduce_reveal.md L110）给出的正是这条判据。反过来，小消息场景（≲16 KiB）用 mesh 更合适（L113）——模式的选择仍是你的责任，内建只负责把选定的模式正确展开。

## 5. 综合实践

把本讲四个示例串成一次完整实验，产出一页「all-reduce 三算法 + 内建」对照笔记。

**任务**：

1. **正确性矩阵**。在模拟器上以 `P=2` 与 `P=4` 各运行一次：

   ```bash
   for ex in 08_allreduce_mesh 09_allreduce_two_phase 10_allreduce_ring; do
     python examples/distributed/$ex.py -p a2a3sim -d 0,1
     python examples/distributed/$ex.py -p a2a3sim -d 0,1,2,3
   done
   python examples/distributed/11_allreduce_reveal.py -p a2a3sim -d 0,1
   python examples/distributed/11_allreduce_reveal.py -p a2a3sim -d 0,1,2,3
   python examples/distributed/11_allreduce_reveal.py -p a2a3sim -d 0,1 --mode ring
   python examples/distributed/11_allreduce_reveal.py -p a2a3sim -d 0,1,2,3 --mode ring
   ```

   记录 8 个结果（应全为 `OK`；任何失败都先核对是否踩了整除/信号形状约束）。同时解释：为什么 `P=2` 那一列无法区分 mesh / 两阶段 / ring 三种算法，而 `P=4` 那一列可以？

2. **cost card 表**。按 `SIZE=64`、FP32、`P=4` 填完下表（字节按元素数 × 4 折算）：

   | 算法 | 每 rank 远程读取（元素） | 轮次 / 同步 | 信号形状 |
   | --- | --- | --- | --- |
   | mesh（08） | \(3 \times 64 = 192\) | 1 barrier + 3 读 | `[P, 1]` |
   | 两阶段（09） | \(2 \times 3 \times 16 = 96\) | 2 barrier | `[2, P]` |
   | ring（10） | \(6 \times 16 = 96\) | 6 轮 × 邻居握手 | `[2(P-1), P]` |
   | 内建 mesh（11） | 同 mesh（按 16 KiB 上限分块，此处单块） | 每块 ready+完成 barrier + 尾声 | `[P, 1]` |
   | 内建 ring（11） | 同 ring | 6 轮 × **全网格** barrier + 尾声 | `[2(P-1), P]` |

3. **IR 对照（核心）**。对 `10_allreduce_ring.py` 与 `11_allreduce_reveal.py --mode ring` 各做一次 `--compile-only`（P=4），在各自 `<output_dir>/passes_dump/` 里定位 `lower_composite_ops` 的 after 快照，逐段回答：
   - 内建 ring 的 RS 轮循环与手写版 `for s in pl.range(nranks - 1)` 如何对应？轮内 `remote_load(peer=left)` 变成了什么？
   - 每轮的同步语句有几条 notify / 几条 wait，分别指向哪些 peer？与手写版「1 发右 / 1 等左」的差异在哪一行 IR 上最直观？
   - 内建版末尾多出的「减法尾声」在 IR 里长什么样？手写版为什么没有？
   - 两者的信号形状注解是否都是 `[6, 4]`？
4. **收尾判断**：用一句话回答「 PyPTO 的内建 allreduce 替我做了什么、没替我做什么」——参考答案：替你选定了调度（mesh/ring 的分块与轮次）、承担了 barrier 与信号信用管理（自清零）、并保证与手写一致的 golden；没替你做的：stage-in/stage-out 仍要自己写、`mode` 仍要自己选、每轮同步的开销差异（全网格 vs 邻居）仍要自己权衡。

所有运行结论以你机器为准；上文「应打印 OK」的预期来自官方 walkthrough 与 CI 配置的既有行为（**待本地验证**）。

## 6. 本讲小结

- **四阶段骨架**：stage-in → barrier → accumulate → stage-out 是所有手写集合通信的公共形状；barrier 必须放在任何远程读之前，否则 `remote_load` 与对端的 `pl.store` 竞态，且 P=2 常常掩盖、P=4 才显形。
- **三种算法的账单**：mesh 每 rank \((P-1)N\)（最简、轮次重）；两阶段 \(2(P-1)N/P\)（省一半、多一个 barrier）；ring 总量同两阶段但切成 \(2(P-1)\) 轮 × \(N/P\)，弱扩展下每轮定量、同步只在邻居间。
- **信号纪律**：计数器单调 ⇒ **一轮一行**。mesh 用 `[P, 1]`，两阶段 `[2, P]`，ring `[2(P-1), P]`；共用一行会让后一个 barrier 被残留计数立即放行。ring 的下标取模一律写 `(x - k + nranks) % nranks` 防止负 dividend。
- **Tile 形状决定工厂**：chunk 作为 `pl.load`/`remote_load` 的 shape 时必须编译期已知——steps 09/10 因此需要 rank 数工厂；08 的信号行数不是 Tile 形状，保持 `pl.dynamic` 即可；11 的内建自己拥有 chunking，真正必须固定的是 `mode`。
- **内建 `pld.tensor.allreduce`**：一个调用承担 barrier + 跨 rank 归约 + 写回；InCore 路径由 Pass 12 `LowerCompositeOps` 展开（mesh：16 KiB 分块 + 每块双 barrier；ring：线性流 + \(2(P-1)\) 轮），host 路径由 Pass 42 改写为 `builtin.tensor.allreduce[_ring]` 派发（ring 信号多一行、仅 Sum+FP32、≤16 设备）。
- **IR diff 的教学点**：内建 ring 的调度与分块和你手写的一致，但每轮是**全网格 barrier**（`EmitNotifyAll`/`EmitWaitAll`）而非邻居就绪握手，且多出自清零尾声（`EmitEpilogueReset`）以支持信号跨调用复用——每轮同步开销要紧时，手写 ring 仍是正解。

## 7. 下一步学习建议

- **补齐分布式阶梯的其余步骤**：`docs/en/user/distributed/05-tutorials.md` 的 12–16 步覆盖 barrier/broadcast/allgather 等其余集合通信的「先手写后揭示」对照；你现在已经掌握了读这套阶梯的方法（示例 + walkthrough + cost card + 踩坑表）。
- **读内建降级的姊妹规则**：在 [src/ir/transforms/lower_composite_ops_pass.cpp](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/lower_composite_ops_pass.cpp) 里对照 `barrier` / `broadcast` / `reduce_scatter` 的规则，它们与 allreduce 共用同一套 `EmitNotifyAll`/`EmitWaitAll`/`EmitEpilogueReset` 基础设施。
- **系统测试对照**：`tests/st/distributed/collectives/`（如 `test_l3_allreduce.py`、`test_l3_allreduce_ring.py`、`test_l3_tensor_allreduce_intrinsic.py`）是本讲四个示例的测试化版本，可作为「同一算法的第二种写法」参照。
- **进入性能调优**：下一讲 u7-l4 讲解 Split-K、自动 matmul 分块与泳道图/关键路径分析——all-reduce 的 cost card 思维（流量、轮次、每轮尺寸）会直接迁移到「依赖受限还是资源串行受限」的判断上。
- 若要深入 host 路径的机器面：回看 u3-l1 讲过的 `pl.builtin.<ns>.<op>` 打印-再解析路径，Pass 42 文档的「Printed form」一节给出了 `pl.builtin.tensor.allreduce` 的具体形态。
