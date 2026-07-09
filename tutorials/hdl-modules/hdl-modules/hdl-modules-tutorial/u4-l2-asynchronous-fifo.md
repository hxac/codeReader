# 异步 FIFO：双时钟与指针跨域

## 1. 本讲目标

本讲承接 u4-l1（同步 FIFO）与 u3-l1（CDC 基础），把 FIFO 从「单时钟」推进到「双时钟」。读完本讲你应该能够：

- 说清楚为什么异步 FIFO 不能直接拿二进制读写指针跨时钟域，而必须改用格雷码指针 + `resync_counter`。
- 看懂 `asynchronous_fifo.vhd` 里写域、读域、存储块、输出寄存器四个部分的分工，以及它们各自跑在哪个时钟上。
- 理解异步 FIFO 的 `write_level` / `read_level` 为什么只是「方向性安全」的近似值（写侧偏高、读侧偏低），以及在 packet 模式下 `read_level` 为何被强制清零。
- 掌握 `fifo_wrapper` 如何用同一个实体 + 一个 `use_asynchronous_fifo` generic，在「直通 / 同步 / 异步」三种模式间切换。
- 对照 `asynchronous_fifo.tcl` 解释为什么当 RAM 被实现成 LUTRAM（分布式 RAM）时，要对读数据寄存器设 `false_path`。

---

## 2. 前置知识

本讲默认你已经读过 u4-l1（同步 FIFO 的 AXI-Stream 式 ready/valid 接口、环形 RAM、用「多一位 MSB」区分满空、`enable_last`/`enable_packet_mode` 等 generic）以及 u3-l1（亚稳态、`async_reg` 同步链、`resync_counter` 用格雷码同步「每次只 ±1」的计数器）。下面把最关键的几条复习一遍。

**ready/valid 握手**：`valid`（主→从）不得组合依赖 `ready`，`ready`（从→主）可组合依赖 `valid`；二者同拍同时为 1 才完成一次 beat。FIFO 的 `write_ready='0'` 表示满，`read_valid='0'` 表示空。

**亚稳态与同步链**：跨时钟域的信号无法被「完美」采样，只能用两级 `async_reg` 寄存器把平均无故障时间（MTBF）拉到可接受。单比特电平可直接走同步链；多比特向量则不能逐位同步，否则会采到「新旧混杂」的脏值（u3-l2 讲的比特一致性风险）。

**格雷码**：相邻两个数的格雷码只有 1 位不同。`resync_counter` 正是利用这一点——只要输入计数器「每次只变化 ±1」，跨域采样时最多只有 1 位处于翻转态，目的域要么采到旧值要么采到新值，绝不会采到混杂值。它内部还有一条断言守护这一前提：

