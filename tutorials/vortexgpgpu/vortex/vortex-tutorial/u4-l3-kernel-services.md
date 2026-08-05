# printf、barrier 与系统调用

## 1. 本讲目标

学完本讲，你应当能够：

- 说清设备侧一次 `vx_printf` 是如何从格式化字符串走到主机终端的：包括 warp 内串行化（`vx_serial`）、tinyprintf 格式化、`vx_putchar` 写入 **COUT 环**、以及主机侧 `CoutDrainer` 排空环的全链路。
- 掌握 `vx_barrier` 的栅栏语义：到达（arrive）、等待（wait）、阶段号（phase）三代概念，以及 `__syncthreads()` 是如何落到一条 WCTL 指令上的。
- 理解设备侧的「轻量系统调用」并不是真正的 trap，而是一组 **newlib 桩函数**（`_write`/`_sbrk`/`_getpid` 等），它们把标准 C 库的 I/O 与进程抽象嫁接到 Vortex 的设备原语上。

本讲承接 u4-l2（SIMT 控制指令与 warp 调度 API），把视角从「控制指令本身」转到「设备内核实际会用到的三类服务」：打印、同步、libc 衔接。

## 2. 前置知识

- **SIMT 与 warp 模型**（u1-l1、u4-l2）：一个 warp 内多条线程共享 PC，靠 thread mask 控制写回。本讲会反复用到「PC 是 warp 级、寄存器与执行是 thread 级」这条结论。
- **SPLIT/JOIN 与 IPDOM 栈**（u4-l2）：`vx_serial` 正是用 SPLIT/JOIN 在 warp 内做「一次只让一个线程跑」的串行化。
- **custom0 指令槽**（u4-l2）：Vortex 把 TMC/WSPAWN/SPLIT/JOIN/PRED/BAR 等控制指令塞进 RISC-V custom0（`0x0B`），靠 `func3`/`rd` 区分。
- **设备内存的 IO 区**：Vortex 在设备地址空间里预留了一块低地址 IO 区，COUT 环就放在这里，主机与设备共享可见。
- **newlib 桩（stubs）**：bare-metal 程序链接 newlib 时，需要提供 `_write`/`_read`/`_sbrk` 等以下划线开头的「系统调用桩」，newlib 的 `printf`/`malloc` 会回调它们。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [sw/kernel/include/vx_print.h](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_print.h) | 打印 API 声明：`vx_printf`/`vx_vprintf`/`vx_putchar`/`vx_putint`/`vx_putfloat`。 |
| [sw/kernel/src/vx_print.c](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/src/vx_print.c) | 打印的 C 实现：用 `vx_serial` 串行化后调用 tinyprintf。 |
| [sw/kernel/src/vx_print.S](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/src/vx_print.S) | `vx_putchar` 的汇编实现：直接操作 COUT 环。 |
| [sw/kernel/src/tinyprintf.c](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/src/tinyprintf.c) | 嵌入式友好的格式化库（第三方 Marco Paland tinyprintf），最终回调 `vx_putchar`。 |
| [sw/kernel/src/vx_serial.S](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/src/vx_serial.S) | warp 内串行化原语：用 SPLIT/JOIN 让线程逐个独占地执行回调。 |
| [sw/kernel/include/vx_intrinsics.h](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_intrinsics.h) | 屏障内联函数：`vx_barrier`/`vx_barrier_arrive`/`vx_barrier_wait`/`vx_barrier_expect_tx`。 |
| [sw/kernel/include/vx_barrier.h](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_barrier.h) | C++ 屏障封装：`barrier`/`gbarrier`/`group_barrier` 三种作用域。 |
| [sw/kernel/include/vx_spawn.h](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_spawn.h) | `__syncthreads()` 宏与 `vx_serial` 声明。 |
| [sw/kernel/src/vx_syscalls.c](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/src/vx_syscalls.c) | newlib 系统调用桩：`_write`/`_sbrk`/`_getpid` 与 TLS/init_array 初始化。 |
| [VX_types.toml](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_types.toml) | COUT 环的内存布局常量（地址、槽数、环大小）。 |
| [sim/common/cout_drainer.h](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/cout_drainer.h) / [cout_drainer.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/cout_drainer.cpp) | 主机侧（仿真器）排空 COUT 环的实现。 |
| [sim/simx/main.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/main.cpp) | 独立 SimX 入口，每个 cycle 调用 `CoutDrainer::tick()`。 |
| [sim/simx/barrier_unit.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/barrier_unit.cpp) | SimX 侧屏障单元：arrive/wait/expect_tx 的时序模型（RTL 的预言机）。 |
| [tests/regression/printf/](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/printf/kernel.cpp) | printf 回归测试，本讲的实践对象。 |

---

## 4. 核心概念与源码讲解

### 4.1 vx_printf 与 COUT 环：设备侧打印

#### 4.1.1 概念说明

在主机程序里 `printf` 是再普通不过的事，但在 GPU 内核里却很难：成百上千个线程同时运行，如果都直接往同一个终端写字节，输出会瞬间乱成一锅粥。Vortex 的解法分成三层：

