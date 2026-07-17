# 协同仿真验证机制

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 Nyuzi **协同仿真（cosimulation）** 的总体思路：让周期精确的 Verilator 硬件模型和功能级 C 模拟器**跑同一个程序、逐条比对指令副作用**。
- 读懂硬件侧 `+trace` 打印的事件文本格式，以及模拟器侧如何「单步到下一个副作用」再比对。
- 理解 `runtest.py` 如何把两个进程用管道串起来，并做「逐事件比对 + 最终内存镜像比对」两级校验。
- 掌握 `generate_random.py` 的**约束随机**策略，以及为何要做这些约束。
- 准确说出协同仿真的**已知覆盖盲区**（浮点、store buffer、store_sync+中断、虚拟内存翻译等），并解释其成因。

## 2. 前置知识

本讲建立在两篇前置讲义之上，请先确认你已经理解：

- **u8-l1 模拟器架构与指令执行**：Nyuzi 有两个执行模型——Verilator 把 SystemVerilog RTL 编译成的**周期精确仿真器**（`nyuzi_vsim`），和 C 写的**指令集模拟器**（`nyuzi_emulator`）。后者只维护架构状态（寄存器、平坦内存、PC、控制寄存器、TLB），不建模微架构（流水线、缓存、记分牌）。本讲正是让这两者互为「金标准」。
- **u6-l3 L2 缓存四阶段流水线**：数据最终要落到系统内存；协同仿真第二级比对的就是「程序结束时内存镜像是否一致」，而 L2 的脏行写回直接影响这个镜像。

几个本讲会反复用到的术语：

- **副作用（side effect）**：一条指令对架构状态的可见改变——写寄存器或写内存。分支不产生副作用（它只改 PC，而 PC 由取指推进天然对齐）。
- **锁步（lockstep）**：两个模型按同一顺序、一步一步执行，每步结果逐一比对。
- **2 态仿真器**：Verilator 的信号只有 0/1，遇到未初始化的 X 会赋予随机值（见后文随机种子）。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [tools/emulator/cosimulation.c](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/cosimulation.c) | 协同仿真的**核心**：读硬件 trace、单步模拟器、比对副作用。 |
| [tools/emulator/cosimulation.h](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/cosimulation.h) | 对外接口：`run_cosimulation` 及四个 `cosim_check_*` 回调。 |
| [hardware/testbench/trace_logger.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/testbench/trace_logger.sv) | 硬件侧：`+trace` 时把写回与访存按**发射序**打印成文本。 |
| [hardware/testbench/soc_tb.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/testbench/soc_tb.sv) | 测试台：结束时 `flush_l2_cache` 并转储内存镜像、打印 `***HALTED***`。 |
| [tools/emulator/processor.c](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c) | 模拟器核心：`set_scalar_reg`/`set_vector_reg`/store 处钩入 `cosim_check_*`。 |
| [tests/cosimulation/runtest.py](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/runtest.py) | 测试驱动：起两个进程、接管道、做两级校验。 |
| [tests/cosimulation/generate_random.py](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/generate_random.py) | 约束随机汇编程序生成器。 |
| [tests/cosimulation/README.md](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/README.md) | 机制说明与已知限制清单。 |

---

## 4. 核心概念与源码讲解

### 4.1 锁步比对：硬件与模拟器的逐事件对账

#### 4.1.1 概念说明

协同仿真要回答一个问题：**我写的 RTL 对不对？** 单元测试只能覆盖孤立场景，真实程序又太长、难以手工预期结果。Nyuzi 借用一个工业界经典做法——**自我对账**：既然已经有了两个独立实现（C 模拟器 + Verilator RTL），就让它们跑同一个程序，把每条指令的副作用逐一比对。只要两者**对同一条指令**写出相同的寄存器值、相同的内存值，就说明（在这一条上）两者一致。

这里有一个关键设计取舍：**只比对架构副作用，不比对时序**。模拟器根本没有缓存、流水线、记分牌，所以无法比对「第几周期发生」「缺失几次」。能比对的只有「寄存器/内存最终被写成了什么」。这也决定了为何本机制叫**功能验证**而非性能验证。

#### 4.1.2 核心流程

整个协同仿真是一个**生产者—消费者**的管道结构：

```
 ┌─────────────────┐  stdout(trace文本)  ┌──────────────────┐
 │ nyuzi_vsim      │ ───────────────────▶ │ nyuzi_emulator   │
 │ (+trace)        │   管道 stdin         │ (-m cosim)       │
 │ 周期精确, 生产者 │                      │ 功能级, 消费者   │
 └─────────────────┘                       └──────────────────┘
```

