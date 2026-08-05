# SIMT 控制指令与 warp 调度 API

## 1. 本讲目标

本讲打开 Vortex 设备侧的「warp 控制工具箱」。读完本讲，你应当能够：

- 说清 Vortex 用哪 6 条自定义指令（TMC / WSPAWN / SPLIT / JOIN / PRED / BAR）操纵 SIMT 执行，以及它们如何塞进 RISC-V 的 custom 指令槽位。
- 读懂 `vx_intrinsics.h` 中每一个 warp 控制内联函数，知道它对应哪条硬件指令、改动了哪个 warp 状态。
- 用 SPLIT/JOIN 配合 IPDOM 栈解释一次 warp 内分支发散与汇聚的过程。
- 区分 Vortex 的两套「派生抽象」：`vx_spawn.h` 的**软件派生**（kernel 自己用 `vx_wspawn`+`vx_tmc` 拉起 warp）与 `vx_spawn2.h` 的**硬件派生**（KMU/CTA 调度器自动派生，kernel 只读 CSR）。
- 在一份真实 kernel 源码里辨认出 warp 派生与线程激活发生在哪里（或为何不可见）。

## 2. 前置知识

本讲假设你已经读过 [u4-l1 内核运行时启动与入口模型](u4-l1-kernel-startup.md)，知道设备侧有统一入口 `__vx_cta_entry`、kernel 入口 PC 来自 `VX_CSR_CTA_ENTRY`。下面补充理解 warp 控制必需的 SIMT 基础。

### 2.1 SIMT 执行模型速览