1. **warp 内串行化**：同一个 warp 里，一次 `vx_printf` 调用通过 `vx_serial` 让线程**逐个**独占地执行格式化与输出，避免一个 warp 内字符交错。
2. **格式化**：用一个无 `malloc`、可重入的第三方 tinyprintf 把 `%d`/`%c`/`%f` 等转换成字节流，每个字符回调 `vx_putchar`。
3. **有界环形缓冲（COUT 环）**：`vx_putchar` 不直接「打印」，而是把字节写进设备内存 IO 区里**每个 hart 独占**的环形缓冲；主机在后台轮询、排空这个环并真正输出到终端。

这套机制对标 CUDA/HIP 的 printf 语义：**有界、非阻塞、满了就丢并报告丢弃数**。这非常关键——如果打印满了就阻塞，一个 printf 密集的 kernel 在主机还没排空环之前就会死锁。

> 术语：**hart**（hardware thread）在这里指一个可执行线程上下文，`vx_putchar` 用 `MHARTID` 折叠到槽位，每个 hart 拥有自己的小环。

#### 4.1.2 核心流程

一次 `vx_printf("cid=%d: task=%d\n", cid, gid)` 的完整数据流：

```
vx_printf(fmt, ...)            // 用户调用（sw/kernel/src/vx_print.c）
   └─ vx_vprintf(fmt, va)
        └─ vx_serial(__vprintf_cb, &arg)   // warp 内串行化（vx_serial.S）
              ├─ for tid in 0..NT:
              │     SPLIT(只让 tid==index 的线程激活)
              │     若当前线程激活：__vprintf_cb(arg)
              │        └─ tiny_vprintf(fmt, va)        // 格式化（tinyprintf.c）
              │              └─ 每个字符 → _out_char → vx_putchar(ch)
              │                    └─ 写入 COUT 环 data[hartid][wr] （vx_print.S）
              │                       满 → 丢字节 + atomic lost[hartid]++
              │     JOIN(汇聚)
              └─ 所有线程都打印完
   ↓ 设备内存 IO 区的 COUT 环被填充
主机侧：CoutDrainer::tick()       // sim/common/cout_drainer.cpp
   └─ 每个槽：读 wr[]，把 [rd, wr) 的字节拷到 stdout，推进 rd[]
       满/丢时打印 "[#slot: lost K bytes]"
```

