# 使用 GDB 调试 CPU 域算子

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚为什么 CPU Debug 要「为每一个核启动一个独立的子进程」，以及这件事对 gdb 调试带来了什么直接影响。
- 掌握 `set follow-fork-mode child` 这条 gdb 指令的作用，并能解释不设置它会发生什么。
- 能在真实算子的核函数内部（例如 `Compute`）设置断点、单步执行，并尝试查看 `LocalTensor` 的内存内容。

本讲是「先用起来」的一讲：**重点是让你掌握一套可复现的 gdb 调试动作**，至于 fork 子进程的底层调度细节（信号处理、进程同步、多核同步检查）会留到 [u3-l1 多核 fork 执行模型](u3-l1-fork-execution-model.md) 深入。

## 2. 前置知识

在进入本讲前，你需要先建立下面这些概念（它们在 u2-l1、u2-l2 中已讲过，这里只做最小回顾）：

- **CPU 域 / NPU 域**：同一份 Ascend C 算子源码，可以在 CPU 上跑（CPU 域），也可以在真实 NPU 上跑（NPU 域）。CPU Debug 的价值，就是让你能在 CPU 上、用 gdb 把算子调通。
- **`<<<>>>` 核函数启动语法**：`add_custom<<<numBlocks, nullptr, stream>>>(x, y, z)` 是 ASC 语言层的核函数启动写法。在 CPU 模式下，bisheng 编译器在编译期把它「转义（lowering）」成对入口头文件里 `AscCPUKernelLaunch` 的普通 C++ 调用。
- **block（核）与 `numBlocks`**：NPU 上一个算子会被分配到多个核上并行执行，`numBlocks` 就是核数。add 样例里 `numBlocks = 8`，即 8 个核各算 1/8 的数据。
- **gdb 基础**：知道 `break`（打断点）、`run`（运行）、`next`（单步步过）、`continue`（继续到下一个断点）这几条最基本命令即可。

如果你对 fork 还不熟悉，记住一句话就够：**`fork()` 是 Unix 创建子进程的系统调用，调用一次会返回两次——在父进程里返回子进程的 pid（大于 0），在子进程里返回 0。** 本讲后面的核心，全部建立在「每个核 = 一个 fork 出来的子进程」这个事实上。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
|------|------|
| [examples/02_cpudebug/add.asc](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc) | add 算子的完整源码（含 `KernelAdd` 类、`Compute`、`main`）。它是我们打断点、单步、查看内存的对象。 |
| [examples/02_cpudebug/README.md](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/README.md) | 样例说明，给出 `gdb --args ./add` 的入口命令与编译运行步骤。 |
| [docs/01_cpu_debug.md](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/01_cpu_debug.md) | CPU Debug 官方文档，明确给出了 `set follow-fork-mode child` 的用法与原因。 |
| [cpudebug/include/cpu_debug_launch.h](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/cpu_debug_launch.h) | `<<<>>>` 转义后的落地入口 `AscCPUKernelLaunch`，它把执行托付给 `RunKernelFunctionOnCpu`。 |
| [cpudebug/include/kern_fwk.h](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kern_fwk.h) | `RunKernelFunctionOnCpu` 的实现，**fork 多核模型就在这里**，是本讲理解「为什么要 follow 子进程」的根。 |

> 提示：`kern_fwk.h` 属于 [u3-l1](u3-l1-fork-execution-model.md) 会深入精读的内容。本讲我们只读其中和「调试受影响」直接相关的片段，不展开信号处理与进程同步的全部细节。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. 多核 fork 模型对调试的影响（为什么会「进不去」核函数）。
2. gdb `follow-fork-mode`：让 gdb 跟踪正确的进程。
3. 断点与单步操作：真正进入核函数内部。

### 4.1 多核 fork 模型对调试的影响

#### 4.1.1 概念说明