消费者侧（`cosimulation.c`）的主循环逻辑，[cosimulation.c:L25-L37](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/cosimulation.c#L25-L37) 的注释把它总结成四步，可表述为下面的伪代码：

```
while 读到一行 trace:
    解析出「期望事件」(pc, thread, reg/addr, mask, values) → 存入 expected_*
    run_until_next_event(thread):                # 推进模拟器
        重复 dbg_single_step(thread) 直到一个副作用回调被触发
            # 模拟器执行指令时, set_scalar_reg / store 等会回调 cosim_check_*
            # 回调里把「模拟器刚发生的副作用」与 expected_* 逐字段比对
            # 不一致 → cosim_mismatch = true
读到 ***HALTED***:
    继续把模拟器跑到也停机, 期间若再触发任何事件即为不一致
```

要点：**比对是逐线程、按程序序的**。trace 每行带 `thread_id`，`run_until_next_event` 只单步这一个线程（见 [cosimulation.c:L314-L330](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/cosimulation.c#L314-L330)）。因此跨线程的交错顺序不需要一致——每个线程各自有一条副作用流，分别对账即可。这一点很重要：它让多线程测试不必苛求两个模型在线程调度上完全一致。

#### 4.1.3 源码精读

**主循环 `run_cosimulation`**（[cosimulation.c:L63-L167](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/cosimulation.c#L63-L167)）用 `fgets` 逐行读 stdin，按前缀 `sscanf` 分流到四种事件之一：

- `store ...` → `EVENT_MEM_STORE`（[L90-L107](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/cosimulation.c#L90-L107)）
- `vwriteback ...` → `EVENT_VECTOR_WRITEBACK`（[L108-L125](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/cosimulation.c#L108-L125)）
- `swriteback ...` → `EVENT_SCALAR_WRITEBACK`（[L126-L136](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/cosimulation.c#L126-L136)）
- `***HALTED***` → 置 `verilog_model_halted` 并跳出（[L137-L141](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/cosimulation.c#L137-L141)）
- `interrupt ...` → 调 `cosim_interrupt`（[L142-L143](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/cosimulation.c#L142-L143)），让模拟器在对应线程注入中断（见 4.4）。

每解析到一个 store/writeback 事件，就把字段塞进一组全局 `expected_*` 变量（[L54-L61](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/cosimulation.c#L54-L61)），然后调 `run_until_next_event`。

**回调比对函数**是这套机制的「比对器」。以标量写回为例，[cosimulation.c:L169-L185](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/cosimulation.c#L169-L185)：

```c
void cosim_check_set_scalar_reg(struct processor *proc, uint32_t pc, uint32_t reg, uint32_t value)
{
    cosim_event_triggered = true;
    if (expected_event != EVENT_SCALAR_WRITEBACK
            || expected_pc != pc
            || expected_register != reg
            || expected_values[0] != value)
    {
        cosim_mismatch = true;
        ...打印 Reference / Hardware 两行...
    }
}
```

它同时比对**四样东西**：事件类型、PC、寄存器号、数值。任何一个不符即标记 `cosim_mismatch`。向量写回（[L187-L211](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/cosimulation.c#L187-L211)）额外比对面掩码，并且只比对**掩码置位的 lane**（`masked_vectors_equal`，[L332-L346](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/cosimulation.c#L332-L346)）——被掩蔽的 lane 不写也不比，符合 u2-l1 讲过的掩码语义。

**这些回调由谁触发？** 答案在模拟器核心。当 `enable_cosim` 打开时，`set_scalar_reg` 会在真正写寄存器前调用 `cosim_check_set_scalar_reg`（[processor.c:L635-L647](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L635-L647)）；向量写回同理（[processor.c:L649-L672](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L649-L672)）；store 在访存执行路径里调用 `cosim_check_scalar_store`/`cosim_check_vector_store`（如 [processor.c:L1407-L1415](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L1407-L1415)）。也就是说：**单步模拟器执行一条有副作用的指令时，副作用天然流进比对器**——无需专门插桩。

注意传给回调的 PC 是 `thread->pc - 4`（[processor.c:L638](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L638)）。这是因为模拟器执行指令时 PC 已自增到下一条，要还原成「本条指令的 PC」需扣回一个字（4 字节）。这与 u8-l1 讲过的 `-v` 踪迹用 `pc-4` 是同一道理。

#### 4.1.4 代码实践

**实践目标**：直观看到「逐事件、逐字段」比对如何触发不匹配。

**操作步骤**（源码阅读型，不依赖完整工具链）：

1. 打开 [cosimulation.c:L169-L185](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/cosimulation.c#L169-L185)，找到 `cosim_check_set_scalar_reg` 的四个比对条件。
2. 假设硬件 trace 给出 `swriteback 00000074 0 00 00000001`（即 pc=0x74, thread=0, reg=s0, value=1），而模拟器执行到该指令时算出 value=2。手工推演：`expected_values[0]=1` 而 `value=2`，第四个条件 `expected_values[0] != value` 为真 → 进入 mismatch 分支。
3. 阅读分支内打印格式：先 `print_registers`，再打印 `Reference: ...`（模拟器侧）与 `Hardware: ...`（trace 侧）两行（[L178-L184](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/cosimulation.c#L178-L184)）。注意命名稍反直觉：`Reference` 指模拟器（功能金标准），`Hardware` 指 Verilator trace。

**需要观察的现象**：任何一项（事件类型/PC/寄存器号/值）不一致都会被同一个 `if` 捕获并定位到具体线程。

**预期结果**：能说出四个比对字段，并解释为何 PC 也必须比对（防止两条不同指令被误判为匹配）。

> 若你本地已按 u1-l2 搭好工具链，可进一步把 `generate_random.py` 生成的某条整数指令的立即数改一个值，重建后看模拟器与硬件在何处报 `COSIM MISMATCH`——这会真实触发上面的分支。运行结果**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么比对是「逐线程按程序序」，而不是「全核按时间序」？
**答案**：模拟器不建模线程调度时序，无法保证与硬件在同一周期切换线程；但每个线程内部的指令是程序序的，只要各自对账即可。trace 每行带 `thread_id`，`run_until_next_event` 只推进该线程，从而绕开了跨线程时序不可比的问题。

**练习 2**：`run_until_next_event` 最多单步 500 次（[L320](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/cosimulation.c#L320)），这个上限的作用是什么？
**答案**：防止模拟器陷入「迟迟不产生副作用」的死循环（例如一条 `membar` 之后期望的事件迟迟不来）。超过 500 步仍无事件，会打印 `Simulator program in infinite loop?` 并判定失败——这通常意味着两侧已偏离同步。

---

### 4.2 trace 格式：副作用事件流水

#### 4.2.1 概念说明

要让两个模型对账，得先有一份**双方都能理解的「事件清单」**。硬件侧用 `+trace` 选项，在指令**退休（writeback）**时把副作用打印成一行行文本；模拟器侧解析这些文本。这份文本就是两者的「共同语言」。

这里有个微妙点：硬件流水线**不一定按程序序退休指令**（浮点 5 级、整数 1 级、访存 2+ 级，长度不同，可能乱序到达写回级，参见 u3-l2）。如果直接按退休时刻打印，事件顺序就和模拟器的程序序对不上，无法逐行比对。所以 `trace_logger` 用一个**重排序队列**，把事件按**发射序**重新排好再打印。

#### 4.2.2 核心流程

事件类型与文本格式（来自 [README.md:L43-L53](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/README.md#L43-L53) 与 `trace_logger.sv` 的 `$display`）：

| 文本行 | 含义 | 字段 |
| --- | --- | --- |
| `swriteback <pc> <thread> <reg> <value>` | 标量寄存器写回 | pc、线程、寄存器号、32 位值 |
| `vwriteback <pc> <thread> <reg> <mask> <value×16>` | 向量寄存器写回 | pc、线程、寄存器号、16 位掩码、16 个 lane |
| `store <pc> <thread> <addr> <mask> <value×16>` | 内存写 | pc、线程、对齐到缓存行的地址、64 字节掩码、16 个 lane |
| `interrupt <thread> <pc>` | 中断注入 | 线程、被中断的 PC |
| `***HALTED***` | 硬件停机 | —— |

`trace_logger` 每个时钟上升沿做三件事（[trace_logger.sv:L108-L243](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/testbench/trace_logger.sv#L108-L243)）：

1. **打印队列头** `trace_reorder_queue[0]`（如果是有效事件类型，[L124-L162](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/testbench/trace_logger.sv#L124-L162)）。
2. **整体下移**：`queue[i] <= queue[i+1]`，末位清零（[L164-L167](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/testbench/trace_logger.sv#L164-L167)）。
3. **按来源入队**到不同槽位（[L171-L219](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/testbench/trace_logger.sv#L171-L219)）：
   - 整数写回 → 槽 4（短延迟，1 级就退休）
   - 访存写回 → 槽 3
   - 浮点写回 → 槽 0（5 级，延迟最长，离队头最远）
   - store / interrupt → 槽 5

槽号 = 该事件还需多少拍才到达队头被打印。**延迟长的入队位置靠后**，等它「走」到队头时，延迟短的同类事件也恰好排到了它前面，从而恢复成**发射序**。这是一种用空间换顺序的巧妙排布。

此外，两类事件会被**作废**（置 `EVENT_INVALID`）：

- 被回滚的 store 不应比对（[L222-L223](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/testbench/trace_logger.sv#L222-L223)）。
- **失败的同步 store**（`store_sync` 未抢到锁）不产生内存副作用，也不打印（[L226-L230](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/testbench/trace_logger.sv#L226-L230)）。这一点是 4.4 节「store_sync + 中断」限制的根源。

#### 4.2.3 源码精读

trace 是否开启由 plusarg 决定，[trace_logger.sv:L100-L103](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/testbench/trace_logger.sv#L100-L103)：

```systemverilog
initial
begin
    trace_en = $test$plusargs("trace") != 0;
end
```

打印格式以向量写回为例，[trace_logger.sv:L125-L133](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/testbench/trace_logger.sv#L125-L133)：

```systemverilog
EVENT_VWRITEBACK:
    $display("vwriteback %x %x %x %x %x",
        trace_reorder_queue[0].pc,
        trace_reorder_queue[0].thread_idx,
        trace_reorder_queue[0].writeback_reg,
        trace_reorder_queue[0].mask,
        trace_reorder_queue[0].data);
```

注意 `%x` 把 `vector_t`（512 位）与掩码直接打成十六进制串，与模拟器侧 `sscanf(line, "vwriteback %x %x %x %" PRIx64 " %s", ...)`（[cosimulation.c:L108](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/cosimulation.c#L108)）的解析格式严格对应——**两端格式必须逐字符对齐**，否则 `sscanf` 返回值不为 5，事件就被当成无法识别的行。

**掩码的两种表示**需要在比对时换算。硬件 store 行里 `mask` 是**字节级**掩码（64 位，每 bit 对应缓存行 1 字节）；模拟器内部用的是 **lane 级**掩码（16 位，每 bit 对应一个 4 字节 lane）。`cosim_check_vector_store` 做换算，[cosimulation.c:L213-L224](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/cosimulation.c#L213-L224)：

```c
for (lane = 0; lane < NUM_VECTOR_LANES; lane++)
    if (mask & (1 << lane))
        byte_mask |= 0xf000000000000000ull >> (lane * 4);
```

即把 lane 掩码的每一位展开成 4 个字节位：

\[
\text{byte\_mask} \;=\; \bigvee_{\text{lane}\,\in\,\text{mask}}\; \bigl(\texttt{0xf000\_0000\_0000\_0000} \;\gg\; (\text{lane}\times 4)\bigr)
\]

标量 store 的字节掩码则按访问大小 `size`（1/2/4 字节）和地址在行内的偏移计算，[cosimulation.c:L260](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/cosimulation.c#L260)：

\[
\text{reference\_mask} \;=\; \bigl((2^{\text{size}}-1)\bigr) \;\ll\; \bigl(63 - (\text{addr}\,\&\,63) - (\text{size}-1)\bigr)
\]

这两个公式说明：硬件把**任何写**都建模成「对 64 字节缓存行的字节级掩码写」，模拟器在比对时把标量/lane 写也换算成同一粒度，于是 store 事件能统一比对。

#### 4.2.4 代码实践

**实践目标**：用 `--debug` 同时看到硬件 trace 行与模拟器踪迹，理解「两套打印为何长得不一样」。

**操作步骤**：

1. 阅读 [README.md:L37-L54](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/README.md#L37-L54) 给的 `--debug` 示例输出。注意两种格式：
   - 硬件：`swriteback 00000074 0 00 00000001`
   - 模拟器：`00000074 [th 0] s0 <= 00000001`
2. 对照源码确认这两种格式分别由谁打印：硬件侧是 `trace_logger` 的 `$display`（[L137-L142](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/testbench/trace_logger.sv#L137-L142)）；模拟器侧是 `enable_tracing` 打开后 `set_scalar_reg` 里的 `printf`（[processor.c:L637-L638](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L637-L638)）。README 也坦言这两种格式「不一样，以后该清理一下」。
3. 注意：`-v`（tracing）和 `-m cosim`（协同仿真）是**两套独立机制**，但在 cosim 模式下 `--debug` 会同时打开二者（[runtest.py:L60-L61](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/runtest.py#L60-L61) 给 emulator 追加 `-v`，[cosimulation.c:L78-L79](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/cosimulation.c#L78-L79) 在 verbose 时 `enable_tracing`），所以你能同时看到两份输出。

**需要观察的现象**：同一 PC 的写回，硬件行与模拟器行交替出现，值一致即正常。

**预期结果**：能指着一行说清它是「硬件生产」还是「模拟器消费」，并解释两套打印格式不同的历史原因。

> 实际运行需要 Verilator 工具链；本机未构建时**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么浮点写回要放到重排队列的**槽 0**（离队头最远），而整数放槽 4？
**答案**：槽号代表「还需几拍到达队头被打印」。浮点流水线 5 级，从发射到退休延迟最长，故要放在最靠后的槽，让它慢慢前移；整数 1 级退休快，放靠前的槽。这样不同延迟的事件最终按**发射先后**排着队打印，与模拟器程序序对齐。

**练习 2**：`store` 事件的地址为何被对齐到缓存行边界（`trace_logger` 里把低位置零）？
**答案**：硬件把写建模为「对整行的字节掩码写」（u6-l1 讲过的 64 字节缓存行）。对齐到行边界后，地址 + 64 位字节掩码就能唯一描述一次写作用了哪些字节，模拟器侧也按同一行同一掩码换算比对（见 4.2.3 的两个公式）。

---

### 4.3 约束随机测试生成

#### 4.3.1 概念说明

有了对账机制，还需要**大量、多样的测试程序**去喂它。手写汇编覆盖面有限，**纯随机**又有问题：分支太密会冲刷流水线、掩盖依赖类 bug；寄存器用满 32 个会让前后指令的 RAW 依赖变得稀疏；随机指针会访存越界直接崩溃。所以 Nyuzi 采用**约束随机（constrained random）**生成：仍是随机的，但加上一组「让 bug 更容易暴露、又不让程序崩」的约束。

这是处理器验证的成熟做法，README 列出了 Alpha 21264、HP PA 8000 等商用处理器的相关论文（[README.md:L7-L12](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/README.md#L7-L12)）。

#### 4.3.2 核心流程

`generate_random.py` 生成一份多线程汇编，结构是：每线程先做一段**初始化**（设指针、用线性同余发生器填充私有内存、给寄存器填非零初值），再进入一段**随机指令流**。指令类型按权重抽样（[generate_random.py:L398-L407](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/generate_random.py#L398-L407)）：

| 权重 | 指令类 | 生成函数 |
| --- | --- | --- |
| 0.50 | 二元算术 | `generate_binary_arith` |
| 1.00（每轮都可能） | 分支 | `generate_branch` |
| 0.20 | 访存 | `generate_memory_access` |
| 0.10 | 计算指针 | `generate_computed_pointer` |
| 0.10 | 比较 | `generate_compare` |
| 0.05 | 一元算术 | `generate_unary_arith` |
| 0.03 | 缓存控制（dflush/iinvalidate/membar） | `generate_cache_control` |
| 0.01 | 设备 IO | `generate_device_io` |

四类关键约束（详细理由见 [README.md:L92-L128](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/README.md#L92-L128)）：

1. **分支**：只生成**前向**分支，且跳转距离 ≤ 6 条指令（[generate_random.py:L335-L354](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/generate_random.py#L335-L354)）。前向避免死循环；短距离避免跳过太多代码、削弱覆盖率。
2. **寄存器**：算术指令的源/目寄存器只在 `s3–s8`/`v3–v8` 里选（`generate_arith_reg`，[L40-L43](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/generate_random.py#L40-L43)）。缩小寄存器池 → 相邻指令撞同一寄存器的概率大增 → **RAW/WAW 依赖密集**，正好压测记分牌（u4-l3）。
3. **内存指针**：保留专用指针寄存器（[L20-L33](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/generate_random.py#L20-L33)）：`s0/v0` 共享只读段基址、`s2/v2` 各线程私有读写段基址、`s1/v1` 由 `add_i` 计算出的合法地址、`s9` 指向设备空间 `0xffff0000`。偏移取对齐随机值，刻意命中不同缓存行，且段基址按 **L2 缓存大小对齐**以诱发别名与脏行写回（[README.md:L120-L123](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/README.md#L120-L123)）。
4. **每线程独立写区**：私有段按线程分布在 `0x800000 + thread*0x100000`（[L27-L33](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/generate_random.py#L27-L33)）。原因是模拟器不建模 store buffer，多线程写同一行会对不上（详见 4.4）。

命令行（[L552-L578](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/generate_random.py#L552-L578)）：`-n` 每线程指令数（默认 60000）、`-t` 线程数（默认 4）、`-m` 生成多个文件、`-i` 开中断、`-o` 输出文件名。

#### 4.3.3 源码精读

看一段最典型的「受限随机」——二元算术指令的拼装，[generate_random.py:L87-L128](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/generate_random.py#L87-L128)：

```python
mnemonic = random.choice(BINARY_OPS)        # or/and/xor/add_i/sub_i/shl/...
typed, typea, typeb, suffix = random.choice(INT_FORMS)
dest = generate_arith_reg()                  # 3..8
rega = generate_arith_reg()
regb = generate_arith_reg()
opstr = '        {}{} {}{}, '.format(mnemonic, suffix, typed, dest)
...
```

注意 `BINARY_OPS` 列表里**浮点操作被注释掉了**（[L78-L84](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/generate_random.py#L78-L84)），`COMPARE_OPS` 里的浮点比较也被注释（[L201-L206](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/generate_random.py#L201-L206)）：

```python
    # Disable for now because there are still some rounding bugs that cause mismatches
    #    'add_f',
    #    'sub_f',
    #   'mul_f'
```

这正是 4.4 节「浮点不在覆盖范围」的直接证据——不是机制不支持，而是**已知精度差异会让比对必然失败**，所以生成器主动回避。

内存访问里有一条很巧的约束：同步 load 前自动插一条 `membar`，[generate_random.py:L296-L299](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/generate_random.py#L296-L299)：

```python
# Because we don't model the store queue in the emulator,
# a store can invalidate a synchronized load that is issued subsequently.
# A membar guarantees order.
if opstr == 'load' and suffix == '_sync':
    opstr = 'membar\n        ' + opstr
```

注释点明了根因：模拟器无 store queue，紧邻的 store 可能让随后的 `load_sync` 失效，插 `membar` 强制定序以绕开。

#### 4.3.4 代码实践

**实践目标**：亲手生成一份随机测试，看清它的「约束」如何体现。

**操作步骤**：

1. 在 `tests/cosimulation/` 下运行（仅需 Python，不需工具链）：
   ```
   ./generate_random.py -n 200 -t 4 -o my_random.S
   ```
2. 打开 `my_random.S`，找到 `start_thread0:` 之后的指令流。
3. 验证三件事：
   - 所有 `add_i`/`or`/`cmp...` 的寄存器号是否都在 `s3–s8`/`v3–v8`。
   - 所有 `bz`/`bnz`/`b`/`call` 的目标标号是否都是**其后方**且 1–6 行内的 `Nf`。
   - 是否有 `add_i s1, s2, ...` / `add_i v1, v2, ...`（计算合法指针）穿插出现。

**需要观察的现象**：寄存器池小 → 连续几条指令频繁撞同一寄存器，形成密集 RAW 依赖。

**预期结果**：能解释「缩小寄存器池」如何提升对记分牌/冒险逻辑的压测强度。

> 生成器是纯 Python，结果可复现；但「随机」意味着每次具体指令不同。若要复现一次失败，用 `-n`/`-t` 配合 `runtest.py --randseed=<seed>`（见 4.4 末尾）。本步骤的运行**可本地验证**（只要装了 Python 3）。

#### 4.3.5 小练习与答案

**练习 1**：为什么生成器要在程序开头用线性同余发生器把私有内存填满随机数（[generate_random.py:L448-L464](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/generate_random.py#L448-L464)）？
**答案**：让随后的随机 `load` 读到的是**有意义、可复现的随机数据**，而不是全 0。全 0 会掩盖很多 bug（比如符号扩展错误、移位错误在 0 上看不出来）。用线程号做种子还能让各线程的数据不同，增加多样性。

**练习 2**：段基址为何要按 L2 缓存大小对齐？
**答案**：让不同线程/不同偏移的访存**映射到相同的 L2 组索引**，诱发缓存别名与脏行替换写回。这能压测 L2 的替换、写回路径（u6-l3）——而这些写回最终进入「内存镜像比对」，正好被第二级校验抓住。

---

### 4.4 已知限制与覆盖盲区

#### 4.4.1 概念说明

协同仿真很强大，但**不是无所不能**。它的能力边界由「模拟器建模了什么」决定：模拟器不建模 store buffer、不建模 TLB 替换时序、浮点又不完全兼容 IEEE 754。这些差异会让某些场景**必然对不上**，于是项目干脆在生成器或文档里把它们排除。理解这些盲区，才能正确读懂「测试全绿」的含义——它不代表这些盲区被验证过了。

README 的 [Limitations 一节](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/README.md#L144-L167)（L144–L167）列出了全部已知限制。

#### 4.4.2 核心流程

把限制按「成因」分类：

| 盲区 | 成因 | 规避手段 |
| --- | --- | --- |
| store buffer 可见性 | 模拟器无 store queue，多线程写同一行对不上 | 每线程独立写区（`generate_random.py`） |
| 浮点指令 | 浮点流水线非完全 IEEE 754 兼容（issue #87） | 生成器注释掉 `add_f/sub_f/mul_f` 及浮点比较 |
| `store_sync` + 中断 | 失败的 sync store 不打印事件，中断却已发生 | 文档记录，属已知不准 |
| subcycle（CR13）+ 中断 | 硬件不为掩蔽的 scatter lane 打印事件 | 文档记录 |
| 虚拟内存翻译 | 软件管理 TLB，替换行为依赖时序，难精确匹配 | 不验证翻译，仅查最终内存 |

另外有一条「不算盲区但要懂」的机制：**最终内存镜像比对**。因为随机程序不主动 flush 缓存，而模拟器不仿真缓存，所以 testbench 在结束前用 `+autoflushl2` 把 L2 脏行全部写回内存（[soc_tb.sv:L469-L470](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/testbench/soc_tb.sv#L469-L470)），再把内存区间转储成文件（[soc_tb.sv:L465-L492](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/testbench/soc_tb.sv#L465-L492)）。模拟器也转储自己的内存（`-d` 参数）。最后 `runtest.py` 用 `assert_files_equal` 比对两个二进制文件（[runtest.py:L90-L91](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/runtest.py#L90-L91)）。这是**第二级**校验，捕获那些没有寄存器副作用、只改内存的尾部行为。

#### 4.4.3 源码精读

**两级校验的连接点**在 `runtest.py`。Verilator 侧带 `+autoflushl2 +memdumpfile=... +memdumpbase=0x800000 +memdumplen=0x400000`（[runtest.py:L41-L50](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/runtest.py#L41-L50)），模拟器侧用 `-d <file>,0x800000,0x400000`（[runtest.py:L52-L58](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/runtest.py#L52-L58)）转储**同一区间**。两者区间、基址必须一致，文件才能比。

两个进程的管道连接（[runtest.py:L65-L68](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/runtest.py#L65-L68)）：

```python
p1 = subprocess.Popen(verilator_args + ['+bin=' + hexfile], stdout=subprocess.PIPE)
p2 = subprocess.Popen(emulator_args + [hexfile], stdin=p1.stdout, stdout=subprocess.PIPE)
```

`p1`（硬件）的 stdout 直接连到 `p2`（模拟器）的 stdin——这正是 4.1 那张管道图的实现。

`***HALTED***` 必须最后打印，[soc_tb.sv:L500-L502](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/testbench/soc_tb.sv#L500-L502) 注释点明原因：

```systemverilog
// Do this last so emulator doesn't kill us with SIGPIPE during cosimulation.
if (processor_halt)
    $display("***HALTED***");
```

模拟器读到 `***HALTED***` 后会退出，若硬件此时仍在写管道就会触发 SIGPIPE——所以这行刻意放在 `final` 块最后。

**store_sync + 中断**为何不准，README 给了精炼的四步场景（[README.md:L155-L161](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/README.md#L155-L161)）：硬件执行 `store_sync` 失败 → 因失败无内存副作用而不打印事件（4.2 讲过的 [trace_logger.sv:L226-L230](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/testbench/trace_logger.sv#L226-L230)）→ 此时中断到来，硬件已跳入 ISR → 模拟器因为没有收到「失败」事件，不会把目标寄存器写成 0，于是状态发散。

**随机种子**也属「已知行为」：Verilator 是 2 态仿真器，复位时未初始化的信号（如 SRAM）被赋随机值，导致每次运行略有不同。程序启动会打印种子（[README.md:L56-L72](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/README.md#L56-L72)），`runtest.py --randseed=<seed>` 可复现（[runtest.py:L33-L36](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/runtest.py#L33-L36)）。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：跑通一个随机协同仿真测试，用 `--debug` 观察两侧事件比对，并解释浮点与 store buffer 为何不在覆盖范围。

**操作步骤**：

1. 先生成一个随机测试（纯 Python，可本地验证）：
   ```
   cd tests/cosimulation
   ./generate_random.py -n 5000 -t 4 -o random.S
   ```
2. 运行协同仿真（需要 u1-l2 搭好的 Verilator 工具链）：
   ```
   ./runtest.py --debug random.S
   ```
3. 在输出中找到成对出现的硬件行与模拟器行，例如：
   ```
   swriteback 00000074 0 00 00000001      ← 硬件 trace
   00000074 [th 0] s0 <= 00000001          ← 模拟器 -v 踪迹
   ```
4. 确认两侧的 PC、线程、寄存器、值一致。
5. 打开 `generate_random.py`，指出 `add_f/sub_f/mul_f`（[L78-L84](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/generate_random.py#L78-L84)）被注释；再打开 [README.md:L146-L154](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/README.md#L146-L154)，对照「浮点不兼容 IEEE 754」与「模拟器不建模 store buffer」两条限制。

**需要观察的现象**：

- 全程**不会出现任何浮点指令**（即便 `BINARY_OPS` 看似有 `_f` 入口，实际被注释）。
- 多线程测试里，各线程的 store 地址都落在**各自的私有段**（`0x800000 / 0x900000 / 0xa00000 / 0xb00000`），没有两个线程写同一缓存行。

**预期结果**：能用一句话解释——浮点不在覆盖范围是因为硬件浮点实现非完全 IEEE 兼容（u5-l3 讲过的舍入/特殊值差异），比对必然失败故主动排除；store buffer 不在覆盖范围是因为模拟器根本不建模它，多线程同写一行会对不上，故生成器为每线程预留独立写区。

> 步骤 1 可本地验证；步骤 2–4 依赖 Verilator 工具链，未构建时**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：既然协同仿真有这么多盲区，它还有价值吗？
**答案**：非常有价值。它用极低的工程成本（两个现成模型 + 一段比對代码）对**整数算术、访存、分支、中断、缓存交互**等核心行为做了**海量随机回归**。盲区（浮点、store buffer 时序、VM 翻译）由其它层次的测试补位（见 u15）。没有任何单一方法能覆盖一切，协同仿真覆盖的是「最易回归、量最大」的那一块。

**练习 2**：为什么第二级「内存镜像比对」需要 `+autoflushl2`？
**答案**：随机程序不会主动写回脏行，而硬件的脏数据可能还停在 L2 缓存里、没落进内存；模拟器则直接写平坦内存、没有缓存概念。若不 flush，硬件转储的内存就会缺失这些脏行，与模拟器不一致。`flush_l2_cache` 把 L2 脏行全部写回，让硬件内存与模拟器内存处于可比的最终状态。

---

## 5. 综合实践

把本讲四块知识串起来，完成一次「读一个失败用例」的小任务：

1. **构造场景**：阅读 [cosimulation.c:L169-L185](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/cosimulation.c#L169-L185) 与 [L279-L312](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/cosimulation.c#L279-L312)，假设你看到如下失败输出：
   ```
   COSIM MISMATCH, thread 2
   Reference: 0000009c s5 <= 00000002      ← 模拟器算出 2
   Hardware:  0000009c s5 <= 00000007       ← 硬件 trace 期望 7
   ```
2. **定位**：这是 `cosim_check_set_scalar_reg` 触发的。说明被比对的四字段中，**值**这一项不符（PC、线程、寄存器号都对）。
3. **回溯事件流**：根据 4.2，这行 `Hardware` 来自 `trace_logger` 的 `EVENT_SWRITEBACK`（[trace_logger.sv:L135-L142](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/testbench/trace_logger.sv#L135-L142)），经重排队列按发射序打印；`Reference` 来自模拟器 `set_scalar_reg` 的 `printf`（[processor.c:L637-L638](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L637-L638)）。
4. **判断盲区**：用 `llvm-objdump --disassemble` 反汇编（[README.md:L26-L30](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/README.md#L26-L30) 给了路径），看 0x9c 处是不是浮点指令或受 store buffer 影响的序列。若不是，才值得当成真实 RTL bug 去查。
5. **复现**：记下输出里的随机种子，用 `./runtest.py --randseed=<seed> random.S` 重跑确认稳定复现（[README.md:L69-L72](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/cosimulation/README.md#L69-L72)）。

> 反汇编与种子复现依赖工具链；未构建时**待本地验证**。本任务的核心是**读懂失败信息如何映射回源码与盲区清单**，这部分不依赖运行。

## 6. 本讲小结

- 协同仿真 = 周期精确 Verilator 模型（`+trace`）与功能级 C 模拟器（`-m cosim`）**逐条比对指令副作用**，只比架构可见的寄存器/内存写，不比时序。
- 比对由 `cosimulation.c` 主导：读一行 trace → 单步对应线程到产生副作用 → 在 `cosim_check_*` 回调里逐字段（事件类型/PC/寄存器/值/掩码）比较；回调由模拟器的 `set_*_reg`/store 自然触发。
- trace 格式有 `swriteback`/`vwriteback`/`store`/`interrupt`/`***HALTED***` 五类；硬件侧用 `trace_logger` 的**重排队列**把乱序退休的事件按**发射序**打印，掩码在两端用字节级 vs lane 级两种表示并换算。
- `runtest.py` 用管道串两个进程，做**两级校验**：逐事件锁步 + 最终内存镜像（`+autoflushl2` 把 L2 脏行写回后 `assert_files_equal`）。
- `generate_random.py` 用**约束随机**（前向短分支、小寄存器池、专用合法指针、每线程独立写区）压测依赖与缓存，同时主动回避浮点。
- 已知盲区：store buffer 可见性、浮点（非完全 IEEE 兼容）、`store_sync`+中断、subcycle+中断、虚拟内存翻译——这些都**不在**覆盖范围内，需结合 u15 的其它测试层次理解。

## 7. 下一步学习建议

- **横向补盲区**：浮点精度的根因在 u5-l3（浮点五级流水线），store buffer 的硬件实现在 u6-l1/u6-l2（L1D 与 store 队列），虚拟内存翻译在 u7-l1（软件管理 TLB）——读懂它们，你就明白这些盲区为何「对不上」。
- **纵向看体系**：协同仿真只是 Nyuzi 五层测试之一。继续读 u15-l1（测试框架与 CHECK 机制）、u15-l2（随机测试生成与约束）、u15-l3（单元/定向/整机测试策略），理解各层如何互补、共同覆盖协同仿真留下的盲区。
- **动手加深**：在本地搭好工具链后，用 `generate_random.py -m 50` 批量生成 50 个随机测试跑一夜（`runtest.py random*`），观察是否有偶发 mismatch；再用 `--randseed` 复现——这是真实处理器验证工程师的日常。