[modules/resync/src/resync_counter.vhd:89-92](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_counter.vhd#L89-L92) —— `hamming_distance(to_gray(counter_in), counter_in_gray) <= 1`，即每次采样到的格雷码与上次相比最多变 1 位。

这条断言是本讲全部指针跨域安全性的基石，下面会反复用到。

**同步 FIFO vs 异步 FIFO 的本质差异**：同步 FIFO 写读共用一个时钟，二进制读写指针在同一时钟域内直接比较，`level`「永远正确」；异步 FIFO 的写指针在写域、读指针在读域，二者必须跨越两个互不同步的时钟，于是引出本讲的全部难点。

---

## 3. 本讲源码地图

| 文件 | 作用 | 所属工程 |
|------|------|----------|
| `modules/fifo/src/asynchronous_fifo.vhd` | 双时钟 FIFO 主体：写域 / 读域 / 存储块 / 输出寄存器四块 | 综合 + 仿真 |
| `modules/fifo/src/fifo_wrapper.vhd` | 统一封装：按 generic 在「直通 / 同步 / 异步」间切换 | 综合 + 仿真 |
| `modules/fifo/src/fifo.vhd` | 同步 FIFO（u4-l1 已精读，本讲只做对照） | 综合 + 仿真 |
| `modules/resync/src/resync_counter.vhd` | 格雷码计数器跨域同步器（u3-l1 已精读，本讲作为被复用构件） | 综合 + 仿真 |
| `modules/fifo/scoped_constraints/asynchronous_fifo.tcl` | 异步 FIFO 的作用域约束，处理 LUTRAM 读数据路径 | 仅综合 |
| `modules/resync/scoped_constraints/resync_counter.tcl` | `resync_counter` 的 `set_bus_skew` / `set_max_delay` 约束（u3-l1/u3-l2 已讲） | 仅综合 |
| `modules/fifo/test/tb_asynchronous_fifo.vhd` | 异步 FIFO 仿真：两个不同频率时钟 + 随机背压 + 数据校验 | 仅仿真 |
| `modules/fifo/module_fifo.py` | 把异步 FIFO 的 generic 组合与资源占用纳入 VUnit / netlist 回归 | 工具脚本 |

---

## 4. 核心概念与源码讲解

### 4.1 双时钟结构与读写指针的格雷码跨域

#### 4.1.1 概念说明

异步 FIFO 要解决的问题是：让一个时钟域（写域 `clk_write`）持续把数据塞进一块 RAM，让另一个频率/相位都不同的时钟域（读域 `clk_read`）持续把数据取出来，且不丢、不重、不乱。

难点不在 RAM 本身（RAM 的写口跑写时钟、读口跑读时钟，这在 FPGA 双口 BRAM 上是原生支持的），而在**满和空的判定**：

- 「满」要由写域判断：写域需要知道读指针走到哪了。
- 「空」要由读域判断：读域需要知道写指针走到哪了。

也就是说，**读指针要被搬进写域，写指针要被搬进读域**。而读写指针都是多比特向量。按 u3-l2 的结论，多比特向量不能逐位挂 `async_reg` 直接同步——否则会采到比特混杂的脏值。

标准解法（Clive Cummings 的经典异步 FIFO 结构，本项目正是采用）是：**把二进制指针转成格雷码再跨域**。因为相邻指针的格雷码只差 1 位，跨域时无论采到旧值还是新值都是合法的指针值，再转回二进制即可。这恰好就是 `resync_counter` 提供的能力。所以异步 FIFO 的指针跨域，就是「二进制指针 → 格雷码 → 两级 async_reg 同步链 → 格雷码 → 二进制」一条流水线。

#### 4.1.2 核心流程

异步 FIFO 的数据/控制流向可以画成下面这样（`→` 表示跨时钟域）：

```
        写域 clk_write                        读域 clk_read
   ┌──────────────────────┐              ┌──────────────────────┐
   │  write_addr (二进制)  │              │  read_addr (二进制)   │
   │        │              │              │        │              │
   │        ▼              │              │        ▼              │
   │   写 RAM (写口)        │              │   读 RAM (读口)       │
   │                       │              │                       │
   │  read_addr_resync ◀───┼── resync_counter(格雷码) ◀── read_addr_next
   │  (判定「满」)          │              │  (判定「空」)          │
   │                       │              │                       │
   │  write_addr ──┐       │              │  write_addr_resync ◀──┘
   │               │       │              │
   └───────────────┼───────┘              └──────────────────────┘
                   │
                   └── resync_counter(格雷码) ──→ write_addr_resync
```

关键三点：

1. **写域用 resync 过来的读指针**判定「满」（`write_ready`）。
2. **读域用 resync 过来的写指针**判定「空」（`read_valid`）。
3. 每条跨域路径都是一个 `resync_counter` 实例，依赖格雷码保证安全性。

> ⚠️ 重要推论：因为格雷码跨域依赖「每次只变 1 位」，所以**指针必须单调连续地 ±1**。这就强制要求 RAM 深度是 2 的幂——否则指针从最大值回绕到 0 时会一次翻转多位，破坏格雷码的单步性质。这正是 `asynchronous_fifo.vhd` 里那条 `is_power_of_two` 断言的来历（见 4.1.3）。

#### 4.1.3 源码精读

先看实体声明，注意它有两个时钟端口：

[modules/fifo/src/asynchronous_fifo.vhd:78-101](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/src/asynchronous_fifo.vhd#L78-L101) —— 写侧用 `clk_write`，读侧用 `clk_read`，两者完全独立。其余 generic 与同步 FIFO 一致（`width`/`depth`/`enable_last`/`enable_packet_mode`/`enable_drop_packet`/`enable_output_register`/`ram_type`）。

接着是那条关键的深度断言。`memory_depth` 是真正用于 RAM 的深度，开了输出寄存器时要减 1：

[modules/fifo/src/asynchronous_fifo.vhd:126](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/src/asynchronous_fifo.vhd#L126) —— `memory_depth := depth - to_int(enable_output_register)`。

[modules/fifo/src/asynchronous_fifo.vhd:150-152](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/src/asynchronous_fifo.vhd#L150-L152) —— `assert is_power_of_two(memory_depth) ... severity failure`。这就是「RAM 深度必须是 2 的幂」的硬约束。所以对用户而言：不开输出寄存器时 `depth` 取 2 的幂（如 16/64/1024）；开输出寄存器时 `depth` 取 2 的幂 +1（如 1025），使 `memory_depth` 仍是 2 的幂。这条约定与同步 FIFO 一致，但在异步 FIFO 里它是**安全性前提**而非仅仅是 BRAM 输出寄存器打包的优化技巧。

读写指针都带「多一位 MSB」用于区分满空，这点与同步 FIFO 相同：

[modules/fifo/src/asynchronous_fifo.vhd:128-131](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/src/asynchronous_fifo.vhd#L128-L131) —— `fifo_addr_t` 的位宽是 `num_bits_needed(2 * memory_depth - 1)`，即比寻址 RAM 所需多 1 位。

现在看**读指针如何被搬进写域**——这是判定「满」的前提。在 `write_block` 里实例化了一个 `resync_counter`：

[modules/fifo/src/asynchronous_fifo.vhd:252-262](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/src/asynchronous_fifo.vhd#L252-L262) —— `clk_in => clk_read, counter_in => read_addr_next, clk_out => clk_write, counter_out => read_addr_resync`。`resync_counter` 在内部把 `read_addr_next` 转成格雷码、过两级 `async_reg` 同步链、再转回二进制交给写域。

对称地，**写指针如何被搬进读域**——在 `read_block` 里（注意它被 `not enable_packet_mode` 门控，原因见 4.2）：

[modules/fifo/src/asynchronous_fifo.vhd:405-420](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/src/asynchronous_fifo.vhd#L405-L420) —— `resync_write_addr` 块，把 `write_addr`（写域）同步到 `write_addr_resync`（读域）。

存储块本身是双口的：写口跑写时钟、读口跑读时钟：

[modules/fifo/src/asynchronous_fifo.vhd:473-489](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/src/asynchronous_fifo.vhd#L473-L489) —— `write_memory` 进程 `wait until rising_edge(clk_write)`，`read_memory` 进程 `wait until rising_edge(clk_read)`。两进程共享同一个 `mem` 数组，这正是双口 RAM 的行为模型，会被 Vivado 推断为 BRAM 或 LUTRAM。

最后，把这一切串起来的拓扑图，实体头注释里也有一张图（`asynchronous_fifo_circuit.png`）和一段说明：

[modules/fifo/src/asynchronous_fifo.vhd:9-18](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/src/asynchronous_fifo.vhd#L9-L18)。

#### 4.1.4 代码实践

**实践目标**：直观感受「两个不同频率的时钟分别写读、数据不丢失」。

**操作步骤**：

1. 打开 `modules/fifo/test/tb_asynchronous_fifo.vhd`，看它如何产生两个不同频率的时钟：

   [modules/fifo/test/tb_asynchronous_fifo.vhd:86-92](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/test/tb_asynchronous_fifo.vhd#L86-L92) —— 由 `read_clock_is_faster` 选择：读快写慢时 `clk_read` 半周期 2 ns、`clk_write` 半周期 3 ns；反之亦然。两时钟频率不同且**完全异步**（无固定相位关系）。

2. 看校验机制：写侧 BFM（`axi_stream_master`）把随机数据塞进 `write_queue`，读侧 BFM（`axi_stream_slave`）从同一个 queue 取期望值逐拍比对，任何丢失/重复/乱序都会让 `check` 报错：

   [modules/fifo/test/tb_asynchronous_fifo.vhd:413-451](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/test/tb_asynchronous_fifo.vhd#L413-L451)。

3. （如本地已装好 VUnit + Vivado/GHDL）运行该 testbench，例如：
   ```bash
   python tools/simulate.py fifo --test test_write_faster_than_read
   python tools/simulate.py fifo --test test_read_faster_than_write
   ```
   两个用例分别对应「写快读慢（FIFO 会写满）」和「读快写慢（FIFO 会读空）」，都用随机背压。

**需要观察的现象**：`test_write_faster_than_read` 末尾断言 `has_gone_full_times > 200`（FIFO 真的被写满过很多次）；`test_read_faster_than_write` 断言 `has_gone_empty_times > 200`（FIFO 真的被读空过很多次）。这说明两个方向都经历了满/空边界，而校验 queue 仍然全部通过——即「无数据丢失」。

[modules/fifo/test/tb_asynchronous_fifo.vhd:192-206](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/test/tb_asynchronous_fifo.vhd#L192-L206) —— 两个边界覆盖断言。

**预期结果**：仿真全部通过。**若本地无法运行仿真**，本实践退化为「源码阅读型」：在 testbench 里追踪一次 `run_test` 调用，确认写侧把数据 push 进 `write_queue`、读侧从 `read_queue` 比对，从而从测试结构上确信「数据完整性是被自动校验的」。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `memory_depth` 设成非 2 的幂（例如 `depth=>24` 且不开输出寄存器），会发生什么？

**答案**：`is_power_of_two` 断言以 `severity failure` 触发，仿真/综合直接报错停止（见 `asynchronous_fifo.vhd:150-152`）。根本原因是格雷码指针回绕时无法保持「单比特翻转」，会破坏 `resync_counter` 的安全性前提。

**练习 2**：为什么写域判定「满」用的是 resync 过来的读指针，而不是直接用读域的读指针？

**答案**：因为读指针存在于读时钟域，写域无法直接读取它而不冒亚稳态风险。必须经由 `resync_counter` 跨域。代价是写域看到的读指针是「过时的」（落后于真实值），因此判定出的「满」是悲观的——宁可少写也不会溢出，这正是我们想要的安全方向（详见 4.2）。

---

### 4.2 满/空判定与电平信号的方向性安全

#### 4.2.1 概念说明

4.1 解决了「指针怎么跨域」，本节解决「跨域过来的指针是过时的，怎么保证不溢出、不空读」。结论是一句话：**所有跨域指针都是陈旧值，但工程上把它们设计成「朝安全方向偏差」**。

- 写域用 `read_addr_resync` 判满。由于读指针可能已经前进（读得更多了），写域看到的「读位置」偏旧，算出来的剩余空间偏小 → **判出的「满」偏悲观**（宁可误报满、少写一拍，绝不溢出）。
- 读域用 `write_addr_resync` 判空。由于写指针可能已经前进（写得更多了），读域看到的「写位置」偏旧，算出来的可读数据偏少 → **判出的「空」偏悲观**（宁可误报空、少读一拍，绝不空读）。

这种「单向偏差」就是所谓的**方向性安全（directional safety）**。它带来的副产物是：`write_level` / `read_level` 这两个状态信号只能给出**近似值**，而且不 deterministic——它们随每次跨域采样的时机而轻微抖动。

#### 4.2.2 核心流程

满/空判定与电平的逻辑：

```
写域:
  write_addr_next      = write_addr + (本次是否写入)
  read_addr_resync     = resync(read_addr_next)   # 陈旧、偏小
  write_level          = (write_addr_next - read_addr_resync) mod 2*depth   # 偏高（悲观）
  write_ready (非满)   = 低位地址不同 OR 高位(MSB)相同
  → write_level 永远 >= 真实剩余量

读域:
  write_addr_resync    = resync(write_addr)        # 陈旧、偏小
  read_level           = (write_addr_resync - read_addr_next) mod 2*depth   # 偏低（悲观）
  read_valid (非空)    = read_level /= 0
  → read_level 永远 <= 真实可读量
```

两个方向都「宁可少做」，于是既不会溢出也不会空读。代价是吞吐在边界附近略有损失、`level` 数值有抖动。

注意满的判定里同时比较「低位地址」和「最高位 MSB」——这正是同步 FIFO 里「多一位 MSB 区分满空」技巧的复用：当地址低位全等时，看 MSB 是否相同，相同则空、不同则满。

#### 4.2.3 源码精读

先看**写域判满与 write_level**：

[modules/fifo/src/asynchronous_fifo.vhd:215-221](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/src/asynchronous_fifo.vhd#L215-L221) —— `write_ready` 的赋值。注意它比较的是 `read_addr_resync(bram_addr_range)`（低位）是否等于 `write_addr_next_if_not_drop` 的低位，**或** MSB 位相同。两者结合给出「非满」。代码注释还特意说明这里刻意用 `write_addr_next_if_not_drop` 而非最终指针，是为了把 `write_ready`（常常是关键路径）的时序做得更松。

[modules/fifo/src/asynchronous_fifo.vhd:236-242](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/src/asynchronous_fifo.vhd#L236-L242) —— `write_level` 的赋值与一段关键注释：*「write_level must never be less than the actual number of words stored in the FIFO」*。因为 `read_addr_resync` 偏旧，减出来的 level 偏大——这正是「写侧偏高」的方向性安全。若开了输出寄存器，还会再 `+1`（见 4.2.3 末尾与实体注释里的警告）。

再看**读域判空与 read_level**，以及一段很重要的解释为什么 packet 模式下 `read_level` 不可用：

[modules/fifo/src/asynchronous_fifo.vhd:284-317](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/src/asynchronous_fifo.vhd#L284-L317) —— `read_level_next := (write_addr_resync - read_addr_next) mod (2*memory_depth)`。由于 `write_addr_resync` 偏旧，读侧 level 偏低（悲观），方向安全。这段代码被 `if not enable_packet_mode` 门控。

那么 packet 模式下用什么判「有整包可读」？答案是**同步 lasts 的计数**，而不是写地址本身。packet 模式开启时，跨域搬的是 `num_lasts_written`（写域累计写了多少个 `last`/包尾），读域拿它和本地 `num_lasts_read` 比较：

[modules/fifo/src/asynchronous_fifo.vhd:425-440](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/src/asynchronous_fifo.vhd#L425-L440) —— `resync_num_lasts_written` 块（仅在 packet 模式生成）：把写域的 `num_lasts_written` 同步到读域 `num_lasts_written_resync`。

[modules/fifo/src/asynchronous_fifo.vhd:319-352](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/src/asynchronous_fifo.vhd#L319-L352) —— packet 模式下 `read_valid_ram_pre` 由 `num_lasts_read` 与 `num_lasts_written_resync` 是否相等决定（「整包到齐才可读」，与同步 FIFO 的 packet 模式语义一致，u4-l1 已讲）。

为什么 packet 模式不提供 `read_level`？实体端口注释说得很直白：

[modules/fifo/src/asynchronous_fifo.vhd:109-120](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/src/asynchronous_fifo.vhd#L109-L120) —— `read_level` 在 packet 模式下**恒为 0**，因为这种模式下无法保证一个无毛刺的 `read_level`。原因有二：① packet 模式根本不再 resync 写地址（4.1.3 的 `resync_write_addr` 块被关掉），没有数据来源；② 即便想算，`enable_drop_packet` 时写指针可能一次跳变多位，破坏格雷码前提，会产生毛刺。代码注释（`asynchronous_fifo.vhd:285-299`）对此有详细论述。

最后是**输出寄存器模式下 write_level 的悲观性**警告：

[modules/fifo/src/asynchronous_fifo.vhd:28-33](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/src/asynchronous_fifo.vhd#L28-L33) —— 开了 `enable_output_register` 后，FIFO 空时 `write_level` 会比真实值高 1（因为那一拍数据在输出寄存器里、不在 RAM 里，难以精确追踪）。所以代码干脆无条件 `+ to_int(enable_output_register)`，保持方向性安全。

> 资源佐证：`module_fifo.py` 里把这套行为量化进了 CI。同一个「最小异步 FIFO」（width=32, depth=1024）开 packet 模式后，LUT 从 68 降到 60，但 FF 从 112 涨到 123——涨的部分正是多出来的那个 `num_lasts_written` 的 `resync_counter`：
>
> [modules/fifo/module_fifo.py:461-479](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/module_fifo.py#L461-L479) —— 注释明确写道 packet 模式「increases resource utilization quite a lot since another resync_counter is added」。

#### 4.2.4 代码实践

**实践目标**：用源码阅读验证「方向性安全」的两个方向，并理解一次完整的写→读往返延迟。

**操作步骤**：

1. 在 `asynchronous_fifo.vhd` 中定位 `write_level`（`asynchronous_fifo.vhd:240-242`）与 `read_level`（`asynchronous_fifo.vhd:301-302`）的赋值表达式，确认它们一个是 `write_addr_next - read_addr_resync`、一个是 `write_addr_resync - read_addr_next`，分别用了「陈旧的对方指针」。
2. 打开 `tb_asynchronous_fifo.vhd` 的 `test_levels_full_range` 用例：

   [modules/fifo/test/tb_asynchronous_fifo.vhd:308-331](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/test/tb_asynchronous_fifo.vhd#L308-L331) —— 注意写完之后要 `wait_for_write_to_propagate`（等 4 个读时钟，必要时再 +1）才能在**读侧**看到正确的 `read_level`；读空之后要 `wait_for_read_to_propagate`（等 4 个写时钟）才能在**写侧**看到正确的 `write_level`。这些等待函数本身就量化了「跨域 level 是慢慢传播、非确定性」的事实：

   [modules/fifo/test/tb_asynchronous_fifo.vhd:143-161](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/test/tb_asynchronous_fifo.vhd#L143-L161)。

**需要观察的现象**：写侧 `write_level` 在写满后立刻正确（本域自己算），但读空后需要等若干拍才回落到 `to_int(enable_output_register)`；读侧 `read_level` 反之。这正说明两个 level 分属不同时钟域、各自只对本域事件响应快、对对域事件响应慢。

**预期结果**：理解了为什么用户代码**不应**用异步 FIFO 的 `read_level` 做精确的门控（它有抖动/延迟），而应优先用 `almost_full` / `almost_empty` 这类单比特标志（它们只比较一次、可被安全采样）。**待本地验证**具体波形。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `write_level` 的注释强调「绝不能小于真实存量」？

**答案**：因为软件/上游可能依据 `write_level` 决定还能写多少。如果它报得比真实小（高估了已用空间），最多是少写；如果报得比真实大（低估了已用空间），上游会继续写导致溢出覆盖未读数据。所以必须「偏高/悲观」。

**练习 2**：packet 模式下，`resync_write_addr`（同步写地址）块被关掉，那读域怎么知道「有没有整包可读」？

**答案**：改用同步 `num_lasts_written`（写域累计的包尾数）。读域比较 `num_lasts_written_resync`（跨域来的）与本地 `num_lasts_read`，二者不等就说明 FIFO 里至少还有一整包，于是拉高 `read_valid`。见 `asynchronous_fifo.vhd:319-352` 与 `425-440`。

---

### 4.3 fifo_wrapper：一个实体切换同步/异步/直通

#### 4.3.1 概念说明

很多上层 IP 在不同应用里有时需要同步 FIFO、有时需要异步 FIFO、有时根本不需要 FIFO（直接直通）。如果为每种情况各写一个例化模板，代码会很啰嗦。`fifo_wrapper` 的思路是：**用同一个实体、同一个端口表，靠 generic 在三种模式间切换**，让上层只需要写一份例化代码。

它引入两个开关：

- `depth`：设为 0 表示完全不要 FIFO，读写口直接短接（直通）。
- `use_asynchronous_fifo`：`true` 用 `asynchronous_fifo`，`false` 用同步 `fifo`。

注意它的端口集是同步 FIFO 和异步 FIFO 的**并集**：同时提供 `clk`（同步用）和 `clk_write`/`clk_read`（异步用）。用不到的时钟就悬空（默认值 `'0'`）。

#### 4.3.2 核心流程

`fifo_wrapper` 的 `generate` 三分支：

```
if depth = 0:                      # 直通：不要 FIFO
    write_ready <= read_ready
    read_valid  <= write_valid
    read_data   <= write_data
    read_last   <= write_last

elsif use_asynchronous_fifo:        # 异步 FIFO
    例化 asynchronous_fifo
    （clk_write / clk_read 接上）

else:                               # 同步 FIFO
    例化 fifo
    （clk 接上）
```

注意一个细节：`fifo_wrapper` 的 generic 默认值必须与 `fifo.vhd` / `asynchronous_fifo.vhd` 严格一致，否则用户传 generic 时容易出现「你以为传给 wrapper 了、其实没透传到底层实体」的错位。源码头注释专门强调了这一点。

#### 4.3.3 源码精读

实体声明，注意三个时钟端口并存：

[modules/fifo/src/fifo_wrapper.vhd:20-72](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/src/fifo_wrapper.vhd#L20-L72) —— `clk`（同步用，必填）、`clk_write`/`clk_read`（异步用，默认 `'0'`）。注释（`fifo_wrapper.vhd:23-25`）强调默认值要与两个底层实体完全一致。

直通分支（`depth=0`），并附带一组断言禁止在直通下启用需要 FIFO 的特性：

[modules/fifo/src/fifo_wrapper.vhd:79-89](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/src/fifo_wrapper.vhd#L79-L89) —— 直接把读侧信号连到写侧输入，零延迟直通。

异步分支：

[modules/fifo/src/fifo_wrapper.vhd:93-131](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/src/fifo_wrapper.vhd#L93-L131) —— 例化 `asynchronous_fifo`，并断言 `not enable_peek_mode`（peek 模式只支持同步 FIFO）。注意端口名映射：wrapper 的 `almost_full` ←→ 底层的 `write_almost_full`，`almost_empty` ←→ `read_almost_empty`（因为这两个信号分属写/读域，命名上做了「域归属」标注，见 `fifo_wrapper.vhd:52-70` 的注释）。

同步分支：

[modules/fifo/src/fifo_wrapper.vhd:135-174](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/src/fifo_wrapper.vhd#L135-L174) —— 例化同步 `fifo`，并令 `write_level <= read_level`（同步 FIFO 只有一个 level，wrapper 对外仍提供两个名字以保持端口一致）。

netlist 资源回归用的顶层夹具 `fifo_netlist_build_wrapper`，正是包了一层 `fifo_wrapper`，只引出「裸」端口以得到最小化的资源数字：

[modules/fifo/rtl/fifo_netlist_build_wrapper.vhd:44-65](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/rtl/fifo_netlist_build_wrapper.vhd#L44-L65) —— 它把 `use_asynchronous_fifo` 透传给 `fifo_wrapper`，所以同一份夹具可同时度量同步与异步两种 FIFO。

#### 4.3.4 代码实践

**实践目标**：通过 netlist 资源数字，直观比较「同步 vs 异步」FIFO 的面积代价。

**操作步骤**：

1. 在 `module_fifo.py` 中找到两个「最小」配置：同步 `fifo.minimal`（width=32, depth=1024）与异步 `asynchronous_fifo.minimal`（width=32, depth=1024），二者都走 `fifo_netlist_build_wrapper` 顶层、都只引裸端口：

   同步：[modules/fifo/module_fifo.py:137-158](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/module_fifo.py#L137-L158) —— `TotalLuts(EqualTo(14)), Ffs(EqualTo(24))`。
   
   异步：[modules/fifo/module_fifo.py:375-396](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/module_fifo.py#L375-L396) —— `TotalLuts(EqualTo(44)), Ffs(EqualTo(90))`。

2. 算出差值：异步比同步多用了约 **30 LUT、66 FF**。

**需要观察的现象**：这多出来的资源几乎全部用在两/三个 `resync_counter`（每个含两级 `async_reg` 同步链 + 格雷码转换逻辑）以及跨域比较器上。BRAM 占用两者相同（都是 1 块 RAMB36），说明存储本身不因同步/异步而变贵——贵的全是 CDC 逻辑。

**预期结果**：理解「异步 FIFO 的面积代价 = CDC 同步逻辑」。这也是为什么能用同步 FIFO 就不要用异步 FIFO 的工程经验来源。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `fifo_wrapper` 的端口里同时有 `clk`、`clk_write`、`clk_read` 三个时钟？

**答案**：为了用同一份端口表覆盖两种 FIFO。选同步时只用 `clk`，`clk_write`/`clk_read` 悬空（默认 `'0'`）；选异步时只用 `clk_write`/`clk_read`，`clk` 不接。这样上层只写一份例化、用 generic 切换即可。

**练习 2**：在 `fifo_wrapper` 里，`almost_full` 和 `almost_empty` 分别映射到底层的哪个信号？为什么？

**答案**：`almost_full` ← `write_almost_full`（满是在写域判的，所以「快满」属于写域）；`almost_empty` ← `read_almost_empty`（空是在读域判的，所以「快空」属于读域）。见 `fifo_wrapper.vhd:118-130` 的端口映射与 `:52-70` 的域归属注释。

---

### 4.4 作用域约束：为什么读数据寄存器要设 false_path

#### 4.4.1 概念说明

异步 FIFO 的 RAM 数据通路有一条「看似违规」的时序路径：写口在 `clk_write` 写入、读口在 `clk_read` 读出，二者没有共同时钟，所以从 `clk_write` 到读侧数据寄存器之间**没有确定的 setup/hold 关系**。Vivado 默认会按常规路径检查，结果几乎必然报时序违例。

但这条路径在功能上是安全的：FIFO 的指针协议保证「一个 RAM 单元只有在写完至少一个完整来回之后才会被读」（读侧靠 `write_addr_resync` 判空，写满前不会读；写侧靠 `read_addr_resync` 判满，读走前不会覆盖）。所以读写永远不会同时碰同一个单元——这是协议级的安全，不需要时序级约束来保证。因此正确的做法是：**把这条路径声明为 false_path，让工具别检查它**。

这条 `false_path` 只在 RAM 被实现成 **LUTRAM（分布式 RAM）** 时才需要。原因是：LUTRAM 的读数据会落在普通的 FF 上（`memory_read_data_reg*`），这些 FF 有真实的、跨时钟域的 D 端路径；而 BRAM 的读数据寄存器在 BRAM 原语**内部**，工具不会把它当成跨域 FF 路径。

#### 4.4.2 核心流程

约束文件的执行逻辑：

```
1. 找到 clk_write 时钟对象
2. 找到 memory.memory_read_data_reg* 这些读数据寄存器单元
   （用 PRIMITIVE_GROUP 过滤，只在它们以 FF/LUTRAM 形式存在时命中）
3. 若两者都存在：
     set_false_path -setup -hold -from clk_write -to 读数据寄存器
     并针对 report_cdc 的 "CDC-26" 警告创建 waiver
```

注意：这条 `false_path` 只是「数据通路」的约束。指针跨域的安全性由另一套约束保证——即 `resync_counter.tcl` 里的 `set_bus_skew` + `set_max_delay -datapath_only`（u3-l1/u3-l2 已讲）。两者分工明确：**指针路径用有界延迟约束（max_delay），数据路径用无界 false_path**。

#### 4.4.3 源码精读

定位读数据寄存器单元：

[modules/fifo/scoped_constraints/asynchronous_fifo.tcl:16-22](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/scoped_constraints/asynchronous_fifo.tcl#L16-L22) —— 用 `get_cells -filter {PRIMITIVE_GROUP==FLOP_LATCH || PRIMITIVE_GROUP==REGISTER}` 抓 `memory.memory_read_data_reg*`。

注释解释为什么需要按原语类型过滤：

[modules/fifo/scoped_constraints/asynchronous_fifo.tcl:24-31](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/scoped_constraints/asynchronous_fifo.tcl#L24-L31) —— LUTRAM 时这些寄存器以普通 FF 形式存在，有跨域路径需要 ignore；BRAM 时读寄存器在 BRAM 原语内部。新版 Vivado（≥2023.2）即使 BRAM 也会列出这些单元，所以才用 `PRIMITIVE_GROUP` 过滤。

核心的 false_path 与 waiver：

[modules/fifo/scoped_constraints/asynchronous_fifo.tcl:32-49](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/scoped_constraints/asynchronous_fifo.tcl#L32-L49) —— `set_false_path -setup -hold -from ${clk_write} -to ${read_data}`，并用 `create_waiver -id "CDC-26"` 把 `report_cdc` 列出的「LUTRAM 读写潜在冲突」警告豁免掉，理由说明是「Read/write pointer logic guarantees no collision」（指针协议保证了无冲突）。

存储块里也有一条断言配合：禁止把异步 FIFO 的 RAM 实现成纯寄存器（`ram_style_registers`），因为那样约束就不成立了：

[modules/fifo/src/asynchronous_fifo.vhd:457-459](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/src/asynchronous_fifo.vhd#L457-L459) —— `assert ram_type /= ram_style_registers ... severity failure`。

对照指针路径的「有界延迟」约束（u3-l1/u3-l2 已精读，这里只点出分工）：

[modules/resync/scoped_constraints/resync_counter.tcl:35](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/scoped_constraints/resync_counter.tcl#L35) —— `set_bus_skew`：保证格雷码多比特采样时最多 1 位在翻转。

[modules/resync/scoped_constraints/resync_counter.tcl:50](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/scoped_constraints/resync_counter.tcl#L50) —— `set_max_delay -datapath_only`：给跨域延迟一个上界（取两时钟周期较小者），保证 MTBF。

实体头注释也反复强调「必须配合作用域约束文件」才能正常工作：

[modules/fifo/src/asynchronous_fifo.vhd:20-27](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/src/asynchronous_fifo.vhd#L20-L27) —— 指出本实体有 `asynchronous_fifo.tcl`，且其内部例化的 `resync_counter` 还有进一步的约束文件，两者都必须应用。

#### 4.4.4 代码实践

**实践目标**：对照 `asynchronous_fifo.tcl`，解释对 LUTRAM 读数据寄存器设 `false_path` 的原因。

**操作步骤**：

1. 读 `asynchronous_fifo.tcl:24-34` 的注释与 `set_false_path` 那一行。
2. 在 `asynchronous_fifo.vhd` 的 `memory` 块里找到 `memory_read_data` 的来源（`:484-489` 的 `read_memory` 进程），确认它是「读口在 `clk_read` 下把 RAM 内容读到 `memory_read_data` 寄存器」。
3. 追问：这个寄存器的 D 端数据来自 RAM 单元，而 RAM 单元由 `clk_write` 写入（`:473-480` 的 `write_memory` 进程）。于是存在一条 `clk_write → memory_read_data_reg` 的跨域路径。
4. 回答：「为什么设 false_path 是安全的？」——因为指针协议保证读写不会同时访问同一地址：读侧只有当 `write_addr_resync` 推进过来（说明写已发生且已跨域传播）才判非空并推进读指针；写侧只有当 `read_addr_resync` 推进过来才判非满并允许覆盖。所以等到某个地址被读时，对它的写早已完成多个时钟周期，不存在 setup/hold 意义上的冲突，时序检查没有意义反而会误报。

**需要观察的现象**：若**忘记**应用 `asynchronous_fifo.tcl`（也没有手动约束），综合后 `report_cdc` 会报 `CDC-26`（LUTRAM 读写潜在冲突），且时序会出现 `clk_write → memory_read_data_reg` 的违例。

**预期结果**：能用自己的话讲清楚「数据通路靠协议安全（所以 false_path）、指针通路靠格雷码 + 有界延迟安全（所以 bus_skew/max_delay）」这一分工。**待本地综合验证**（需 Vivado）。

#### 4.4.5 小练习与答案

**练习 1**：为什么这条 `false_path` 在 BRAM 实现时往往「不需要」、而在 LUTRAM 实现时需要？

**答案**：BRAM 的读数据寄存器在 BRAM 原语内部，不是普通 FF，工具不把它当跨域路径检查；LUTRAM 的读数据落在普通 FF（`memory_read_data_reg*`）上，D 端有真实的跨域路径会被检查并误报违例，所以需要 `false_path`。约束文件用 `PRIMITIVE_GROUP` 过滤来兼容两种情况（`asynchronous_fifo.tcl:18-31`）。

**练习 2**：数据路径用 `false_path`（无界），指针路径却用 `set_max_delay`（有界），为什么不对称？

**答案**：指针跨域若延迟无界，亚稳态会让 MTBF 不可接受，所以必须用 `set_max_delay -datapath_only` 给一个上界（并配合 `set_bus_skew` 保证格雷码多比特一致性）。数据路径则不同——它只承载「已确定写完」的内容，靠指针协议（而非时序）保证安全，延迟大一点只会损失一点最高吞吐、不会出错，所以可以无界 `false_path`。

---

## 5. 综合实践

把本讲四块知识串起来：**用 `fifo_wrapper` 实例化一个异步 FIFO，跟踪一次完整的「写一拍 → 跨域 → 读一拍」往返，并说明每一步的安全保证来自哪里。**

1. **例化**：写一份 `fifo_wrapper` 的例化，`use_asynchronous_fifo=true`、`width=32`、`depth=16`（注意 16 是 2 的幂，满足 `is_power_of_two` 断言）、`enable_last=true`、`enable_packet_mode=true`。把 `clk_write` 接 100 MHz、`clk_read` 接 150 MHz（两时钟异步）。

2. **写一拍**：在写域拉一拍 `write_valid` + `write_data` + `write_last`。在源码里确认这拍数据进入 `write_memory` 进程（`asynchronous_fifo.vhd:473-480`），同时 `num_lasts_written` 加 1（`:203-208`）。

3. **跨域（指针侧）**：`num_lasts_written` 经 `resync_num_lasts_written` 的 `resync_counter` 跨到读域（`:425-440`）。说明这一步的安全性来自格雷码 + `resync_counter.tcl` 的 `set_bus_skew`/`set_max_delay`。

4. **读侧判可读**：读域比较 `num_lasts_written_resync != num_lasts_read`，拉高 `read_valid_ram_pre`（`:335` 或 `:349`）。

5. **跨域（数据侧）**：读口在 `clk_read` 下从 RAM 读出数据到 `memory_read_data`（`:484-489`）。说明这条路径靠 `asynchronous_fifo.tcl` 的 `false_path` 免于时序误判，靠指针协议保证不冲突。

6. **读一拍**：读域拉 `read_ready`，完成一次握手，数据经 `handshake_pipeline`（可选输出寄存器，`:495-516`）送到 `read_data`。

7. **自检**：对照 4.2，解释为什么在此过程中 `write_level` 可能短暂偏高、而 packet 模式下 `read_level` 始终为 0。

**交付物**：一张标注了「哪段逻辑在哪个时钟域、靠哪种机制（格雷码 / false_path / 协议）保证安全」的数据流图。如果你装了 VUnit，可把上面的例化放进一个小 testbench，复用 `tb_asynchronous_fifo.vhd` 里 `axi_stream_master`/`axi_stream_slave` + queue 校验的模式，跑随机数据确认无丢失。

---

## 6. 本讲小结

- 异步 FIFO 的核心难题不是 RAM，而是**读写指针的跨域**。本项目用「二进制指针 → 格雷码 → `resync_counter` 两级 async_reg 同步链 → 格雷码 → 二进制」搬指针，依赖格雷码「相邻只差 1 位」保证采样安全。
- 为维持格雷码「每次只翻转 1 位」（含回绕），RAM 深度**必须是 2 的幂**（`is_power_of_two` 断言）；开输出寄存器时用户 `depth` 取 2 的幂 +1。
- 跨域指针都是**陈旧值**，工程上让它们朝安全方向偏差：`write_level` 偏高（防溢出）、`read_level` 偏低（防空读），这就是「方向性安全」。代价是两个 level 都有抖动、非确定性，用户应优先用 `almost_full`/`almost_empty` 单比特标志。
- packet 模式下不同步写地址，改同步 `num_lasts_written`（包尾计数）来判「整包可读」，并因此关闭 `read_level`（恒为 0），多耗一个 `resync_counter`。
- `fifo_wrapper` 用 `depth=0`（直通）/ `use_asynchronous_fifo`（同步 vs 异步）两个 generic，一个实体覆盖三种用法；netlist 回归量化出「异步比同步贵约 30 LUT + 66 FF」，贵的全是 CDC 逻辑。
- 约束分工：**指针路径**用 `resync_counter.tcl` 的 `set_bus_skew` + `set_max_delay -datapath_only`（有界，保 MTBF）；**数据路径**用 `asynchronous_fifo.tcl` 的 `false_path`（无界，靠指针协议保安全），且后者仅在 LUTRAM 实现时命中。

---

## 7. 下一步学习建议

- **横向延伸到 AXI CDC**：u5-l3（AXI 跨时钟域与通道 FIFO）会把异步 FIFO 用到 AXI 的 AR/R/AW/W/B 各通道上，本讲是它的直接前置。读完本讲后看 `axi_read_cdc`/`axi_write_cdc` 会非常自然。
- **纵向深挖约束**：若想彻底搞懂 `set_bus_skew`、`set_max_delay -datapath_only` 与 `false_path` 的取舍，建议精读 `resync_counter.tcl`、`asynchronous_fifo.tcl` 头部引用的两篇 LinkedIn 文章（CDC 可靠约束系列第 2、5 篇），以及 AMD UG903。
- **验证方法**：u8-l1 / u8-l2 会讲 `tb_asynchronous_fifo.vhd` 里用到的 `axi_stream_master`/`axi_stream_slave` BFM、随机背压、`check_stable`（防 packet 中间出现 bubble）等通用验证套路，本讲的 testbench 是很好的实例。
- **源码继续阅读**：`modules/fifo/src/asynchronous_fifo.vhd` 的 `set_full_packet_status` 块（`:372-400`）处理「输出寄存器 + packet 模式」这一最复杂组合下的 `read_valid` 计算，注释极为详尽，推荐作为进阶阅读。