先建立直觉：**NPU 上有多少个核在并行跑这个算子，CPU Debug 就在 CPU 上 fork 出多少个子进程，每个子进程模拟一个核。** 父进程负责「分发 + 等待」，子进程负责「真正执行核函数」。

这个设计的直接后果是——**你真正关心的算子逻辑（`Init` / `CopyIn` / `Compute` / `CopyOut`）全都跑在子进程里，而不在父进程里。** 父进程只做一件事：循环 fork，然后 `waitpid` 等所有子进程结束。

于是就有了新手最常遇到的困惑：

> 我在 `Compute` 打了断点，`run` 之后 gdb 直接跑完了，根本没停下来！

原因几乎总是：**gdb 默认跟踪父进程，而父进程根本不会调用 `Compute`**。`Compute` 在子进程里。要进入它，必须让 gdb「跟着 fork 出去的子进程走」。

#### 4.1.2 核心流程

把 `add_custom<<<numBlocks, ...>>>` 在 CPU 域的完整执行链路画出来：

```text
main()                                          [add.asc:166]
  └─ kernel_add(x, y)                           [add.asc:175]
       └─ add_custom<<<numBlocks, ...>>>(...)    [add.asc:123]   ← <<<>>> 启动
            │  (bisheng 编译期转义)
            ▼
       AscCPUKernelLaunch(...)                  [cpu_debug_launch.h:22]
            └─ RunKernelFunctionOnCpu(...)       [kern_fwk.h:75]
                 │
                 │  for idx in 0..processNum-1:  ← 父进程的循环
                 │      pid = fork()             ← 每个核 fork 一次
                 │      ├─ 父进程：记录 pid，继续下一次循环，最后 waitpid
                 │      └─ 子进程 (pid==0)：
                 │            设置核类型 / 信号处理
                 │            kernelFunc(args...) ← 真正执行 add_custom
                 │            exit(0)            ← 子进程算完即退出
                 ▼
       add_custom(...)                          [add.asc:90]
        └─ KernelAdd::Process()                 [add.asc:44]
             └─ CopyIn / Compute / CopyOut      [add.asc:55 / 64 / 74]
```

关键认知有两条：

1. **fork 发生在父进程的一个循环里**：父进程并非只 fork 一次，而是循环 `processNum` 次，每次 fork 出一个子进程模拟一个核。
2. **核函数只存在于子进程**：父进程的代码路径里压根没有 `Compute`，所以「跟踪父进程」永远断不到 `Compute`。

> 关于 `processNum`：它由 `get_process_num()` 返回。对于 add 这种简单算子，进程数就等于核数 `numBlocks = 8`。需要注意，`get_process_num`、`set_block_dim`、`set_core_type`、`GetBlockIdx` 等函数在开源代码里**只被调用、没有定义**——它们的实现位于 cpudebug 的闭源模型库（`libcpudebug_model.a`）中，链接时才解析进来。这是 asc-tools 的开源/闭源边界之一，我们在 [u9-l1](u9-l1-cmake-multi-arch.md) 会专门讲。

#### 4.1.3 源码精读

先看启动链路的「分发」入口。`<<<>>>` 在 CPU 模式被转义成对 `AscCPUKernelLaunch` 的调用，而它只做两件事：设置 kernel 模式，然后立刻把执行权交给 `RunKernelFunctionOnCpu`——它自己不执行算子逻辑。