COUT 环的内存布局（由 [VX_types.toml:27-42](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_types.toml#L27-L42) 固定为软硬件契约）：

```
VX_MEM_IO_COUT_ADDR 起的连续区：
  uint32 wr  [SLOTS]      // SLOTS=64，每个 hart 一个生产者写指针
  uint32 rd  [SLOTS]      // 主机消费者的读指针
  char   data[SLOTS][RING]// RING=512 字节的环形数据
  uint32 lost[SLOTS]      // 溢出丢弃计数
```

其中占用率 \( \text{occupancy} = wr - rd \)，容量 \( \text{RING} = 512 \)。写入位置用环形掩码 \( wr \,\&\, (RING-1) \)。因为 `RING` 是 2 的幂（\( 2^9 \)），所以「mod RING」可以用按位与实现。

#### 4.1.3 源码精读

**① 公开 API 只是声明**。[vx_print.h:23-28](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_print.h#L23-L28) 暴露了五个函数，实现分散在 `vx_print.c`（C 逻辑）与 `vx_print.S`（`vx_putchar` 汇编）。

**② `vx_printf` 用 `vx_serial` 串行化**。看 [vx_print.c:88-103](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/src/vx_print.c#L88-L103)：

```c
int vx_vprintf(const char* format, va_list va) {
    printf_arg_t arg;
    arg.format = format;
    arg.va = &va;
    vx_serial((vx_serial_cb)__vprintf_cb, &arg);   // 串行化执行
    return arg.ret;
}
```

关键点是：`vx_printf` 本身不做格式化，它把工作打包成 `printf_arg_t`，交给 `vx_serial`。回调 [__vprintf_cb](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/src/vx_print.c#L70-L72) 才真正调用 `tiny_vprintf`。

**③ `vx_serial` 是 SPLIT/JOIN 的经典应用**。看 [vx_serial.S:20-75](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/src/vx_serial.S#L20-L75)，核心循环（节选）：

```asm
    csrr s2, VX_CSR_NUM_THREADS   # NT：warp 内线程数
    csrr s1, VX_CSR_THREAD_ID     # tid：自己的线程号
label_loop:
    sub  t0, s0, s1
    seqz t1, t0                   # t1 = (index == tid) ? 1 : 0
    .insn r RISCV_CUSTOM0, 2, 0, s5, t1, x0   # SPLIT s5, t1
    bnez t0, label_join           # 当前线程不是 index，跳过回调
    mv   a0, s3
    jalr s4                       # callback(arg)
label_join:
    .insn r RISCV_CUSTOM0, 3, 0, x0, s5, x0   # JOIN s5
    addi s0, s0, 1
    blt  s0, s2, label_loop
```

它的语义是：遍历 `index` 从 0 到 `NT-1`，每一轮用 SPLIT 只激活 `tid==index` 的那个线程去跑回调，其余线程在 JOIN 处汇聚。于是 warp 内的线程被强制**轮流**执行 printf 体——同一个 warp 不会出现两个线程同时写字符，从而保证单次 `vx_printf` 的字符串完整。注意它**不跨 warp 串行化**：不同 warp（不同 hart）并发打印是允许的，因为它们写各自的 COUT 槽。

**④ 格式化由 tinyprintf 完成**。[tiny_printf](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/src/tinyprintf.c#L858-L865) 与 [tiny_vprintf](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/src/tinyprintf.c#L883-L886) 都把 `_out_char` 作为字符出口，而 [_out_char](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/src/tinyprintf.c#L149-L155) 直接调 `vx_putchar(character)`。选用 tinyprintf 而非 newlib `printf` 的原因写在文件头注释里：newlib 的 printf 会用 `malloc` 且不保证线程安全，在裸机多 hart 环境下不可接受。

**⑤ `vx_putchar` 直接操作 COUT 环**。这是整条链路里最值得读的一段，[vx_print.S:37-66](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/src/vx_print.S#L37-L66)（`a0` = 待写字符）：

```asm
vx_putchar:
    csrr  t0, VX_CSR_MHARTID
    andi  t0, t0, %lo(VX_MEM_IO_COUT_SLOTS-1)   # t0 = hart 对应的槽号
    li    t1, VX_MEM_IO_COUT_ADDR               # 缓冲区基址
    ...
    lw    t5, 0(t3)              # t5 = wr（本 hart 拥有，单写者）
    li    t6, VX_MEM_IO_COUT_RING
    lw    a1, 0(t4)              # a1 = rd（主机写，这里只取一次快照）
    sub   a2, t5, a1             # 占用率 = wr - rd
    bgeu  a2, t6, .Lvx_putchar_drop   # 占用率 >= RING → 满，丢弃
    ...                          # 计算 &data[slot][wr & (RING-1)]
    sb    a0, 0(a1)              # 写入字符
    fence ow, ow                 # 发布屏障：字符必须先于 wr 对主机可见
    addi  t5, t5, 1
    sw    t5, 0(t3)              # 发布 wr+1
    ret
```

两个细节决定了正确性：

- **单写者模型**：每个槽的 `wr` 只由拥有该槽的 hart 写，所以 `wr` 的自增无需原子指令；`rd` 由主机写，设备只读一次快照。
- **`fence ow, ow` 发布屏障**：必须先把字符字节写全局可见，再推进 `wr`。否则主机可能观察到 `wr` 已前进、却读到尚未写入的数据字节。

满槽时的丢弃路径 [vx_print.S:67-76](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/src/vx_print.S#L67-L76) 用 `amoadd.w` 原子地把 `lost[slot]` 加一（因为当 hart 总数 > SLOTS 时多个 hart 会共享一个槽），然后返回——**绝不阻塞**。

**⑥ 主机侧排空环**。独立 SimX 没有主机运行时的 launch-wait 轮询循环，所以必须在主循环里自己排空，否则环一满 kernel 就会丢字节点甚至（在旧的阻塞实现里）死锁。看 [main.cpp:227-231](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/main.cpp#L227-L231)：

```cpp
CoutDrainer cout(ram);            // 构造时把 wr[]/rd[]/lost[] 清零
while (processor.cycle()) {
    cout.tick();                  // 每个 cycle 排空一次
}
cout.tick();                      // 退出后再 flush 一次未结尾的行
```

`CoutDrainer::tick()` 的实现在 [cout_drainer.cpp:40-75](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/cout_drainer.cpp#L40-L75)：对每个槽读 `wr[]` 与 `lost[]`，把 `[rd, wr)` 的字节拷进一个 `line_[slot]` 缓冲，遇到 `'\n'` 就以 `#slot: ` 前缀输出整行；发现 `lost` 增长就打印 `[#slot: lost K bytes]`；最后把推进后的 `rd[]` 写回设备内存。注意它的布局常量与 `vx_putchar` 完全一致（[cout_drainer.cpp:23-28](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/cout_drainer.cpp#L23-L28)），这是软硬两端共享 `VX_types.toml` 契约的直接体现。

> 在真实驱动后端（非独立 simx）里，等价的排空逻辑在主机运行时 `Device::drain_cout` 里，由 CP launch-wait 轮询驱动，语义与 `CoutDrainer` 完全一致（见 [cout_drainer.h:25-32](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/cout_drainer.h#L25-L32) 的注释）。

#### 4.1.4 代码实践

**目标**：跟踪一条 `vx_printf` 从源码到主机终端的完整调用链。

**步骤**：

1. 打开 [tests/regression/printf/kernel.cpp:5-11](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/printf/kernel.cpp#L5-L11)，确认内核里调用了 `vx_printf("cid=%d: task=%d, value=%c\n", cid, gid, value)`。
2. 依次跳读：`vx_printf`（vx_print.c）→ `vx_serial`（vx_serial.S）→ `tiny_vprintf`（tinyprintf.c）→ `_out_char` → `vx_putchar`（vx_print.S）→ `CoutDrainer::tick`（cout_drainer.cpp）。
3. 在每一处用一句话写下「这里数据是什么形态」：变量参数 → 打包结构 → 字符流 → 环中字节 → 主机字符串。

**需要观察的现象 / 预期结果**：你应当得到一张 6 步的链路图。特别注意 `vx_putchar` 里那道 `fence ow, ow`，以及 `CoutDrainer` 输出每行时带的 `#slot:` 前缀——它对应写该行的 hart 槽号。运行步骤见第 5 节综合实践。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `vx_putchar` 自增 `wr` 时不需要 `amoadd` 之类的原子指令，但满槽自增 `lost` 时却需要？

> 答案：每个槽的 `wr` 是**单写者**——只有拥有该槽的 hart 会写它（hart 折叠到槽，常态下一对一），所以普通 `sw` 即可。而 `lost[slot]` 在「hart 总数 > SLOTS」时会被多个 hart 并发自增，因此必须用 `amoadd.w` 原子累加。

**练习 2**：如果删掉 `vx_putchar` 里的 `fence ow, ow`，可能会出现什么错误现象？

> 答案：字符字节（`sb`）和写指针推进（`sw wr+1`）之间失去顺序保证。主机排空时可能看到 `wr` 已前进，于是去读对应数据字节，却读到尚未写入的旧值，导致打印出乱码或空字符。`fence` 强制「数据先于指针可见」。

**练习 3**：`vx_serial` 串行化了 warp 内的线程，那两个不同 warp 同时调用 `vx_printf` 会冲突吗？

> 答案：不会字符交错。不同 warp 的线程拥有不同 hart id，会落到不同 COUT 槽（hartid mod SLOTS），各自有独立的 `wr`/`data`。冲突只会在 hart 总数超过 64 个槽时发生，那时 `lost` 用原子加来正确计数丢弃。

---

### 4.2 vx_barrier：warp 间同步与栅栏语义

#### 4.2.1 概念说明

并发程序里「栅栏（barrier）」表示一组执行者必须全部到达某点后才能一起继续。在 Vortex 的 SIMT 模型里，栅栏由一条 WCTL 类（custom0, `func3=4/6`）指令实现，硬件里有专门的 **barrier unit** 跟踪每个栅栏的到达计数与阶段号。

Vortex 提供了两种 API 风格：

- **C 风格内联函数**（`vx_intrinsics.h`）：`vx_barrier`（同步，到达即等）、`vx_barrier_arrive`（异步，到达但不阻塞）、`vx_barrier_wait`（阻塞到指定阶段完成）、`vx_barrier_expect_tx`（预登记异步事务事件）。
- **C++ 封装**（`vx_barrier.h`）：`barrier`（CTA/本地组内）、`gbarrier`（跨核全局）、`group_barrier`（跨 CTA 会合，主要用于 DXA 多播前的同步）。

CUDA 风格的 `__syncthreads()` 本质就是 `vx_barrier(get_local_group_id(), get_num_sub_groups())`——对本 CTA 内的所有 warp 做一次同步栅栏。

#### 4.2.2 核心流程

栅栏的核心是**阶段号（phase）**这一代际概念。每个硬件栅栏槽维护 `count`（已到达数）、`wait_mask`（挂起的 warp）、`phase`（第几代）。对于本地栅栏：

```
每个 warp 执行 vx_barrier(id, N)：
  arrive(id, N, wid, is_sync=true)
    ├─ wait_mask.set(wid)          // 同步栅栏：自己也挂起
    ├─ count++
    └─ if (count+1 == N && events==0):
          resume 所有 wait_mask 里的 warp
          wait_mask.clear()
          phase++                   // 进入下一代
          count = (count+1) % N
```

异步 arrive/wait 分离的用法：

```
phase = vx_barrier_arrive(id, N)    // 报到但不挂起，返回当前阶段号
... 干别的活 ...
vx_barrier_wait(id, phase)          // 阻塞直到 phase 已经翻代（barrier.phase > phase）
```

`wait` 的判定很简单：`wait = (barrier.phase == phase)`——如果阶段还没翻，说明大家还没到齐，当前 warp 挂起；翻代后由 `resume` 唤醒。

`expect_tx` 是为异步数据传输（如 DXA 多播 DMA）设计的：在发起传输**之前**预先登记「还要等 K 个完成事件」，这样即使所有 warp 都已 arrive，栅栏也会等到 `events` 归零才翻代。

栅栏 id 的编码约定（见 [vx_barrier.h](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_barrier.h) 三种构造函数）：

- `barrier`：`bar_id = (id << 8) + local_group_id`——CTA 本地，每个 CTA 独占一个硬件槽。
- `gbarrier`：`bar_id = (id << 8) | 0x80000000`——最高位 1 标记全局，跨核同步。
- `group_barrier`：`bar_id = (id << 8)`——不带 CTA id，所有 CTA 共享同一槽，用于 DXA 多播前的会合。

#### 4.2.3 源码精读

**① `__syncthreads()` 的一行定义**。[vx_spawn.h:64-65](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_spawn.h#L64-L65)：

```c
#define __syncthreads() \
  vx_barrier(get_local_group_id(), get_num_sub_groups())
```

`get_num_sub_groups()` 返回本 CTA 的 warp 数（[vx_spawn.h:55-58](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_spawn.h#L55-L58) 读 `__warps_per_group`），所以 `N` = CTA 内 warp 总数。

**② 同步栅栏是一条 custom0 指令**。[vx_intrinsics.h:166-169](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_intrinsics.h#L166-L169)：

```c
inline void vx_barrier(int barried_id, int num_warps) {
    __asm__ volatile (".insn r %0, 4, 0, x0, %1, %2"
        :: "i"(RISCV_CUSTOM0), "r"(barried_id), "r"(num_warps) : "memory");
}
```

`func3=4` 表示同步栅栏（arrive + wait 合一），`rd=x0`。`"memory"` clobber 防止编译器把栅栏前后的访存重排到栅栏另一侧——这是同步原语必备的屏障。

**③ 异步 arrive/wait 用 `rd` 区分**。看 [vx_barrier_arrive](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_intrinsics.h#L494-L500)（`func3=6`，`rd` 为真实寄存器，返回 phase）与 [vx_barrier_wait](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_intrinsics.h#L505-L509)（同 opcode，但 `rd=x0` 表示 wait 而非 arrive）。解码器正是用 `rd != 0` 来区分 arrive 与 wait，这一点 [expect_tx 的注释](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_intrinsics.h#L517-L521) 明确说明了。

**④ `expect_tx` 复用 arrive 操作码但置 `rs2[31]=1`**。[vx_barrier_expect_tx](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_intrinsics.h#L522-L529)：

```c
inline void vx_barrier_expect_tx(int barrier_id, int count) {
    int discard;
    int rs2 = count | 0x80000000;   // 最高位置 1，标志 expect-tx 语义
    __asm__ volatile (".insn r %1, 6, 0, %0, %2, %3"
        : "=r"(discard) : "i"(RISCV_CUSTOM0), "r"(barrier_id), "r"(rs2) : "memory");
    ...
}
```

**⑤ SimX 的 barrier unit 是 RTL 的预言机**。[barrier_unit.cpp 的 arrive](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/barrier_unit.cpp#L43-L94) 把上面流程图的语义精确实现：本地栅栏到达时 `count++`，当 `count+1 == N && events==0` 时唤醒所有 `wait_mask` 中的 warp 并 `++phase`，`count` 按 `(count+1) % N` 回绕。[wait](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/barrier_unit.cpp#L96-L111) 则是 `wait = (phase == 请求的phase)`。[event_release](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/barrier_unit.cpp#L135-L167) 在异步事务完成时把 `events--`，归零后才允许翻代——这正是 expect_tx 等待 DMA 完成的机制。

**⑥ C++ 封装的便捷性**。[barrier 类](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_barrier.h#L21-L59) 把「构造时算好 `bar_id`」与「arrive/wait/arrive_and_wait/expect_tx」打包，让你不必手写 id 编码。注意它构造时默认 `num_warps = get_num_sub_groups()`，与 `__syncthreads()` 一致。

#### 4.2.4 代码实践

**目标**：验证 `__syncthreads()` 与 `vx_barrier` 的等价性，并跟踪到硬件 barrier unit。

**步骤**：

1. 在 [vx_spawn.h:64-65](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_spawn.h#L64-L65) 阅读 `__syncthreads()` 宏定义，确认它展开为 `vx_barrier(get_local_group_id(), get_num_sub_groups())`。
2. 跳到 `vx_barrier` 内联汇编，记录它的 opcode 字段（custom0、`func3=4`、`rd=x0`）。
3. 在 [barrier_unit.cpp 的 arrive](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/barrier_unit.cpp#L43-L94) 找到「`count+1 == N && events==0` 时唤醒 + `phase++`」这段，对照流程图理解。
4. 打开一个用到 `__syncthreads()` 的回归测试（例如 `tests/regression` 下涉及本地内存交换的例子），确认它编译后确实发射了 custom0 `func3=4` 指令。

**需要观察的现象 / 预期结果**：你能写出「`__syncthreads()` → `vx_barrier` → WCTL 指令 → barrier_unit::arrive → N 个 warp 到齐后 phase++ 并 resume」这条链。若要确认指令发射，可在 SimX 用 `--debug` 看解码日志（见 u13-l2），属于待本地验证项。

#### 4.2.5 小练习与答案

**练习 1**：`vx_barrier_arrive` 返回的 `phase` 是什么？`vx_barrier_wait(id, phase)` 何时返回？

> 答案：`phase` 是调用 arrive 时该栅栏的当前代际编号。`wait` 的判定是 `barrier.phase == phase`——只要阶段还没翻代就挂起；一旦所有参与者到齐（`count` 满足）并 `++phase`，`barrier.phase > 请求的phase` 成立，wait 返回。所以「等所有人到齐」=「等阶段号翻代」。

**练习 2**：`barrier` 与 `group_barrier` 在 `bar_id` 编码上的关键区别是什么？为什么 DXA 多播前要用 `group_barrier`？

> 答案：`barrier` 把 `local_group_id` 编进 `bar_id`（`(id<<8)+local_group_id`），所以每个 CTA 用各自的硬件槽，互不影响。`group_barrier` 不编 CTA id（`(id<<8)`），所有 CTA 共享同一个槽，于是能实现「跨 CTA 会合」。DXA 多播要求所有接收 CTA 在发射者点火前都调好 `expect_tx`，这种跨 CTA 的顺序约束只能用共享槽的 `group_barrier` 来保证。

**练习 3**：为什么 `vx_barrier` 的内联汇编要加 `"memory"` clobber？

> 答案：栅栏是同步原语，必须禁止编译器把栅栏之前的访存重排到栅栏之后（或反之），否则会破坏「所有 warp 在栅栏处看到彼此之前写的数据」这一语义。`"memory"` 告诉编译器这条指令可能读写任意内存，从而充当编译期内存屏障。

---

### 4.3 轻量系统调用：newlib 桩与 libc 衔接

#### 4.3.1 概念说明

Vortex 设备内核是裸机（bare-metal）程序，没有操作系统，因此也没有真正的「系统调用 trap」。但它链接了 newlib（一个面向嵌入式系统的 C 库），而 newlib 的 `printf`/`write`/`malloc`/`getpid` 等高层函数最终都要回调一组以下划线开头的「系统调用桩」：`_write`、`_read`、`_sbrk`、`_close`、`_fstat`、`_isatty`、`_lseek`、`_open`、`_kill`、`_getpid`。

[vx_syscalls.c](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/src/vx_syscalls.c) 就是这组桩的实现。它的策略很务实：

- **能对接设备原语的，就对接**：`_write` 把每个字节通过 `vx_putchar` 送进 COUT 环，于是 newlib 的 `printf`/`puts`/`putchar` 也能正常输出（尽管推荐用 `vx_printf`）；`_getpid` 返回 `vx_hart_id()`。
- **用不到 / 不支持的，就返回无害值**：`_close`/`_open`/`_read`/`_fstat` 等大多返回 `-1`，`_lseek` 返回 `0`。
- **显式禁用危险操作**：`_sbrk`（堆扩展，newlib `malloc` 的基石）执行 `ebreak` 并返回 0——也就是「裸机内核不支持动态堆分配」，强行 malloc 会陷入调试断点。这正是设备内核用 tinyprintf 而非 newlib `printf` 的根本原因。

此外，`vx_syscalls.c` 还承担了两件裸机启动杂务：TLS（线程局部存储）初始化 `__init_tls`，以及 C++/C 全局构造与析构数组的遍历 `__libc_init_array`/`__libc_fini_array`（后者由 `__funcs_on_exit` 在退出时调用）。

#### 4.3.2 核心流程

newlib 桩在内核生命周期中的位置：

```
CTA 入口 __vx_cta_entry（vx_start.S，见 u4-l1）
   ├─ __init_tls()                 // 复制 .tdata、清零 .tbss（本文件）
   ├─ __libc_init_array()          // 调用所有全局构造函数（本文件）
   ├─ <用户 kernel 运行>
   │     ├─ vx_printf  ──→ （推荐）tinyprintf ──→ vx_putchar ──→ COUT 环
   │     └─ printf/puts ──→ newlib ──→ _write ──→ 循环 vx_putchar ──→ COUT 环
   ├─ kernel 返回
   └─ __funcs_on_exit() ──→ __libc_fini_array()  // 全局析构
```

注意两条打印路径都最终汇到 `vx_putchar`：`vx_printf` 走 tinyprintf（无 malloc、warp 内串行），newlib `printf` 走 `_write`（字符逐个 `vx_putchar`，但**没有** warp 串行化）。所以在内核里应优先使用 `vx_printf`。

#### 4.3.3 源码精读

**① `_write` 是 libc 输出到设备的桥梁**。[vx_syscalls.c:42-48](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/src/vx_syscalls.c#L42-L48)：

```c
int _write(int file, char *ptr, int len) {
  int i;
  for (i = 0; i < len; ++i) {
    vx_putchar(*ptr++);
  }
  return len;
}
```

逐字节 `vx_putchar`——所以任何走 newlib `write` 的输出（含 `printf`、`puts`）都会进 COUT 环。代价是它不像 `vx_printf` 那样在 warp 内串行化，多线程并发用 `printf` 仍可能字符交错。

**② `_sbrk` 用 `ebreak` 拒绝堆扩展**。[vx_syscalls.c:37-40](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/src/vx_syscalls.c#L37-L40)：

```c
caddr_t _sbrk(int incr) {
  __asm__ __volatile__("ebreak");
  return 0;
}
```

`ebreak` 是 RISC-V 的调试断点指令；返回 0 会让 newlib 认为「堆无法增长」。结论：设备内核**不支持 `malloc`/`free`**。

**③ 其余桩大多返回无害值**。[vx_syscalls.c:25-35](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/src/vx_syscalls.c#L25-L35)：`_close`/`_fstat`/`_open`/`_read` 返回 `-1`，`_isatty` 返回 `0`，`_lseek` 返回 `0`。`_getpid`（[L52-L54](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/src/vx_syscalls.c#L52-L54)）返回 `vx_hart_id()`，让 newlib 的 `getpid`/`kill` 抽象有一个唯一标识。

**④ TLS 初始化**。[__init_tls](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/src/vx_syscalls.c#L70-L78) 用当前线程指针 `tp` 把 `.tdata` 复制过去、把 `.tbss` 清零。这里有一个 RV64 的精彩细节：`.tdata`/`.tbss` 的大小是链接器绝对符号，在 `medany` 模型下用 PC 相对寻址读取「符号地址」时，会因为代码段（`0x80000000`）与这些很小的绝对常量之间约 2GB 的跨度而溢出 `R_RISCV_PCREL_HI20` 重定位。所以专门定义了 [VX_ABS_LINKER_SYM](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/src/vx_syscalls.c#L63-L68) 宏，用绝对的 `lui/addi` 来读这些符号（注释在 [L56-L62](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/src/vx_syscalls.c#L56-L62) 解释了 RV32「碰巧能跑」而 RV64 会崩的原因）。

**⑤ 全局构造/析构**。[__libc_init_array](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/src/vx_syscalls.c#L93-L108) 遍历链接器生成的 `__preinit_array_start`/`__init_array_start` 表，逐个调用 C++ 全局构造函数（或 `__attribute__((constructor))`）；[__funcs_on_exit](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/src/vx_syscalls.c#L136-L140) 在退出时反向遍历 fini 数组。注意 `__libc_init_array` 正是 u4-l1 里 `__vx_cta_entry` prologue 调过的那个函数——这条桩文件是 CTC 入口能进入「正常 C/C++ 运行时」的必要条件。

#### 4.3.4 代码实践

**目标**：理解 libc 抽象如何被「最小化」地嫁接到设备原语上。

**步骤**：

1. 在 [vx_syscalls.c](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/src/vx_syscalls.c) 中找出所有「真正调用了 Vortex 设备原语」的桩（答案：`_write`→`vx_putchar`、`_getpid`→`vx_hart_id`），其余桩分别返回什么。
2. 回忆 u4-l1：`__vx_cta_entry` 在派发到用户 kernel 之前调用了 `__libc_init_array`。在本文件找到它的定义，确认它遍历了哪两个构造函数表。
3. 解释：如果有人误在 kernel 里写了 `malloc(100)`，运行时会经由 `_sbrk` 走到 `ebreak`，会发生什么？

**需要观察的现象 / 预期结果**：你应当能画出「newlib 高层 API → `_` 桩 → Vortex 原语（或断点 / 无害返回）」的映射表。`malloc` 在裸机内核里会触发 `ebreak` 陷入调试器（或被忽略后返回 0 导致 newlib 报告分配失败），属于待本地验证项。

#### 4.3.5 小练习与答案

**练习 1**：内核里同时存在 `vx_printf` 和（newlib 的）`printf`，二者最终都把字节送进 COUT 环。它们的关键差别是什么？

> 答案：`vx_printf` 用 tinyprintf 格式化（无 `malloc`、可重入），且经 `vx_serial` 在 warp 内串行化，单次调用的字符串不会被打断。newlib `printf` 走 `_write` 逐字符 `vx_putchar`，没有 warp 串行化，也没有避开 `malloc`（newlib printf 可能调 malloc，但设备 `_sbrk` 已用 ebreak 禁掉堆）。所以内核应优先用 `vx_printf`。

**练习 2**：为什么 `_sbrk` 要用 `ebreak` 而不是直接 `return 0`？

> 答案：直接 `return 0` 会让 newlib 静默地认为堆分配失败，程序可能继续跑出难以定位的错误。`ebreak` 是显式的调试断点，能在第一时间停下来，让开发者立刻看到「设备内核不支持动态堆分配」这一事实，便于排错。这是一种 fail-loud 的裸机设计。

**练习 3**：`__init_tls` 为什么要用 `VX_ABS_LINKER_SYM` 宏（绝对 `lui/addi`）而不是普通的 C 取地址来读 `__tdata_size`？

> 答案：`__tdata_size` 等是链接器 `SIZEOF()` 绝对符号，其「地址」其实是一个很小的常量值。在 RV64 的 `-mcmodel=medany` 下，普通取地址会编译成 PC 相对 `auipc`，而代码段基址 `0x80000000` 与这些小常量之间约 2GB 的跨度会让 `R_RISCV_PCREL_HI20` 重定位溢出（RV32 因 32 位空间回绕而碰巧没事）。用绝对的 `lui/addi` 直接把常量装进寄存器，绕开了 PC 相对重定位，从而在 RV32/RV64 都正确。

---

## 5. 综合实践

**任务**：在 `tests/regression/printf` 里新增一次 `vx_printf` 调用，在 SimX 上运行并观察输出，最后解释 `CoutDrainer` 如何在 `main.cpp` 中排空 COUT 环。

**操作步骤**：

1. 阅读现有内核 [tests/regression/printf/kernel.cpp:5-11](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/printf/kernel.cpp#L5-L11)，它在每个线程里打印 `cid`、`gid`、`value`。
2. 复制一行，新增一条不同格式的打印，例如：
   ```c
   vx_printf("hello from cid=%d tid=%d\n", cid, vx_thread_id());
   ```
   （这是示例代码，请保存为本地修改，不要提交。）
3. 确认已按 u1-l3 在某 `build/` 目录执行过 `../configure`。然后参照 u1-l4 的统一启动器，在仓库根目录运行：
   ```bash
   ./ci/blackbox.sh --driver=simx --app=printf
   ```
   也可直接在 `tests/regression/printf` 里用 `make` 后用 `vortex`（SimX 可执行）运行，具体命令以本仓库 `tests/regression/common.mk` 为准（待本地验证）。
4. 观察终端输出：每行形如 `#N: ...`，其中 `N` 是 COUT 槽号（对应写该行的 hart）。

**需要观察的现象**：

- 输出按 hart 槽分前缀，每个线程的整条字符串保持完整、不与其他线程字符交错（这正是 `vx_serial` 的功劳）。
- 即便多个 warp 并发执行，它们的输出落在不同 `#N` 槽里。
- 如果人为把环打满（例如在 kernel 里死循环打印），会出现形如 `[#N: lost K bytes]` 的丢弃报告（见 `cout_drainer.cpp`）。

**解释 CoutDrainer 如何排空 COUT 环**（这是本实践的核心）：

1. 独立 SimX 没有主机运行时的 launch-wait 轮询，所以在 [main.cpp:227-231](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/main.cpp#L227-L231) 构造 `CoutDrainer cout(ram)`，构造函数（[cout_drainer.cpp:31-38](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/cout_drainer.cpp#L31-L38)）先把设备内存里的 `wr[]`/`rd[]`/`lost[]` 清零，避免未初始化 RAM 被误当成有效写指针。
2. 每个 simulator cycle 调用 `cout.tick()`：对 64 个槽逐一读 `wr[]`，把 `[rd, wr)` 范围内的字节从 `data[]` 拷出，遇 `'\n'` 就以 `#slot:` 前缀打印整行，然后把推进后的 `rd[]` 写回设备内存（[cout_drainer.cpp:40-75](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/common/cout_drainer.cpp#L40-L75)）。
3. 退出主循环后再 `cout.tick()` 一次，flush 末尾未换行的残留。

**预期结果**：你新增的 `vx_printf` 输出会以 `#N:` 前缀出现在终端；能用自己的话讲清「设备写字节进环 → 主机每 cycle 读 wr、拷数据、推进 rd」这条生产者—消费者回路，以及它为何是有界、非阻塞、满了就丢的（与 CUDA/HIP printf 一致）。

> 如果无法在本地运行 SimX，请把本实践降级为「源码阅读型实践」：完成第 1、2 步的代码修改（不编译），并按第 4.1.3 节的链路图讲清数据流即可，明确标注「待本地验证」。

## 6. 本讲小结

- `vx_printf` 的完整链路是：**warp 内 `vx_serial` 串行化 → tinyprintf 格式化 → `vx_putchar` 写 COUT 环 → 主机 `CoutDrainer` 排空**。`vx_serial` 用 SPLIT/JOIN 让 warp 内线程逐个独占执行，保证单次字符串不交错。
- COUT 环是设备 IO 区里**每 hart 一个**的有界环形缓冲（`wr/rd/data/lost`，SLOTS=64、RING=512），满了就丢字节并原子累加 `lost`，**绝不阻塞**——这是 CUDA/HIP printf 语义，避免了 printf 密集 kernel 死锁。
- `vx_putchar` 依赖**单写者模型**（无需原子自增 `wr`）和一道 `fence ow, ow` **发布屏障**（字符先于指针可见）；`CoutDrainer` 与之共享同一份 `VX_types.toml` 布局契约。
- `vx_barrier` 是一条 custom0 WCTL 指令，核心是**阶段号（phase）**代际：到达数到齐且 `events==0` 时翻代并唤醒所有 wait 者；`__syncthreads()` 即 `vx_barrier(local_group_id, num_warps)`。
- 异步 arrive/wait/expect_tx 用 `rd` 是否为 0、`rs2[31]` 等编码区分；C++ 封装 `barrier`/`gbarrier`/`group_barrier` 对应 CTA 本地、跨核全局、跨 CTA 会合三种作用域。
- 设备侧「系统调用」其实是 [vx_syscalls.c](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/src/vx_syscalls.c) 的一组 newlib 桩：`_write` 桥接 `vx_putchar`、`_getpid` 返回 hart id、`_sbrk` 用 `ebreak` 禁掉堆分配；它还负责 `__init_tls` 与 `__libc_init_array`，是 CTC 入口进入正常 C/C++ 运行时的前提。

## 7. 下一步学习建议

- **向下游（主机侧）**：本讲的 COUT 环排空在真实驱动后端里由主机运行时 `Device::drain_cout` 完成，建议接着学 u3-l2（设备、缓冲区与内存管理），看 CP 的 launch-wait 轮询如何驱动它。
- **向下游（命令通路）**：COUT 环与命令处理器（CP）共用设备 IO 区，且 launch 本身也走 CP。可在 u11-l3（命令处理器与 KMU）里看到这套主机↔设备控制/IO 区域的全貌。
- **向纵深（RTL）**：本讲的 barrier unit 与 COUT 访存都强调了「SimX 是 RTL 的预言机」。学完 u7-l4（SimX↔RTL 模型一致性）后，可回到 [barrier_unit.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/barrier_unit.cpp) 与 RTL 侧的 barrier 实现对照，体会 model parity 纪律。
- **异步事务延伸**：`expect_tx` 是为 DXA 异步多播设计的，完整用法在 u9-l2（DXA 异步拷贝与多播）展开，届时可回头看本讲的 `group_barrier` 会合机制。