Vortex 采用 SIMT（Single Instruction, Multiple Threads）模型，**每个周期只发射一个 warp**。层次关系如下（详见 [docs/designs/microarchitecture.md:5-16](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/microarchitecture.md#L5-L16)）：

- **Thread（线程）**：最小计算单位，每个线程私有 32 个整型 + 32 个浮点寄存器。
- **Warp（线程束）**：一组线程的逻辑簇。同一 warp 内**所有线程共享同一条 PC**，靠一个「线程掩码（thread mask，简称 tmask）」决定哪些线程真正参与写回。
- **时间复用**：多个 warp 在不同周期轮流发射，以隐藏延迟。

关键直觉：**PC 是 warp 级的、寄存器是 thread 级的**。所以「谁在执行」由两个量决定——当前 warp 有多少线程激活（tmask）、当前 core 有多少 warp 激活。本讲的 6 条指令，本质上就是在改这两个量。

### 2.2 六条类 GPU 控制指令

Vortex 只对 RISC-V 做最小化扩展，加入 6 条控制指令（见 [docs/designs/microarchitecture.md:18-32](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/microarchitecture.md#L18-L32)）：

| 指令 | 作用 | 改变的状态 |
|------|------|-----------|
| `TMC` count | 激活 count 个线程 | 当前 warp 的 tmask |
| `WSPAWN` count, addr | 激活 count 个 warp 并跳到 addr | core 的 active warp 集合 |
| `SPLIT` taken, predicate | 分支发散：保存当前状态入 IPDOM 栈，按谓词分流 | IPDOM 栈 + tmask |
| `JOIN` | 分支汇聚：弹 IPDOM 栈恢复 tmask | IPDOM 栈 + tmask |
| `PRED` predicate, restore_mask | 设置线程谓词掩码 | tmask |
| `BAR` id, count | warp 屏障：凑齐 count 个 warp 才放行 | 屏障计数 |

这 6 条指令没有独立的 opcode——它们复用 RISC-V 的 custom0 槽位（`0x0B`），靠指令字内的 `func3`/`func7` 字段区分。这正是下一节 `vx_intrinsics.h` 要包装的东西。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [sw/kernel/include/vx_intrinsics.h](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_intrinsics.h) | warp 控制内联函数库：每条 SIMT 指令的 C 包装，外加 CSR 读写、投票、跨 lane 洗牌、周期计数等 |
| [sw/kernel/include/vx_spawn.h](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_spawn.h) | 软件派生 API：CUDA 风格的 `blockIdx`/`threadIdx`/`gridDim`/`blockDim` 全局量与 `vx_spawn_threads` 声明 |
| [sw/kernel/src/vx_spawn.c](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/src/vx_spawn.c) | 软件派生实现：`vx_spawn_threads` 如何用 `vx_wspawn`+`vx_tmc` 把一个 kernel 撒到多个 warp/线程上 |
| [sw/kernel/include/vx_spawn2.h](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_spawn2.h) | 硬件派生 API（KMU 模型）：`blockIdx`/`threadIdx` 直接读 CTA CSR，无显式 spawn |
| [tests/kernel/conform/tests.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/kernel/conform/tests.cpp) | 显式使用 `vx_split`/`vx_join`/`vx_wspawn`/`vx_tmc` 的对照测试，本讲的「标准答案」 |
| [tests/regression/demo/kernel.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/demo/kernel.cpp) | 综合实践对象：用 `vx_spawn2.h` 的向量加法 kernel |

## 4. 核心概念与源码讲解

### 4.1 模块一：vx_intrinsics.h —— warp 控制内联函数

#### 4.1.1 概念说明

`vx_intrinsics.h` 是设备侧最底层的 SIMT 控制接口。它的设计哲学很简单：**每一条 warp 控制内联函数，就是一条 RISC-V 自定义指令的 C 语言包装**。编译器后端（VOLT）在处理普通的 `if/else` 时，会自动为发散分支发射 `SPLIT`/`JOIN`；但当你需要手动控制线程激活、派生 warp、或做跨 lane 通信时，就直接调用这里的内联函数。

这些函数都用 GNU as 的 `.insn` 伪指令直接拼出机器码。`.insn r` 的参数顺序是：

```
.insn r opcode, func3, func7, rd, rs1, rs2
```

所有 warp 控制 + 协作线程指令都用同一个 opcode = `RISCV_CUSTOM0`（`0x0B`，见 [sw/kernel/include/vx_intrinsics.h:34-37](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_intrinsics.h#L34-L37)），再用 `func7` 把它们分成两族：

- `func7 = 0`：**warp 控制族**（TMC/WSPAWN/SPLIT/JOIN/BAR/PRED/WSYNC），由 `func3` 区分具体操作。
- `func7 = 1`：**协作线程族**（vote 投票 / shuffle 跨 lane 洗牌），同样由 `func3` 区分。

另外，文件里还有一个对编译器很重要的标注 `__UNIFORM__`（[sw/kernel/include/vx_intrinsics.h:24-28](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_intrinsics.h#L24-L28)），它展开成 `annotate("vortex.uniform")`，告诉后端「这个值在所有 lane 上都相同」，从而不会被当作发散分支处理——它和 SPLIT/JOIN 是一体两面的发散控制手段。

#### 4.1.2 核心流程

把内联函数按「改了哪个状态」分组，调用流程如下：

```text
派生维度（拉起多少执行者）
  vx_wspawn(num_warps, func)   ── 激活 num_warps 个 warp 跳到 func
        │
线程维度（每个 warp 激活多少线程）
  vx_tmc(mask) / vx_tmc_one() / vx_tmc_zero()  ── 设置 tmask
        │
发散控制（warp 内 if/else）
  sp = vx_split(predicate)     ── 压栈 + 按谓词只留「真」的 lane
        │   ... then 分支 ...
  vx_join(sp)                  ── 弹栈，恢复之前被挂起的 lane
        │
同步
  vx_barrier(id, count)        ── warp 间屏障
  vx_wsync()                   ── 等 warp 内所有 in-flight 指令完成
```

身份与拓扑查询（`vx_thread_id`/`vx_warp_id`/`vx_core_id`/`vx_num_threads` 等）则是只读 CSR，不改状态，供 kernel 计算自己的全局坐标。

#### 4.1.3 源码精读

**(1) 线程掩码控制 TMC**

设置 tmask 的三种用法（[sw/kernel/include/vx_intrinsics.h:114-129](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_intrinsics.h#L114-L129)）：

```c
inline void vx_tmc(int thread_mask) {            // func3=0
    __asm__ volatile (".insn r %0, 0, 0, x0, %1, x0" :: "i"(RISCV_CUSTOM0), "r"(thread_mask) : "memory");
}
inline void vx_tmc_zero() { ... }                // 关闭全部线程
inline void vx_tmc_one()  { ... "li a0, 1" ... } // 只留线程 0
```

`vx_tmc(mask)` 把传入的位掩码写进当前 warp 的 tmask。例如 `vx_tmc(-1)`（全 1）激活所有线程，`vx_tmc(0x5)` 只激活第 0、2 号线程。`"memory"` clobber 告诉编译器这条内联汇编有内存副作用，不要把它和周围的访存指令乱序交换。

**(2) 派生 warp WSPAWN**

[sw/kernel/include/vx_intrinsics.h:142-145](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_intrinsics.h#L142-L145) —— `func3=1`：

```c
typedef void (*vx_wspawn_pfn)();
inline void vx_wspawn(int num_warps, vx_wspawn_pfn func_ptr) {
    __asm__ volatile (".insn r %0, 1, 0, x0, %1, %2" :: "i"(RISCV_CUSTOM0), "r"(num_warps), "r"(func_ptr) : "memory");
}
```

`rs1=num_warps`、`rs2=func_ptr`：激活 `num_warps` 个 warp 并让它们都从 `func_ptr` 开始执行。调用者（通常是 warp 0）自身继续往下走。注意 `vx_wspawn` 是「发后即走」的，调用者要自己负责同步等待（见模块二的 `vx_wspawn(1, 0)` 收尾技巧）。

**(3) 分支发散 SPLIT / JOIN 与 IPDOM 栈**

这是本模块最核心的一对（[sw/kernel/include/vx_intrinsics.h:148-164](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_intrinsics.h#L148-L164)）：

```c
inline int vx_split(int predicate) {     // func3=2，rd=返回栈指针
    int ret;
    __asm__ volatile (".insn r %1, 2, 0, %0, %2, x0" : "=r"(ret) : "i"(RISCV_CUSTOM0), "r"(predicate) : "memory");
    return ret;
}
inline void vx_join(int stack_ptr) {     // func3=3
    __asm__ volatile (".insn r %0, 3, 0, x0, %1, x0" :: "i"(RISCV_CUSTOM0), "r"(stack_ptr) : "memory");
}
```

`vx_split(predicate)` 做三件事：① 把「当前 tmask」与「谓词为假的 tmask」压入 **IPDOM 栈**；② 把 tmask 改成「谓词为真的 lane」；③ 返回一个**栈指针** `sp`，作为稍后汇聚的凭证。`vx_join(sp)` 则弹出该凭证，把之前被挂起的 lane 重新并回 tmask。

IPDOM 即 Immediate Post-Dominator（直接后支配节点）——分支两边必然汇合的最近指令。Vortex 用一个硬件栈在 SPLIT/JOIN 之间保存「另一条路径的 tmask」，从而先跑完 then 分支、再跑 else 分支，最后在汇聚点合流。

下面用一个 4 线程 warp、谓词 `tid < 2` 走一遍 tmask 演化（位 0 = thread 0，最右是最低位）：

\[
\text{tmask}_{\text{初始}} = 1111_2
\]

执行 `sp = vx_split(tid < 2)`：栈里压入「假」侧 `1100`，tmask 变成「真」侧：

\[
\text{tmask}_{\text{then}} = 0011_2 \quad(\text{thread }0,1)
\]

跑完 then 分支后 `vx_join(sp)`：弹出 `1100`，tmask 恢复为：

\[
\text{tmask}_{\text{汇聚}} = 1100_2 \quad(\text{thread }2,3)
\]

继续跑 else 分支，再次 `vx_join`（外层）恢复成 `1111`。整个过程 PC 始终只有一条，靠 tmask 决定谁写回。

> 顺带一提：`vx_split_n`/`vx_pred_n` 是「非」版本，靠把指令字里的 `rs2` 从 `x0` 换成 `x1` 来区分（见 [sw/kernel/include/vx_intrinsics.h:155-159](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_intrinsics.h#L155-L159)）。

**(4) warp 屏障 BAR 与同步**

[sw/kernel/include/vx_intrinsics.h:167-169](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_intrinsics.h#L167-L169)：`vx_barrier(id, count)` 让进入屏障 `id` 的 warp 阻塞，直到凑齐 `count` 个。`vx_wsync()`（[sw/kernel/include/vx_intrinsics.h:244-246](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_intrinsics.h#L244-L246)，`func3=7`）则只等当前 warp 的在途指令排空，是 kernel 收尾常用的轻量同步。

**(5) 协作线程：投票与跨 lane 洗牌**

`func7=1` 这一族让 warp 内的 lane 互相通信。例如 `vx_vote_ballot` 返回一个位掩码，第 i 位为 1 表示 lane i 的谓词为真（[sw/kernel/include/vx_intrinsics.h:403-407](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_intrinsics.h#L403-L407)）；`vx_shfl_idx` 让每个 lane 从指定 lane 收集一个值（[sw/kernel/include/vx_intrinsics.h:434-438](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_intrinsics.h#L434-L438)）。它们是实现规约、转置等 warp 级算法的基础。

**(6) 身份查询 CSR**

`vx_thread_id()`/`vx_warp_id()`/`vx_core_id()` 等用 `csr_read_nv`（non-volatile）读 CSR（[sw/kernel/include/vx_intrinsics.h:48-52](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_intrinsics.h#L48-L52)、[173-185](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_intrinsics.h#L173-L185)）。因为这些值在线程生命期内恒定，函数标了 `__attribute__((const))` + 非volatile 读，编译器可以把它们公共子表达式消除、提到循环外。相反，`vx_active_threads()`（[sw/kernel/include/vx_intrinsics.h:188-190](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_intrinsics.h#L188-L190)）会随发散变化，用 volatile 读，每次都重新取。CSR 的编号定义在 `VX_types.toml`，例如 `VX_CSR_THREAD_ID = 0xCC0`（[VX_types.toml:526-528](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_types.toml#L526-L528)）。

#### 4.1.4 代码实践

**实践目标**：用一个真实测试，亲眼看清 SPLIT/JOIN 如何与 IPDOM 栈配合管理 tmask。

**操作步骤**：

1. 打开 [tests/kernel/conform/tests.cpp:147-183](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/kernel/conform/tests.cpp#L147-L183) 的 `do_divergence()` 函数，这是手工写出的嵌套发散：

   ```c
   int tid = vx_thread_id();
   int cond1 = tid < 2;
   int sp1 = vx_split(cond1);   // 第一层分流
   if (cond1) {
       int cond2 = tid < 1;
       int sp2 = vx_split(cond2);  // 第二层（then 侧再分流）
       ...
       vx_join(sp2);
   } else {
       int cond2 = tid < 3;
       int sp2 = vx_split(cond2);  // 第二层（else 侧再分流）
       ...
       vx_join(sp2);
   }
   vx_join(sp1);                 // 第一层汇聚
   ```

2. 假设 4 个线程（tmask=`1111`），按下表跟踪每次 `vx_split`/`vx_join` 后的 tmask 与 IPDOM 栈内容：

   | 执行点 | tmask | IPDOM 栈（顶在右） |
   |--------|-------|--------------------|
   | 初始 | `1111` | `[]` |
   | `vx_split(tid<2)` 后（跑 then） | `0011` | `[1100]` |
   | `vx_split(tid<1)` 后 | `0001` | `[1100, 0010]` |
   | `vx_join(sp2)` 后 | `0010` | `[1100]` |
   | `vx_join(sp1)` 后（跑 else） | `1100` | `[]` |
   | else 内 `vx_split(tid<3)` 后 | `0100` | `[1000]` |
   | `vx_join` 后 | `1000` | `[]` |
   | 最外层 `vx_join(sp1)` 后 | `1111` | `[]` |

3. 注意 `test_divergence()` 在调用前后用 `vx_tmc(tmask)`/`vx_tmc_one()` 控制激活线程数（[tests/kernel/conform/tests.cpp:185-195](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/kernel/conform/tests.cpp#L185-L195)）。

**需要观察的现象**：尽管 4 个线程走不同的 if/else 路径，PC 始终单条推进，tmask 决定每一步谁真正写回 `dvg_buffer[tid]`。

**预期结果**：跟踪完成后，`dvg_buffer` 应当被 4 个线程各自正确写入，最终 `check_error` 返回 0（无错）。具体每个 tid 写入的字符（A/B/C/D）可由源码的分支条件推出，作为自验答案。

> 说明：本实践为**源码阅读型实践**。如需在 SimX 上实跑 conform 测试，可用 `./ci/blackbox.sh --driver=simx --app=conform`（若该 app 在当前 testcases 目录中可用），运行结果待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：`vx_wspawn` 与 `vx_tmc` 分别改变哪个层次的「激活集合」？为什么 Vortex 需要两层？

**参考答案**：`vx_wspawn` 改变 **warp 层**（core 内有多少 warp 激活），`vx_tmc` 改变 **线程层**（一个 warp 内有多少线程激活）。需要两层是因为 SIMT 里 PC 是 warp 级共享的、寄存器是 thread 级私有的：先用 WSPAWN 决定「派几组 warp 并行」，再用 TMC 决定「每组 warp 内多少线程一起跑同一条指令」。

**练习 2**：如果把 `vx_split` 返回的栈指针 `sp` 丢弃、直接调用无参的 `vx_join`，会发生什么？（提示：看 `vx_join` 的签名。）

**参考答案**：`vx_join` 需要传入 `stack_ptr` 参数（[sw/kernel/include/vx_intrinsics.h:162-164](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_intrinsics.h#L162-L164)），它告诉硬件弹 IPDOM 栈的哪一项。丢弃 `sp` 就无法正确配对 SPLIT/JOIN，会导致栈失配、tmask 恢复错误——这正是为什么源码里每次 `vx_split` 都严格把返回值喂给对应的 `vx_join`。

**练习 3**：`vx_thread_id()` 标了 `const` 并用 `csr_read_nv`，而 `vx_active_threads()` 用 volatile 的 `csr_read`。请解释原因。

**参考答案**：线程 id 在其生命期内不变，标 `const` + 非volatile 读允许编译器把多次调用合并、提到循环外（CSE/hoist），省掉重复 CSR 读。而 `active_threads`（当前 tmask）会随 SPLIT/JOIN 发散而改变，必须每次都真正去读 CSR，故用 volatile。

---

### 4.2 模块二：vx_spawn.h —— 软件派生与 CUDA 风格抽象

#### 4.2.1 概念说明

`vx_tmc`/`vx_wspawn` 是「原子操作」，直接手写很繁琐。`vx_spawn.h` 在它们之上提供了一套 **CUDA 风格的派生抽象**：你只要声明一个 grid（gridDim）和每个 block 的大小（blockDim），剩下的「把 work 撒到哪些 core/warp/thread」由 `vx_spawn_threads` 算好并用 WSPAWN+TMC 自动拉起。

这套抽象暴露的全局变量（[sw/kernel/include/vx_spawn.h:32-39](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_spawn.h#L32-L39)）对 CUDA 读者会很眼熟：

- `blockIdx` / `threadIdx`：`__thread` 修饰，每个线程各有一份，表示当前 block/线程在 grid/block 内的坐标。
- `gridDim` / `blockDim`：全局共享，grid 与 block 的维度。
- `__local_group_id` / `__sub_group_id` / `__warps_per_group`：CTA（cooperative thread array）内的 group 与子组（warp）坐标。

> 注意：这是**旧式「libvortex」软件派生模型**。kernel 自己的 `main()` 调用 `vx_spawn_threads` 来拉起整个 grid。下一节会对比 `vx_spawn2.h` 的硬件派生模型——后者把派生交给 KMU，kernel 里看不到 `vx_spawn_threads`。

#### 4.2.2 核心流程

`vx_spawn_threads` 的派生算法（见 [sw/kernel/src/vx_spawn.c:169-341](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/src/vx_spawn.c#L169-L341)）分两条路径，取决于 `block_dim` 是否大于 1：

```text
vx_spawn_threads(dim, grid_dim, block_dim, kernel_func, arg)
  ├─ 计算 num_groups = ∏grid_dim, group_size = ∏block_dim
  ├─ 读设备规格：num_cores / warps_per_core / threads_per_warp
  │
  ├─ 若 group_size > 1（每个 block 多于 1 线程）→ 「groups」路径
  │     · 算 warps_per_group = ⌈group_size / threads_per_warp⌉
  │     · 算每个 core 分到几个 group、激活几个 warp
  │     · 把参数写进 CSR MSCRATCH
  │     · vx_wspawn(active_warps, process_thread_groups_stub)  ← WSPAWN
  │     · process_thread_groups_stub() 在 warp0 上也跑一遍
  │           └─ stub 内部: vx_tmc(threads_mask) → 真正处理 → vx_tmc(...)  ← TMC
  │
  └─ 若 group_size == 1（每 block 单线程）→ 「threads」路径
        · 把 grid 当成一组「任务」平铺到所有线程
        · vx_wspawn(active_warps, process_threads_stub)         ← WSPAWN
        · vx_tmc(-1) → process_threads() → vx_tmc_one()         ← TMC
        · 处理剩下的零头线程

  收尾：vx_wspawn(1, 0)   ← 激活 1 个 warp、地址 0：作为「等待所有派生 warp 结束」的同步原语
```

两个关键点：

1. **派生 = WSPAWN + TMC 的组合**：stub 函数（如 [process_thread_groups_stub](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/src/vx_spawn.c#L152-L165)）先用 `vx_tmc` 选好本 warp 该激活哪些线程，再调用真正的处理函数，最后再用 `vx_tmc` 收敛回单线程。这正是模块一两条指令的真实用武之地。
2. **`vx_wspawn(1, 0)` 当同步**（[sw/kernel/src/vx_spawn.c:338](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/src/vx_spawn.c#L338)）：派生 warp 数为 1、目标地址为 0 是一个约定，作用是「等所有之前派生的 warp 都退休」。所以 `vx_spawn_threads` 返回时，整个 grid 已执行完毕。

#### 4.2.3 源码精读

**(1) CUDA 风格全局量与便捷宏**

[sw/kernel/include/vx_spawn.h:23-65](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_spawn.h#L23-L65) 定义了 `dim3_t` 三维坐标、`blockIdx`/`threadIdx` 等 `__thread` 全局量，以及两个高频宏：

```c
#define __local_mem(size) \
  (void*)((int8_t*)csr_read(VX_CSR_LOCAL_MEM_BASE) + get_local_group_id() * size)

#define __syncthreads() \
  vx_barrier(get_local_group_id(), get_num_sub_groups())
```

`__syncthreads()` 直接映射到模块一的 `vx_barrier`：屏障 id 用本 group 的 id，参与方数量用「本 group 的 warp 数」。这就是 CUDA 的块内同步在 Vortex 上的落点。

**(2) vx_spawn_threads 的入口**

[sw/kernel/src/vx_spawn.c:169-198](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/src/vx_spawn.c#L169-L198)：先算 `num_groups`/`group_size`、填好 `gridDim`/`blockDim`，再读设备规格，并校验 `group_size` 不超过一个 core 的线程总数（否则报错返回 -1）。

**(3) 多线程 block 的「groups」路径**

[sw/kernel/src/vx_spawn.c:199-260](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/src/vx_spawn.c#L199-L260) 把每个 block（group）映射到 `warps_per_group` 个 warp，计算每个 core 分配多少 group、需要激活多少 warp，然后把打包好的参数 `wspawn_groups_args_t` 写进 `VX_CSR_MSCRATCH`，最后：

```c
vx_wspawn(active_warps, process_thread_groups_stub);  // 拉起其他 warp
process_thread_groups_stub();                          // warp0 自己也跑
```

stub 内部（[process_thread_groups_stub](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/src/vx_spawn.c#L152-L165)）根据本 warp 在 group 内的位置算出该激活哪些线程（最后一个 warp 可能不满，用 `remaining_mask` 屏蔽多余线程），调用 `vx_tmc`：

```c
uint32_t threads_mask = (group_warp_id == warps_per_group-1) ? remaining_mask : -1;
vx_tmc(threads_mask);                 // 激活线程
process_thread_groups();              // 每个 thread 跑 kernel_func
vx_tmc(0 == vx_warp_id());            // 除 warp0 外关闭，回到单线程
```

**(4) 单线程 block 的「threads」路径**

[sw/kernel/src/vx_spawn.c:261-335](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/src/vx_spawn.c#L261-L335)：当每个 block 只有 1 个线程时，直接把 grid 当成平铺任务，分摊到所有线程。这里能清楚看到「全线程激活→处理→回到单线程」的 TMC 三连（[sw/kernel/src/vx_spawn.c:319-334](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/src/vx_spawn.c#L319-L334)）：

```c
vx_wspawn(active_warps, process_threads_stub);  // 拉起其他 warp
vx_tmc(-1);          // 激活全部线程
process_threads();   // 处理
vx_tmc_one();        // 回到单线程
...
vx_tmc(tmask);       // 处理零头
process_remaining_threads();
vx_tmc_one();
```

每个线程在 `process_threads` 内根据 `warp_id`/`thread_id` 反算出自己的 `blockIdx` 并调用回调（[process_threads](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/src/vx_spawn.c#L55-L84)）——这就是 `blockIdx.x = task_id % gridDim_x; ...` 的来源。

#### 4.2.4 代码实践

**实践目标**：对比「软件派生」与「硬件派生」两种 kernel 写法，看清 `vx_spawn_threads` 在哪里真的发出了 WSPAWN/TMC。

**操作步骤**：

1. 读 [tests/regression/vecadd_v1/kernel.cpp:1-15](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/vecadd_v1/kernel.cpp#L1-L15)（软件派生）：

   ```c
   void kernel_body(kernel_arg_t* __UNIFORM__ arg) {
       ...
       dst_ptr[blockIdx.x] = src0_ptr[blockIdx.x] + src1_ptr[blockIdx.x];
   }
   int main() {
       kernel_arg_t* arg = (kernel_arg_t*)csr_read(VX_CSR_MSCRATCH);
       return vx_spawn_threads(1, &arg->num_points, nullptr, (vx_kernel_func_cb)kernel_body, arg);
   }
   ```

   这里 `main()` 在设备 warp0 上运行，调用 `vx_spawn_threads` 把 `kernel_body` 撒到所有线程；`blockIdx.x` 由派生机制注入。

2. 用不同线程数跑这个向量加，观察每个线程拿到哪个 `blockIdx.x`：

   ```bash
   ./ci/blackbox.sh --driver=simx --app=vecadd_v1 --threads=4
   ./ci/blackbox.sh --driver=simx --app=vecadd_v1 --threads=8
   ```

3. 在 `vx_spawn.c` 里定位本次调用走的是「threads」路径（`block_dim` 传 `nullptr` ⇒ `group_size=1`），确认 `vx_wspawn`（[行 319](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/src/vx_spawn.c#L319)）和 `vx_tmc`（[行 321](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/src/vx_spawn.c#L321)）就是真正发出 WSPAWN/TMC 指令的两行。

**需要观察的现象**：`--threads` 改变时，每个线程处理的元素索引随之改变，但程序仍打印 `PASSED!`、退出码 0——说明派生算法自动把 work 重新分摊。

**预期结果**：两次都应通过校验。若 `vecadd_v1` 不在当前 testcases 列表，运行结果待本地验证；此时可退化为纯源码阅读：跟踪 `vx_spawn_threads → process_threads → blockIdx.x` 的赋值链即可。

#### 4.2.5 小练习与答案

**练习 1**：`vx_spawn.c` 末尾的 `vx_wspawn(1, 0)`（[行 338](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/src/vx_spawn.c#L338)）为什么能当「同步等待」用？

**参考答案**：它把派生 warp 数设为 1、目标地址设为 0，是一个约定语义：硬件会等到之前所有被 `vx_wspawn` 拉起的 warp 都退休后才让调用者继续。因此 `vx_spawn_threads` 返回时整个 grid 已跑完，调用者可以安全读取结果。

**练习 2**：`process_thread_groups_stub` 里为何要判断 `group_warp_id == warps_per_group-1` 并使用 `remaining_mask`？

**参考答案**：一个 block 的线程数未必是 `threads_per_warp` 的整数倍，最后一个 warp 往往只有部分线程属于本 block。用 `remaining_mask` 把多余线程在 tmask 里屏蔽掉，避免它们误写越界数据；非末尾 warp 激活全部线程（mask = `-1`）。

**练习 3**：CUDA 风格的 `__syncthreads()` 在 Vortex 上落到哪条硬件指令？为什么用 `get_local_group_id()` 当屏障 id？

**参考答案**：落到 `vx_barrier`（即 BAR 指令）。用 `get_local_group_id()` 当 id 是为了让**同一 block 内的 warp** 互相同步，而不同 block 的 warp 用不同的屏障 id 互不干扰；参与方数量取 `get_num_sub_groups()`（block 内的 warp 数），保证凑齐本 block 全部 warp 才放行。

---

## 5. 综合实践

**任务**：阅读 [tests/regression/demo/kernel.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/demo/kernel.cpp)，找出其中的「warp 派生」与「线程激活」调用，并画出 grid/block/warp 的派生关系图。

**关键发现（请先自己读源码再对照）**：demo 的 kernel 头部是 `#include <vx_spawn2.h>`（[tests/regression/demo/kernel.cpp:1](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/demo/kernel.cpp#L1)），用的是 **KMU 硬件派生模型**，kernel 里**没有**任何 `vx_wspawn`/`vx_tmc`/`vx_spawn_threads` 调用。warp 派生与线程激活是**隐式**的——由 KMU + CTA 调度器在硬件里完成（参见 u4-l1 的 `__vx_cta_entry` 与命令处理器一讲）。kernel 里能看到的只是身份坐标：

```c
__kernel void kernel_main(kernel_arg_t* __UNIFORM__ arg) {
    uint32_t gx = blockIdx.x * blockDim.x + threadIdx.x;   // 读 CSR
    uint32_t gy = blockIdx.y * blockDim.y + threadIdx.y;
    ...
}
```

在 `vx_spawn2.h` 中，`blockIdx`/`threadIdx`/`blockDim`/`gridDim` 是 `static const` 结构体，其 `.x/.y/.z` 通过 `csr_read_nv` 读 CTA 专用 CSR（如 `VX_CSR_CTA_BLOCK_ID_X`、`VX_CSR_CTA_THREAD_ID_X`，见 [sw/kernel/include/vx_spawn2.h:27-119](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_spawn2.h#L27-L119)）——这些 CSR 由 KMU 在派生每个 CTA 时注入。

**操作步骤**：

1. 打开 [tests/regression/demo/main.cpp:144-158](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/demo/main.cpp#L144-L158)，看清主机侧如何决定启动维度：

   ```text
   num_threads   = vx_device_query(VX_CAPS_NUM_THREADS)   （每 warp 线程数）
   block_dim_x   = num_threads （默认）, block_dim_y = 1
   total_threads = num_cores * num_warps * num_threads
   num_blocks    = ⌈total_threads / (block_dim_x*block_dim_y)⌉
   grid          = {num_blocks, 1}, block = {block_dim_x, block_dim_y}
   ```

2. 以默认配置 1 core / 4 warps / 4 threads 为例，画出派生关系图：

   ```text
   grid (num_blocks=4 个 block)
   ├── block (0): 1 个 warp，4 个线程 (threadIdx.x = 0..3)
   ├── block (1): 1 个 warp，4 个线程
   ├── block (2): 1 个 warp，4 个线程
   └── block (3): 1 个 warp，4 个线程

   因为 block_dim_x = num_threads = 4，每个 block 恰好 = 1 个 warp。
   KMU 把每个 block 当作一个 CTA 派发给某个 warp，并为它注入
   blockIdx / threadIdx / blockDim / gridDim 的 CSR 值。
   ```

3. **对比练习**：把 demo 的 kernel 与 [tests/regression/vecadd_v1/kernel.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/vecadd_v1/kernel.cpp)（软件派生）并排看，回答：为什么前者没有 `main()` 和 `vx_spawn_threads`，后者有？

**预期结果**：

- demo（`vx_spawn2.h`）：派生由 KMU 硬件完成，kernel 只声明 `__kernel` 入口并读 CTA CSR；`blockIdx`/`threadIdx` 是 CSR 读取的语法糖。
- vecadd_v1（`vx_spawn.h`）：kernel 自带 `main()`，调用 `vx_spawn_threads`，内部经 `vx_wspawn`+`vx_tmc` 软件派生。

**需要观察的现象**：用 `./ci/blackbox.sh --driver=simx --app=demo --threads=4 --warps=4 --cores=1` 运行，改变 `--threads`/`--warps`/`--cores` 时，主机侧 `num_blocks` 与 `grid`/`block` 维度会相应变化，但 kernel 源码一行都不用改——这正是把派生交给硬件（或统一抽象）的好处。运行通过判据为打印 `PASSED!` 且退出码 0；具体输出待本地验证。

## 6. 本讲小结

- Vortex 用 6 条复用 RISC-V custom0 槽位的指令（TMC/WSPAWN/SPLIT/JOIN/PRED/BAR）操纵 SIMT 的两层激活状态：warp 层（多少 warp）与线程层（warp 内多少线程）。
- `vx_intrinsics.h` 是这 6 条指令（外加 vote/shuffle/CSR/周期计数）的 C 包装；每条内联函数就是一条 `.insn r`，靠 `func3`/`func7` 字段区分具体操作。
- SPLIT/JOIN 配合 **IPDOM 栈**实现 warp 内分支发散与汇聚：SPLIT 压栈并按谓词分流，JOIN 弹栈恢复 tmask，PC 始终单条推进。
- `vx_spawn.h`+`vx_spawn.c` 提供 CUDA 风格的**软件派生**：`vx_spawn_threads` 把 grid/block 算好后，用 `vx_wspawn`+`vx_tmc` 自动拉起 warp 与线程，`vx_wspawn(1,0)` 当作同步收尾。
- `vx_spawn2.h` 提供**硬件派生**（KMU/CTA 模型）：kernel 只读 CTA CSR 取 `blockIdx`/`threadIdx`，派生完全由硬件完成——demo 就是这一类。
- `__syncthreads()` 直接映射到 `vx_barrier`，块内同步以 `get_local_group_id()` 为屏障 id。

## 7. 下一步学习建议

- **往调度器深处去**：本讲讲了「指令做什么」，下一站可读 SimX 的 `scheduler.cpp`/`barrier_unit.cpp` 与 RTL 的 `VX_split_join.sv`/`VX_ipdom_stack.sv`，看 IPDOM 栈与 tmask 在硬件里如何被维护（对应 u6-l1、u7-l3）。
- **命令处理器视角**：想知道 KMU 如何「注入 CTA CSR」并把一个 CTA 派发给 warp，可继续读命令处理器与 KMU（u11-l3）以及 u4-l1 的设备入口。
- **设备侧其他服务**：本讲的 `vx_barrier` 是块内同步；若想了解 `vx_printf`（COUT 环）、系统调用等设备服务，可读 u4-l3。
- **动手验证**：挑一个 `vx_spawn.h` 旧式测试（如 `vecadd_v1`）和一个 `vx_spawn2.h` 新式测试（如 `demo`），在 SimX 上分别加 `--debug=3` 跑一遍，在 trace 里找到 WSPAWN/TMC/SPLIT/JOIN 指令的踪迹，把本讲的知识落到具体周期上。