[cpu_debug_launch.h:21-27](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/cpu_debug_launch.h#L21-L27) 中，`AscCPUKernelLaunch` 先按核函数名取出 kernel 模式，再调用 `RunKernelFunctionOnCpu`。注意它「只分发、不执行」。

真正决定「多核 = 多进程」的是 `RunKernelFunctionOnCpu` 里的 fork 循环。我们只看和调试相关的骨架（省略了 workspace 初始化等无关代码）：

[kern_fwk.h:101-104](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kern_fwk.h#L101-L104) 先确定进程数：`block_num = numBlocks`，再用 `get_process_num()` 得到本次要 fork 出多少个子进程。

[kern_fwk.h:111-151](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kern_fwk.h#L111-L151) 是核心的 fork 循环，关键几行：

```cpp
for (idx = 0; idx < processNum; ++idx) {
    set_block_dim(idx);
    int pid = fork();                 // 每个核 fork 一个子进程
    ...
    if (pid == 0) {                   // —— 子进程分支 ——
        ...                           // 注册信号处理、设置核类型
        kernelFunc(args...);          // 真正执行 add_custom（Compute 就在这里被调到）
        exit(0);                      // 子进程算完立即退出
    }
}
```

- [kern_fwk.h:113](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kern_fwk.h#L113)：`fork()` 是分叉点。
- [kern_fwk.h:137](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kern_fwk.h#L137)：`kernelFunc(args...)` 才是真正调用算子（`add_custom`）的地方，`Compute` 在它的调用链深处。
- [kern_fwk.h:149](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kern_fwk.h#L149)：子进程执行完立刻 `exit(0)`，不会回到父进程的循环。

父进程做什么？它在循环结束后用 `waitpid` 等所有子进程结束，并逐个打印结果：

[kern_fwk.h:163-171](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kern_fwk.h#L163-L171) 父进程对每个子进程 `waitpid`，正常退出时打印 `[SUCCESS][<核名>][pid xxx] exit success!`。注意：**父进程的这条路径里完全没有 `Compute`**，这正是默认 gdb 跟踪父进程时「断点不命中」的根因。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目标是让你亲眼确认「核函数在子进程里」这件事，不依赖运行环境。

1. **实践目标**：通过读源码，定位「`Compute` 究竟被谁调用、在哪个进程里执行」。
2. **操作步骤**：
   - 打开 [add.asc](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc)，找到 `Compute` 函数（[L64-L73](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L64-L73)）。
   - 沿调用链往上追：`Compute` ← `Process`（[L44-L52](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L44-L52)）← `add_custom`（[L90-L95](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L90-L95)）← `<<<>>>` 启动（[L123](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L123)）。
   - 切到 [kern_fwk.h](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kern_fwk.h)，确认 `kernelFunc(args...)`（[L137](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kern_fwk.h#L137)）位于 `if (pid == 0)` 分支内（即子进程）。
3. **需要观察的现象**：`Compute` 的整条调用链最终落在 `fork()` 之后、`pid == 0` 的分支里。
4. **预期结果**：你能用一句话回答——「`Compute` 跑在子进程中，所以 gdb 必须跟踪子进程才能断到它」。
5. 无法本地编译运行时，本实践结论依然成立（纯源码推导），故无需标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `numBlocks` 从 8 改成 1（单核），还需要 `set follow-fork-mode child` 吗？为什么？

> **答案**：仍然需要。即使只有一个核，`fork()` 依然会发生一次，核函数依然在 `pid == 0` 的子进程里执行（[kern_fwk.h:137](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kern_fwk.h#L137)）。核数只决定 fork 的次数，不改变「核函数在子进程」这一事实。

**练习 2**：父进程的代码路径里为什么找不到 `Compute`？

> **答案**：因为父进程在 `fork()` 之后，通过 `if (pid == 0)` 把 `kernelFunc(args...)` 限定在了子进程分支；父进程走的是循环继续 + `waitpid`（[kern_fwk.h:163-171](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kern_fwk.h#L163-L171)），自然不会调用 `Compute`。

---

### 4.2 gdb follow-fork-mode：跟踪正确的进程

#### 4.2.1 概念说明

gdb 在遇到 `fork()` 时，默认行为是**继续跟踪父进程**（`follow-fork-mode parent`），并自动 detach（脱离）子进程。这正是前面「断不到 `Compute`」的原因。

把模式改成 child：

```text
(gdb) set follow-fork-mode child
```

它告诉 gdb：**遇到 fork 时，切换到子进程继续调试，脱离父进程。** 这样 gdb 就会跟着子进程进入 `kernelFunc`，你打的 `Compute` 断点才会命中。

官方文档把这一点说得非常直白：

> [docs/01_cpu_debug.md:48](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/01_cpu_debug.md#L48)：CPU Debug 通过为每个核函数启动单独的子进程来模拟 NPU 的执行逻辑，因此使用 gdb 调试时，需要设置 `follow-fork-mode` 让 gdb 跟踪子进程。

一个**重要的细节**（gnu gdb 的通用行为，非项目特有）：在默认 `detach-on-fork on` 下，gdb 跟踪子进程时只能跟**第一个**被 fork 出来的子进程（即 `idx == 0` 的核，对应核名通常是 `CORE_0` / `AIV_0`）。其余子进程会脱离 gdb 自行运行。所以用最简配置时，你实际调试的是「0 号核」。调试非 0 号核属于进阶用法，见 4.2.4 的扩展说明。

#### 4.2.2 核心流程

```text
启动 gdb，加载可执行程序
   ↓
(gdb) set follow-fork-mode child      ← 关键开关：fork 时跟子进程
   ↓
(gdb) break Compute                   ← 在核函数逻辑里打断点
   ↓
(gdb) run                             ← 运行，命中 fork
   ↓
gdb 脱离父进程，attach 到子进程（0 号核）
   ↓
程序停在 Compute 入口                   ← 断点命中！可以单步/查内存了
```

#### 4.2.3 源码精读

我们对照官方文档把命令逐条对上源码意图。

[docs/01_cpu_debug.md:44-60](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/01_cpu_debug.md#L44-L60) 给出了完整的 gdb 用法：先用 `gdb ./build/add` 进入，再 `set follow-fork-mode child`，然后才 `break Compute` / `run`。注意**顺序**：要在 `run` **之前**设置好 `follow-fork-mode`，否则 gdb 已经按默认的 parent 模式跟住父进程了。

[docs/01_cpu_debug.md:78](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/01_cpu_debug.md#L78) 明确解释了原理：不设置该选项时 gdb 默认跟踪父进程，将无法进入核函数内部。

样例 README 则给出了等价的、更推荐的启动写法——用 `--args` 直接把可执行文件和参数一起交给 gdb：

[examples/02_cpudebug/README.md:88-94](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/README.md#L88-L94) 中 `gdb --args ./add`，进入后即可按需设置断点、单步或查看调用栈。

> `gdb ./add` 与 `gdb --args ./add` 的区别：前者进入 gdb 后还需手动 `run`；后者用 `--args` 预先指定了要运行的程序（及参数），同样需要 `run` 才真正启动。两者都行，`--args` 在需要传命令行参数时更方便。

#### 4.2.4 代码实践

1. **实践目标**：亲手验证「设了 `follow-fork-mode child` 能进核函数，不设就进不去」。
2. **操作步骤**（前置：按 u1-l4 编译安装好 asc-tools，再按样例用 `CMAKE_ASC_RUN_MODE=cpu` 编出 `./add`）：
   - 第一轮（对照组）：`gdb --args ./add`，**不**设置 follow-fork-mode，直接 `break Compute` 然后 `run`。
   - 第二轮（实验组）：再次 `gdb --args ./add`，先 `set follow-fork-mode child`，再 `break Compute`，最后 `run`。
3. **需要观察的现象**：
   - 对照组：程序一路跑完，最终输出 `[Success] Case accuracy is verification passed.`，**断点未命中**。
   - 实验组：程序停在 `Compute` 入口，gdb 提示命中断点，并显示当前位于子进程。
4. **预期结果**：对照组不命中、实验组命中。这正反向印证了 4.1 的结论。
5. 本实践依赖本地已编译出 CPU 域可执行程序；若当前环境未安装 CANN/cpudebug，实际运行结果**待本地验证**，但结论可由源码确定。

> **扩展（gnu gdb 通用知识，非项目特有）**：若想调试 0 号以外的核，可使用 `set detach-on-fork off` 让 gdb 同时保留父进程与所有子进程，再用 `info inferiors` / `inferior <编号>` 切换到目标子进程。这是 gdb 的多进程调试能力，asc-tools 文档未展开，了解即可。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `set follow-fork-mode child` 必须在 `run` 之前执行？

> **答案**：fork 发生在程序运行期间。`run` 之后 gdb 已经按当前模式（默认 parent）跟踪了父进程；模式只对**之后**即将发生的 fork 生效，所以必须先设好再 `run`（见 [docs/01_cpu_debug.md:56-69](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/01_cpu_debug.md#L56-L69) 的命令顺序）。

**练习 2**：默认配置下，`follow-fork-mode child` 实际让你调试的是第几号核？为什么？

> **答案**：0 号核。父进程循环 fork，第一次 fork 产生 0 号核的子进程；gdb 在该次 fork 处切换到子进程并脱离父进程，后续 fork（1~7 号核）不再被 gdb 跟踪（默认 `detach-on-fork on`）。

---

### 4.3 断点与单步操作：进入核函数内部

#### 4.3.1 概念说明

跟住子进程之后，gdb 的常规调试能力就全部可用了——断点、单步、查看调用栈、查看内存。本模块把这些动作对齐到 add 样例的真实代码上，让你知道「断在哪里能看到什么」。

回顾 add 样例的三段式结构（u2-l2 已详述），它正是调试时最值得下手的几个点：

- `CopyIn`：把数据从 Global Memory 搬进 Unified Buffer（`xLocal` / `yLocal` 在这里被分配和填充）。
- `Compute`：真正做向量加法 `AscendC::Add` 的地方（最常打断点的位置）。
- `CopyOut`：把结果搬回 Global Memory。

#### 4.3.2 核心流程

进入核函数后的典型调试动作：

```text
命中 Compute 断点
   ↓
(gdb) next                 ← 单步步过，逐行观察 CopyIn/Compute/CopyOut
(gdb) bt                   ← 查看调用栈：Compute ← Process ← add_custom ← kernelFunc ← RunKernelFunctionOnCpu
(gdb) print xLocal         ← 查看输入 tensor 对象
(gdb) print yLocal
(gdb) continue             ← 继续到下一个断点
```

#### 4.3.3 源码精读

断点的首选目标——`Compute`，它取出输入、做加法、入队输出：

[examples/02_cpudebug/add.asc:64-73](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L64-L73) 是 `Compute` 的全部实现。可以看到 `xLocal`、`yLocal` 由 `DeQue` 取出（[L66-L67](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L66-L67)），真正的加法在 [L69](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L69) `AscendC::Add(zLocal, xLocal, yLocal, TILE_LENGTH)`。

`xLocal` / `yLocal` 的数据从哪来？来自 `CopyIn` 把 `xGm` / `yGm` 搬进 Unified Buffer：

[examples/02_cpudebug/add.asc:55-63](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L55-L63) 中 `DataCopy(xLocal, xGm[progress * TILE_LENGTH], TILE_LENGTH)` 把全局数据拷进 `xLocal`。而全局数据本身由 `main` 初始化：

[examples/02_cpudebug/add.asc:171-174](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L171-L174) 中 `x[i] = i * 0.1f; y[i] = i * 0.1f;`。所以**断在 `Compute`、单步到 `Add` 之前，`xLocal` 与 `yLocal` 的值应当相等**，且都是 `i * 0.1f` 形式——这是你判断「数据是否正确搬入」的黄金参照。

> **关于查看 `LocalTensor` 内存**：`LocalTensor` 在开源代码里只做了前向声明（`class LocalTensor;`，见 `kernel_utils_struct_norm_sort.h`），其完整实现位于 cpudebug 闭源模型库中。因此：
> - `print xLocal` 能打印出该对象的可见成员，是查看它最稳妥的第一步。
> - 想直接读到底层 float 数值（例如 `*(float*)<地址>`），需要从对象里取到它在 CPU 侧模拟 Unified Buffer 中的起始指针；**该成员名属于闭源实现细节，开源代码中无法确认，待确认**。
> - 不依赖闭源细节的可靠做法：用上面的「输入恒等参照」——既然 `x[i] == y[i] == i * 0.1f`，只要 `Add` 之后 `zLocal == 2 * i * 0.1f`（最终 `VerifyResult` 通过），就反向说明 `xLocal`/`yLocal` 搬入正确。

官方文档列出的常用命令（断点 / 运行 / 单步 / 继续）可直接照搬：

[docs/01_cpu_debug.md:62-76](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/docs/01_cpu_debug.md#L62-L76) 给出 `break Compute` / `run` / `next` / `continue` 的标准组合。

#### 4.3.4 代码实践

这是本讲的核心动手任务（即任务规格指定的实践）。

1. **实践目标**：用 gdb 进入 add 样例的 `Compute`，单步到 `Add` 调用前，确认 `xLocal` / `yLocal` 已正确搬入。
2. **操作步骤**（前置：CPU 域已编出 `./add`）：
   ```bash
   cd examples/02_cpudebug/build
   gdb --args ./add
   ```
   进入 gdb 后：
   ```text
   (gdb) set follow-fork-mode child
   (gdb) break Compute
   (gdb) run
   # 命中断点后
   (gdb) next          # 走到 DeQue 取出 xLocal / yLocal 之后
   (gdb) print xLocal
   (gdb) print yLocal
   (gdb) next          # 单步接近 AscendC::Add
   (gdb) bt            # 观察调用栈，确认在子进程的核函数链路里
   ```
3. **需要观察的现象**：
   - 命中 `Compute` 断点时，gdb 提示处于 fork 出的子进程中。
   - `bt` 显示调用栈包含 `Compute` ← `Process` ← `add_custom`。
   - `print xLocal` / `print yLocal` 打印出 LocalTensor 对象（其底层 float 值应为 `i * 0.1f` 形式，且 x、y 相等）。
4. **预期结果**：能停在 `Compute`、单步到 `Add` 之前，并通过对象打印 / 输入恒等参照确认 `xLocal`、`yLocal` 数据正确；继续运行后最终输出 `[Success] Case accuracy is verification passed.`
5. 实际运行依赖本地 CANN/cpudebug 环境，**待本地验证**；`LocalTensor` 底层指针的精确成员名因属闭源实现，亦**待确认**。

#### 4.3.5 小练习与答案

**练习 1**：断在 `Compute` 后，`bt`（backtrace）应该能看到哪些函数？

> **答案**：自顶向下大致是 `Compute` → `Process` → `add_custom`（核函数）→ `RunKernelFunctionOnCpu` 中的 `kernelFunc(args...)`（[kern_fwk.h:137](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/include/kern_fwk.h#L137)）→ `AscCPUKernelLaunch`。这条栈正好印证了 4.1 画出的调用链。

**练习 2**：为什么可以用「`x[i] == y[i] == i*0.1f`」来间接验证 `xLocal`/`yLocal` 是否正确？

> **答案**：`main` 中 `x`、`y` 都初始化为 `i * 0.1f`（[add.asc:171-174](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L171-L174)），`CopyIn` 原样搬入 `xLocal`/`yLocal`。只要后续 `Add` 得到 `2 * i * 0.1f` 且 `VerifyResult` 通过，就说明搬入的数据与预期一致，无需直接窥探闭源 `LocalTensor` 内部。

**练习 3**：如果断点设在 `main`（而非 `Compute`），还需要 `follow-fork-mode child` 吗？

> **答案**：调试 `main` 本身不需要，因为 `main` 在父进程里（父进程从 `main` 开始执行）。只有要进入 `Compute`/`CopyIn` 等「核函数逻辑」时才需要切到子进程。但通常我们调试算子关心的正是核函数内部，所以 `follow-fork-mode child` 几乎是必设项。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「定位并观察一次完整 tile 计算」的小任务：

**背景**：add 样例里每个核会循环执行 `TILE_NUM * BUFFER_NUM` 次 CopyIn/Compute/CopyOut（见 [add.asc:46-52](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L46-L52)）。你要用 gdb 抓住**其中一次**完整的三段式执行。

**任务步骤**：

1. 按 u1-l4 + 样例 README 编出 CPU 域 `./add`。
2. `gdb --args ./add`，设置 `set follow-fork-mode child`。
3. 在 `Process`（[add.asc:44](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/examples/02_cpudebug/add.asc#L44)）打断点并 `run`。
4. 命中后，用 `next` 逐步走过 `CopyIn(i)` → `Compute(i)` → `CopyOut(i)` 一整轮，观察 `i` 的变化。
5. 再把断点改到 `Compute`，`continue` 让它命中多次，体会「同一个核内多次 tile 循环」。
6. 退出 gdb 后，确认程序最终仍输出 `[Success] Case accuracy is verification passed.`

**验收标准**：能说清楚——① 为什么必须 `follow-fork-mode child`；② `Process` 的循环体由哪三步组成；③ 单步过程中如何用 `print` / 输入恒等参照确认数据正确。

> 实际运行依赖本地 CANN/cpudebug 环境，命令可执行性与具体输出**待本地验证**。

## 6. 本讲小结

- CPU Debug 用「一个核 = 一个 fork 子进程」来模拟 NPU 多核，核函数（`add_custom` → `Compute` 等）**全部跑在子进程里**，父进程只负责 fork 与 `waitpid`。
- 这导致 gdb 默认（跟踪父进程）时**断不到** `Compute`；必须 `set follow-fork-mode child` 让 gdb 在 fork 处切换到子进程，断点才会命中。
- 命令顺序很重要：先设 `follow-fork-mode child`，再 `break`，最后 `run`；默认配置下实际调试的是 0 号核。
- 进入核函数后，gdb 常规能力全可用：`break Compute` / `next` / `bt` / `print xLocal` / `continue`。
- `LocalTensor` 的完整实现位于闭源模型库，`print` 对象可行；直接读取底层 float 指针的成员名**待确认**，可用「输入恒等 `x[i]==y[i]==i*0.1f`」作为数据正确性的间接参照。
- 本讲只用到 `kern_fwk.h` 的 fork 骨架；信号处理、进程同步、`Handler` 清理等细节留待 [u3-l1](u3-l1-fork-execution-model.md)。

## 7. 下一步学习建议

- 接下来强烈建议学 [u3-l1 多核 fork 执行模型](u3-l1-fork-execution-model.md)：它会带你看完 `RunKernelFunctionOnCpu` 的**全部**细节——workspace 分配、`Handler` 信号处理、`waitpid` 进程同步、`GetCoreName` 如何把进程映射成核名（`AIV_0`/`AIC_0` 等）。
- 如果你对「算子执行时还会被同步检查内存错误」感兴趣，可以提前翻 [u5-l1 npu check 错误体系](u5-l1-npuchk-error-system.md)，看 `kern_fwk.h` 里 `AscendCKernelBegin`/`AscendCBlockBegin` 与 `try { kernelFunc } catch` 那一段是如何在执行的同时收集错误的。
- 想巩固 Ascend C 源码结构（三段式、TQue、`<<<>>>`），可回看 [u2-l2 Ascend C 算子源码与 .asc 核函数结构](u2-l2-asc-kernel-source.md)。
```
